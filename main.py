from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import uuid
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv

# Load environment variables from a local .env file if present. Done before
# any os.getenv() call below so the rest of the module sees the values.
load_dotenv()

from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from starlette.background import BackgroundTask

import cache
import db
import youtube_external_download as youtube_download

BASE_DIR = Path(__file__).resolve().parent

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("drakonrhym")


def _positive_int_env(name: str, default: str) -> int:
    raw = os.getenv(name, default)
    try:
        value = int(raw)
    except ValueError as e:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from e
    if value < 1:
        raise RuntimeError(f"{name} must be >= 1, got {value}")
    return value


def _origins_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default).strip()
    if not raw:
        return []
    return [o.strip() for o in raw.split(",") if o.strip()]


MAX_CONCURRENT = _positive_int_env("DRAKON_MAX_CONCURRENT", "2")
YT_DLP_TIMEOUT = _positive_int_env("DRAKON_YT_DLP_TIMEOUT", "300")
FFMPEG_TIMEOUT = _positive_int_env("DRAKON_FFMPEG_TIMEOUT", "600")
MAX_DURATION_SECONDS = _positive_int_env("DRAKON_MAX_DURATION_SECONDS", "420")
RATE_LIMIT_PER_DAY = _positive_int_env("DRAKON_RATE_LIMIT_PER_DAY", "20")
ALLOWED_ORIGINS = _origins_env("DRAKON_ALLOWED_ORIGINS", "*")
GOOGLE_CLIENT_ID = os.getenv("DRAKON_GOOGLE_CLIENT_ID", "").strip()
YT_DLP_COOKIES_FILE = os.getenv("DRAKON_YT_DLP_COOKIES_FILE", "").strip()
YT_DLP_COOKIES_FROM_BROWSER = os.getenv("DRAKON_YT_DLP_COOKIES_FROM_BROWSER", "").strip()
YT_DLP_JS_RUNTIME = os.getenv("DRAKON_YT_DLP_JS_RUNTIME", "deno").strip()
YT_DLP_PRIMARY_FORMAT = os.getenv("DRAKON_YT_DLP_PRIMARY_FORMAT", "bestaudio/best").strip()
YT_DLP_HLS_FALLBACK_FORMAT = os.getenv("DRAKON_YT_DLP_HLS_FALLBACK_FORMAT", "91/92/93/94/95/96").strip()
ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}

if not GOOGLE_CLIENT_ID:
    logger.warning(
        "DRAKON_GOOGLE_CLIENT_ID is empty — Google sign-in checks are DISABLED. "
        "Set this env var in production to require authenticated requests."
    )

_download_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
_google_request = google_requests.Request()


class _RateLimiter:
    """Sliding window counter, keyed by an arbitrary string. In-memory,
    single-process — fine for one uvicorn worker. Production with multiple
    workers/instances should swap this for Redis."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self._limit = limit
        self._window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()

    async def consume(self, key: str) -> tuple[bool, int]:
        """Try to record a hit for `key`. Return (allowed, remaining)."""
        async with self._lock:
            now = time.time()
            cutoff = now - self._window
            timestamps = [t for t in self._hits.get(key, []) if t > cutoff]
            if len(timestamps) >= self._limit:
                self._hits[key] = timestamps
                return False, 0
            timestamps.append(now)
            self._hits[key] = timestamps
            return True, self._limit - len(timestamps)

    async def refund(self, key: str) -> None:
        """Pop the most recent recorded hit for `key`. Used when a consumed
        request later fails for a reason that isn't the user's fault."""
        async with self._lock:
            timestamps = self._hits.get(key)
            if not timestamps:
                return
            timestamps.pop()
            if timestamps:
                self._hits[key] = timestamps
            else:
                # Drop empty entries so the dict doesn't grow with each user.
                self._hits.pop(key, None)


# Daily quota on /api/download (the expensive endpoint).
_download_rate = _RateLimiter(limit=RATE_LIMIT_PER_DAY, window_seconds=86400)
# Burst cap on /api/metadata so authenticated users can't pin yt-dlp
# subprocesses by spamming the metadata endpoint.
_metadata_rate = _RateLimiter(limit=30, window_seconds=60)


def _verify_google_id_token_claims(authorization: Optional[str]) -> Optional[dict]:
    """Verify a Bearer ID token and return the full claims dict.

    Raises 401 on any failure. Returns None when `GOOGLE_CLIENT_ID` is empty
    (DEV mode — auth is disabled).
    """
    if not GOOGLE_CLIENT_ID:
        return None
    if not authorization or not authorization.startswith("Bearer "):
        # Never log the header value itself — even a malformed value might
        # contain a real bearer token someone tried to send.
        logger.info("Auth rejected: header missing or malformed")
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty bearer token.")
    try:
        claims = google_id_token.verify_oauth2_token(token, _google_request, GOOGLE_CLIENT_ID)
    except ValueError as e:
        logger.info("Google token verification failed: %s", e)
        raise HTTPException(status_code=401, detail="Invalid Google token.") from e
    if not claims.get("sub"):
        raise HTTPException(status_code=401, detail="Token missing subject claim.")
    return claims


def _verify_google_id_token(authorization: Optional[str]) -> Optional[str]:
    """Backwards-compatible wrapper that returns just the `sub` claim."""
    claims = _verify_google_id_token_claims(authorization)
    return claims.get("sub") if claims else None


async def _auth_and_upsert(authorization: Optional[str]) -> tuple[Optional[str], Optional[dict]]:
    """Verify the token and (if Supabase is configured) upsert the user row.

    Returns (google_sub, user_row). `user_row` is None when Supabase is off
    or the upsert failed; callers should treat that as 'persistence disabled,
    fall back to anonymous behaviour'.

    `db.upsert_user` is a synchronous HTTP call to Supabase, so we offload
    it to a worker thread to keep the event loop free.
    """
    claims = _verify_google_id_token_claims(authorization)
    if claims is None:
        return None, None
    google_sub = claims["sub"]
    if not db.is_enabled():
        return google_sub, None
    user_row = await asyncio.to_thread(
        db.upsert_user,
        google_sub,
        email=claims.get("email"),
        name=claims.get("name"),
        avatar_url=claims.get("picture"),
    )
    return google_sub, user_row

app = FastAPI(title="DrakonRhymServer", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "Content-Disposition",
        "X-Pitch-Applied",
        "X-Quota-Remaining",
        "X-Cache",
        "X-Source-Provider",
        "X-Processing-Time",
        "X-Download-Strategy",
    ],
)


def _is_valid_youtube_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.netloc or "").lower().split(":")[0]
    return host in ALLOWED_HOSTS


def _yt_dlp_auth_args(cookies_file: Optional[str] = None) -> list[str]:
    if cookies_file:
        return ["--cookies", cookies_file]
    if YT_DLP_COOKIES_FILE:
        return ["--cookies", YT_DLP_COOKIES_FILE]
    if YT_DLP_COOKIES_FROM_BROWSER:
        return ["--cookies-from-browser", YT_DLP_COOKIES_FROM_BROWSER]
    return []


def _yt_dlp_js_runtime_args() -> list[str]:
    if not YT_DLP_JS_RUNTIME:
        return []
    return ["--js-runtimes", YT_DLP_JS_RUNTIME]


def _yt_dlp_cmd(*args: str, cookies_file: Optional[str] = None) -> list[str]:
    return [
        sys.executable,
        "-m",
        "yt_dlp",
        *_yt_dlp_auth_args(cookies_file),
        *_yt_dlp_js_runtime_args(),
        *args,
    ]


def _copy_ytdlp_cookies(source: Path, workdir: Path, req_id: str) -> Path:
    cookie_copy = workdir / f"yt_dlp_cookies_{req_id}.txt"
    shutil.copyfile(source, cookie_copy)
    cookie_copy.chmod(0o600)
    return cookie_copy


async def _run_subprocess(
    cmd: list[str], timeout: int, req_id: str, label: str
) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("[%s] %s timed out after %ds; killing", req_id, label, timeout)
        proc.kill()
        await proc.wait()
        raise HTTPException(
            status_code=504,
            detail=f"{label} timed out after {timeout}s.",
        )
    except asyncio.CancelledError:
        # Client disconnected. Make sure we don't leak the subprocess.
        logger.info("[%s] %s cancelled; killing subprocess", req_id, label)
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, stdout, stderr


async def _run_ytdlp(
    args: list[str],
    timeout: int,
    req_id: str,
    label: str,
    cookie_workdir: Optional[Path] = None,
) -> tuple[int, bytes, bytes]:
    temp_cookie_dir: Optional[Path] = None
    copied_cookie: Optional[Path] = None

    if YT_DLP_COOKIES_FILE:
        if cookie_workdir is None:
            temp_cookie_dir = Path(tempfile.mkdtemp(prefix="drakonrhym_cookies_"))
            cookie_workdir = temp_cookie_dir
        try:
            copied_cookie = _copy_ytdlp_cookies(Path(YT_DLP_COOKIES_FILE), cookie_workdir, req_id)
        except OSError as e:
            logger.exception("[%s] failed to prepare yt-dlp cookies file", req_id)
            raise HTTPException(
                status_code=500,
                detail="Configured YouTube cookies file is not readable.",
            ) from e

    cmd = _yt_dlp_cmd(
        *args,
        cookies_file=str(copied_cookie) if copied_cookie is not None else None,
    )
    try:
        return await _run_subprocess(cmd, timeout, req_id, label)
    finally:
        if copied_cookie is not None:
            copied_cookie.unlink(missing_ok=True)
        if temp_cookie_dir is not None:
            _cleanup(temp_cookie_dir)


async def _probe_duration_seconds_with_ytdlp(url: str, req_id: str) -> Optional[int]:
    """Legacy yt-dlp-only duration probe used by the external chain fallback."""
    args = [
        "--no-warnings",
        "--no-playlist",
        "--socket-timeout",
        "20",
        "--skip-download",
        "--print",
        "%(duration)s",
        url,
    ]
    code, stdout, stderr = await _run_ytdlp(args, YT_DLP_TIMEOUT, req_id, "yt-dlp-duration")
    if code != 0:
        logger.info("[%s] duration probe failed: %s", req_id, stderr.decode(errors="replace"))
        return None
    raw = stdout.decode(errors="replace").strip().splitlines()[0] if stdout else ""
    try:
        return int(float(raw))
    except (ValueError, IndexError):
        logger.info("[%s] could not parse duration %r", req_id, raw)
        return None


def _download_audio_args(output_template: str, format_selector: str) -> list[str]:
    return [
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout",
        "30",
        "--retries",
        "2",
        # Defence-in-depth: also enforce the duration cap inside yt-dlp so
        # even a direct API call that skipped /api/metadata gets rejected.
        "--match-filter",
        f"duration<={MAX_DURATION_SECONDS}",
        "-f",
        format_selector,
        "-x",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "0",
        "-o",
        output_template,
    ]


def _should_retry_with_hls_fallback(stderr: bytes) -> bool:
    text = stderr.decode(errors="ignore").lower()
    return "http error 403" in text or "403 forbidden" in text


def _raise_if_bot_blocked(decoded: str) -> None:
    """Raise HTTPException if the decoded stderr contains a bot-block signal."""
    if youtube_download._contains_bot_signal(decoded):
        raise HTTPException(
            status_code=400,
            detail="YouTube blocked downloads from this server. Please try again later.",
        )


async def _fetch_metadata_with_ytdlp(url: str) -> youtube_download.DownloadResult:
    req_id = uuid.uuid4().hex[:8]
    args = [
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        "--socket-timeout",
        "20",
        "--no-playlist",
        url,
    ]
    code, stdout, stderr = await _run_ytdlp(args, YT_DLP_TIMEOUT, req_id, "yt-dlp-metadata")
    if code != 0:
        decoded = stderr.decode(errors="replace")
        logger.error("[%s] metadata fetch failed: %s", req_id, decoded)
        _raise_if_bot_blocked(decoded)
        raise HTTPException(status_code=400, detail="Could not read video metadata.")

    try:
        data = json.loads(stdout.decode())
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail="Failed to parse video metadata.") from e

    duration = data.get("duration")
    return youtube_download.DownloadResult(
        path=None,
        provider="youtube",
        title=data.get("title"),
        duration=int(duration) if isinstance(duration, (int, float)) else None,
        filesize=None,
        external_provider=None,
        video_id=data.get("id"),
        thumbnail=data.get("thumbnail"),
        channel=data.get("uploader") or data.get("channel"),
        duration_string=data.get("duration_string"),
        view_count=data.get("view_count"),
        download_url=None,
        source_ext=data.get("ext"),
    )


async def _download_audio_with_ytdlp(
    url: str, workdir: Path, req_id: str
) -> youtube_download.DownloadResult:
    output_template = str(workdir / "source.%(ext)s")
    args = [*_download_audio_args(output_template, YT_DLP_PRIMARY_FORMAT), url]
    code, _, stderr = await _run_ytdlp(args, YT_DLP_TIMEOUT, req_id, "yt-dlp", workdir)
    if code != 0 and YT_DLP_HLS_FALLBACK_FORMAT and _should_retry_with_hls_fallback(stderr):
        logger.info(
            "[%s] yt-dlp primary format failed with 403; retrying HLS fallback format %s",
            req_id,
            YT_DLP_HLS_FALLBACK_FORMAT,
        )
        for candidate in workdir.glob("source.*"):
            candidate.unlink(missing_ok=True)
        fallback_args = [*_download_audio_args(output_template, YT_DLP_HLS_FALLBACK_FORMAT), url]
        code, _, stderr = await _run_ytdlp(
            fallback_args,
            YT_DLP_TIMEOUT,
            req_id,
            "yt-dlp-hls-fallback",
            workdir,
        )
    if code != 0:
        decoded = stderr.decode(errors="replace")
        logger.error("[%s] yt-dlp failed: %s", req_id, decoded)
        _raise_if_bot_blocked(decoded)
        raise HTTPException(status_code=400, detail="Failed to download audio from the given URL.")

    candidates = list(workdir.glob("source.*"))
    if not candidates:
        # yt-dlp ran cleanly but skipped (most commonly because of the
        # duration filter). Surface that as a clear 400.
        raise HTTPException(
            status_code=400,
            detail=f"Only videos under {MAX_DURATION_SECONDS // 60} minutes are allowed.",
        )
    source = candidates[0]
    duration = await _probe_duration_seconds_with_ytdlp(url, req_id)
    return youtube_download.DownloadResult(
        path=source,
        provider="youtube",
        title=None,
        duration=duration,
        filesize=source.stat().st_size if source.exists() else None,
        external_provider=None,
        video_id=cache.extract_video_id(url),
        thumbnail=None,
        channel=None,
        duration_string=None,
        view_count=None,
        download_url=None,
        source_ext=source.suffix.lstrip(".") or None,
    )


YOUTUBE_DOWNLOADER = youtube_download.YouTubeDownloadService.from_env(
    ytdlp_metadata_fetcher=_fetch_metadata_with_ytdlp,
    ytdlp_downloader=_download_audio_with_ytdlp,
)


async def _apply_pitch_shift(
    src: Path, dst: Path, pitch_factor: float, req_id: str
) -> None:
    # rubberband filter shifts pitch while preserving tempo, matching the
    # RubberBand-style WASM processor used by the DrakonRhym browser extension.
    # quality/transients/detector/phase tuned for offline rendering, where we
    # do not have the realtime-latency constraint the worklet operates under.
    filter_chain = (
        f"rubberband=pitch={pitch_factor:.6f}"
        ":pitchq=quality"
        ":transients=crisp"
        ":detector=compound"
        ":phase=laminar"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-af",
        filter_chain,
        "-vn",
        "-acodec",
        "libmp3lame",
        "-q:a",
        "2",
        str(dst),
    ]
    code, _, stderr = await _run_subprocess(cmd, FFMPEG_TIMEOUT, req_id, "ffmpeg")
    if code != 0:
        logger.error("[%s] ffmpeg failed: %s", req_id, stderr.decode(errors="replace"))
        raise HTTPException(status_code=500, detail="Failed to apply pitch shift.")


def _cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


app.mount(
    "/assets",
    StaticFiles(directory=BASE_DIR / "assets"),
    name="assets",
)


@app.get("/", include_in_schema=False)
async def home_page() -> HTMLResponse:
    html = (BASE_DIR / "UI" / "HomePage.html").read_text(encoding="utf-8")
    html = html.replace("{{DRAKON_GOOGLE_CLIENT_ID}}", GOOGLE_CLIENT_ID)
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/download", include_in_schema=False)
async def download_page() -> HTMLResponse:
    html = (BASE_DIR / "UI" / "DownloadPage.html").read_text(encoding="utf-8")
    # Use a unique placeholder that should never appear in real code so we
    # only touch the meta tag, not random string literals in the JS below.
    html = html.replace("{{DRAKON_GOOGLE_CLIENT_ID}}", GOOGLE_CLIENT_ID)
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/metadata")
async def metadata(
    url: str = Query(..., description="YouTube URL"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    user_sub, _user_row = await _auth_and_upsert(authorization)
    # Burst cap: signed-in users can't spam yt-dlp subprocesses by hammering
    # this endpoint. Generous enough that a normal page load (one call per
    # download) is never affected.
    if user_sub is not None:
        allowed, _ = await _metadata_rate.consume(user_sub)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many metadata requests. Please slow down.",
            )

    if not _is_valid_youtube_url(url):
        raise HTTPException(
            status_code=400,
            detail="Invalid URL — only YouTube domains are accepted.",
        )

    try:
        data = await YOUTUBE_DOWNLOADER.fetch_metadata(url)
    except youtube_download.ProviderDownloadError as e:
        logger.error("metadata fetch failed: %s", e.message)
        raise HTTPException(status_code=400, detail=_download_error_detail(e)) from e

    return {
        "title": data.title,
        "channel": data.channel,
        "duration": data.duration,
        "duration_string": data.duration_string,
        "thumbnail": data.thumbnail,
        "video_id": data.video_id,
        "view_count": data.view_count,
    }


def _download_error_detail(exc: youtube_download.ProviderDownloadError) -> str:
    kind = exc.error_kind
    if kind == youtube_download.ErrorKind.FATAL_USER:
        return "This video is unavailable or cannot be downloaded."
    if kind in {youtube_download.ErrorKind.CLOUDFLARE, youtube_download.ErrorKind.BOT_BLOCKED}:
        return "Temporary upstream block while fetching audio. Please try again shortly."
    if kind == youtube_download.ErrorKind.TIMEOUT:
        return "Download timed out while contacting upstream providers. Please try again."
    if kind == youtube_download.ErrorKind.UNAVAILABLE:
        return "Source audio is temporarily unavailable. Please try again later."
    return "Failed to download audio from the given URL."


@app.get("/api/download")
async def download(
    url: str = Query(..., description="YouTube URL to process"),
    pitch: float = Query(
        0.0,
        ge=-6.0,
        le=6.0,
        description="Pitch shift in semitones (range -6.0..6.0, step 0.1)",
    ),
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    user_sub, user_row = await _auth_and_upsert(authorization)

    # Validate URL BEFORE consuming quota — a bad URL is the caller's fault
    # but shouldn't burn one of their daily downloads.
    if not _is_valid_youtube_url(url):
        raise HTTPException(
            status_code=400,
            detail="Invalid URL — only YouTube domains are accepted.",
        )

    # Pre-flight duration probe so an over-long video gets rejected with a
    # clear 400 BEFORE we consume quota and BEFORE we pay for a full
    # download. yt-dlp also enforces this via --match-filter inside
    # _download_audio (defence in depth), but doing it up front avoids a
    # quota debit on too-long videos.
    try:
        metadata = await YOUTUBE_DOWNLOADER.fetch_metadata(url)
    except youtube_download.ProviderDownloadError as e:
        logger.error("metadata fetch failed before download: %s", e.message)
        raise HTTPException(status_code=400, detail="Could not read video metadata.") from e
    duration = metadata.duration
    if duration is not None and duration > MAX_DURATION_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"Only videos under {MAX_DURATION_SECONDS // 60} minutes are allowed.",
        )

    # Quota source of truth:
    #   - Supabase RPC `consume_quota` when DB is configured (per-user limit
    #     in the `users` table, persisted across restarts, supports overrides)
    #   - In-memory _download_rate otherwise (DEV / single-process fallback)
    # Track whether we actually incremented a counter. Only refund when this
    # is True — refunding on a denied (429) consume would credit the user.
    quota_consumed = False
    used_db_quota = False
    remaining: Optional[int] = None
    if user_sub is not None:
        if user_row is not None:
            result = await asyncio.to_thread(db.consume_quota, user_row["id"])
            if result is None:
                # RPC failed despite Supabase being configured — fall back to
                # in-memory limiter so we still meter the request.
                allowed, remaining = await _download_rate.consume(user_sub)
                if not allowed:
                    raise HTTPException(
                        status_code=429,
                        detail=f"Daily download limit reached ({RATE_LIMIT_PER_DAY}). Try again tomorrow.",
                    )
                quota_consumed = True
            else:
                if not result.get("allowed"):
                    raise HTTPException(
                        status_code=429,
                        detail=(
                            f"Daily download limit reached "
                            f"({result.get('limit', '?')}). Try again tomorrow."
                        ),
                    )
                used_db_quota = True
                quota_consumed = True
                remaining = max(0, int(result.get("limit", 0)) - int(result.get("used", 0)))
        else:
            allowed, remaining = await _download_rate.consume(user_sub)
            if not allowed:
                raise HTTPException(
                    status_code=429,
                    detail=f"Daily download limit reached ({RATE_LIMIT_PER_DAY}). Try again tomorrow.",
                )
            quota_consumed = True

    # Match the extension's slider: clamp to step 0.1 using half-away-from-zero
    # rounding (1.25 -> 1.3, not 1.2 as Python's banker's-rounding `round` gives).
    pitch = float(Decimal(str(pitch)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    pitch_factor = 2 ** (pitch / 12)
    semitones, cents = db.split_pitch_to_semitones_cents(pitch)

    async def _refund_quota_if_consumed() -> None:
        """Roll back the quota slot we just consumed, if any.

        Guarded by `quota_consumed` so we don't refund on a denied (429)
        request — that would credit the user a slot they never used.
        """
        if user_sub is None or not quota_consumed:
            return
        if used_db_quota and user_row is not None:
            await asyncio.to_thread(db.refund_quota, user_row["id"])
        else:
            await _download_rate.refund(user_sub)

    async def _record_failed() -> None:
        if user_row is not None:
            await asyncio.to_thread(
                db.record_download,
                user_id=user_row["id"],
                youtube_url=url,
                semitones=semitones,
                cents=cents,
                status="failed",
            )

    req_id = uuid.uuid4().hex[:8]
    request_started = time.monotonic()
    video_id = cache.extract_video_id(url)
    filename = f"drakonrhym_{pitch:+.1f}st.mp3"

    def _success_headers(
        *,
        source_provider: Optional[str] = None,
        download_strategy: Optional[str] = None,
    ) -> dict[str, str]:
        h = {
            "X-Pitch-Applied": f"{pitch:.1f}",
            "X-Processing-Time": f"{time.monotonic() - request_started:.1f}",
        }
        if source_provider:
            h["X-Source-Provider"] = source_provider
        if download_strategy:
            h["X-Download-Strategy"] = download_strategy
        if remaining is not None:
            h["X-Quota-Remaining"] = str(remaining)
        return h

    # ---- Cache hit path: skip yt-dlp + ffmpeg entirely ----
    if video_id is not None:
        cached = await cache.get(video_id, pitch)
        if cached is not None:
            logger.info("[%s] cache HIT video=%s pitch=%+.1f bytes=%d", req_id, video_id, pitch, len(cached))
            if user_row is not None:
                await asyncio.to_thread(
                    db.record_download,
                    user_id=user_row["id"],
                    youtube_url=url,
                    semitones=semitones,
                    cents=cents,
                    status="success",
                )
            headers = _success_headers(source_provider="cache", download_strategy="cache_hit")
            headers["Content-Disposition"] = f'attachment; filename="{filename}"'
            headers["X-Cache"] = "HIT"
            return Response(content=cached, media_type="audio/mpeg", headers=headers)

    workdir = Path(tempfile.mkdtemp(prefix="drakonrhym_"))
    delivered = False
    provider = "unknown"
    strategy = "unknown"
    download_seconds = 0.0
    pitch_seconds = 0.0
    try:
        async with _download_semaphore:
            logger.info("[%s] download url=%s pitch=%+.1f cache=%s",
                        req_id, url, pitch, "MISS" if video_id else "n/a")
            download_started = time.monotonic()
            try:
                source_result = await YOUTUBE_DOWNLOADER.download_audio(url, workdir, req_id)
            except youtube_download.ProviderDownloadError as e:
                logger.error("[%s] download failed: %s (%s)", req_id, e.message, e.error_kind.value)
                raise HTTPException(status_code=400, detail=_download_error_detail(e)) from e
            download_seconds = time.monotonic() - download_started
            source = source_result.path
            if source is None:
                raise HTTPException(status_code=400, detail="Failed to download audio from the given URL.")
            provider = source_result.external_provider or "yt-dlp"
            strategy = source_result.download_strategy or "unknown"
            logger.info(
                "[%s] source ready via provider=%s strategy=%s resolve=%.1fs materialize=%.1fs download=%.1fs ext=%s bytes=%s",
                req_id,
                provider,
                strategy,
                source_result.resolve_seconds or 0.0,
                source_result.materialize_seconds or 0.0,
                download_seconds,
                source.suffix.lstrip("."),
                source_result.filesize,
            )
            output = workdir / f"shifted_{req_id}.mp3"
            pitch_started = time.monotonic()
            await _apply_pitch_shift(source, output, pitch_factor, req_id)
            pitch_seconds = time.monotonic() - pitch_started
            logger.info("[%s] pitch shift finished in %.1fs", req_id, pitch_seconds)

        # Populate cache for the next caller. Read once so we can both stream
        # to the client and stash in Redis without re-reading the file.
        cache_write_seconds = 0.0
        if video_id is not None:
            try:
                cache_started = time.monotonic()
                blob = output.read_bytes()
                await cache.set(video_id, pitch, blob)
                cache_write_seconds = time.monotonic() - cache_started
            except Exception:
                logger.exception("[%s] cache populate failed", req_id)
        logger.info(
            "[%s] request complete provider=%s strategy=%s download=%.1fs pitch=%.1fs cache_write=%.1fs total=%.1fs",
            req_id,
            provider,
            strategy,
            download_seconds,
            pitch_seconds,
            cache_write_seconds,
            time.monotonic() - request_started,
        )

        # Success — persist a "success" download row. Counters were already
        # incremented atomically inside consume_quota.
        if user_row is not None:
            await asyncio.to_thread(
                db.record_download,
                user_id=user_row["id"],
                youtube_url=url,
                semitones=semitones,
                cents=cents,
                status="success",
            )

        headers = _success_headers(source_provider=provider, download_strategy=strategy)
        headers["X-Cache"] = "MISS"
        response = FileResponse(
            path=output,
            media_type="audio/mpeg",
            filename=filename,
            background=BackgroundTask(_cleanup, workdir),
            headers=headers,
        )
        delivered = True
        return response
    except HTTPException:
        await _refund_quota_if_consumed()
        await _record_failed()
        raise
    except asyncio.CancelledError:
        logger.info("[%s] request cancelled by client", req_id)
        await _refund_quota_if_consumed()
        await _record_failed()
        raise
    except Exception as e:
        logger.exception("[%s] unexpected error", req_id)
        await _refund_quota_if_consumed()
        await _record_failed()
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error (ref: {req_id}).",
        ) from e
    finally:
        if not delivered:
            _cleanup(workdir)


@app.get("/api/me")
async def me(authorization: Optional[str] = Header(None, alias="Authorization")):
    """Return the signed-in user's profile + current quota state.

    Requires a valid bearer token. If Supabase is not configured, returns just
    the claims from the Google token (no quota fields).
    """
    claims = _verify_google_id_token_claims(authorization)
    if claims is None:
        # DEV mode (auth disabled). Behave like an anonymous session.
        return {"signed_in": False}
    user_row = await asyncio.to_thread(
        db.upsert_user,
        claims["sub"],
        email=claims.get("email"),
        name=claims.get("name"),
        avatar_url=claims.get("picture"),
    )
    if user_row is None:
        return {
            "signed_in": True,
            "google_id": claims["sub"],
            "email": claims.get("email"),
            "name": claims.get("name"),
            "avatar_url": claims.get("picture"),
        }
    return {
        "signed_in": True,
        "id": user_row["id"],
        "google_id": user_row["google_id"],
        "email": user_row.get("email"),
        "name": user_row.get("name"),
        "avatar_url": user_row.get("avatar_url"),
        "download_count": user_row.get("download_count", 0),
        "daily_download_limit": user_row.get("daily_download_limit", 0),
        "daily_download_used": user_row.get("daily_download_used", 0),
        "last_download_date": user_row.get("last_download_date"),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)