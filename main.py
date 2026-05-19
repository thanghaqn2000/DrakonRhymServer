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
from urllib.parse import urlparse

from dotenv import load_dotenv

# Load environment variables from a local .env file if present. Done before
# any os.getenv() call below so the rest of the module sees the values.
load_dotenv()

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from starlette.background import BackgroundTask

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
RATE_LIMIT_PER_DAY = _positive_int_env("DRAKON_RATE_LIMIT_PER_DAY", "20")
ALLOWED_ORIGINS = _origins_env("DRAKON_ALLOWED_ORIGINS", "*")
GOOGLE_CLIENT_ID = os.getenv("DRAKON_GOOGLE_CLIENT_ID", "").strip()
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


def _verify_google_id_token(authorization: str | None) -> str | None:
    """Verify a Bearer ID token from the Authorization header.

    Returns the user's stable Google `sub` claim. Raises 401 on any failure.
    No-op (returns None) when `GOOGLE_CLIENT_ID` is empty — DEV mode.
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
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Token missing subject claim.")
    return sub

app = FastAPI(title="DrakonRhymServer", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "X-Pitch-Applied", "X-Quota-Remaining"],
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


async def _download_audio(url: str, workdir: Path, req_id: str) -> Path:
    output_template = str(workdir / "source.%(ext)s")
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout",
        "30",
        "--retries",
        "2",
        "-f",
        "bestaudio/best",
        "-x",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "0",
        "-o",
        output_template,
        url,
    ]
    code, _, stderr = await _run_subprocess(cmd, YT_DLP_TIMEOUT, req_id, "yt-dlp")
    if code != 0:
        logger.error("[%s] yt-dlp failed: %s", req_id, stderr.decode(errors="replace"))
        raise HTTPException(status_code=400, detail="Failed to download audio from the given URL.")

    candidates = list(workdir.glob("source.*"))
    if not candidates:
        raise HTTPException(status_code=500, detail="Downloaded file not found.")
    return candidates[0]


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
    authorization: str | None = Header(None, alias="Authorization"),
):
    user_sub = _verify_google_id_token(authorization)
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

    req_id = uuid.uuid4().hex[:8]
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        "--socket-timeout",
        "20",
        "--no-playlist",
        url,
    ]
    code, stdout, stderr = await _run_subprocess(cmd, 60, req_id, "yt-dlp-metadata")
    if code != 0:
        logger.error("[%s] metadata fetch failed: %s", req_id, stderr.decode(errors="replace"))
        raise HTTPException(status_code=400, detail="Could not read video metadata.")

    try:
        data = json.loads(stdout.decode())
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to parse video metadata.")

    return {
        "title": data.get("title"),
        "channel": data.get("uploader") or data.get("channel"),
        "duration": data.get("duration"),
        "duration_string": data.get("duration_string"),
        "thumbnail": data.get("thumbnail"),
        "video_id": data.get("id"),
        "view_count": data.get("view_count"),
    }


@app.get("/api/download")
async def download(
    url: str = Query(..., description="YouTube URL to process"),
    pitch: float = Query(
        0.0,
        ge=-6.0,
        le=6.0,
        description="Pitch shift in semitones (range -6.0..6.0, step 0.1)",
    ),
    authorization: str | None = Header(None, alias="Authorization"),
):
    user_sub = _verify_google_id_token(authorization)

    # Validate URL BEFORE consuming quota — a bad URL is the caller's fault
    # but shouldn't burn one of their daily downloads.
    if not _is_valid_youtube_url(url):
        raise HTTPException(
            status_code=400,
            detail="Invalid URL — only YouTube domains are accepted.",
        )

    if user_sub is not None:
        allowed, remaining = await _download_rate.consume(user_sub)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=f"Daily download limit reached ({RATE_LIMIT_PER_DAY}). Try again tomorrow.",
            )
    else:
        remaining = None

    # Match the extension's slider: clamp to step 0.1 using half-away-from-zero
    # rounding (1.25 -> 1.3, not 1.2 as Python's banker's-rounding `round` gives).
    pitch = float(Decimal(str(pitch)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    pitch_factor = 2 ** (pitch / 12)

    req_id = uuid.uuid4().hex[:8]
    workdir = Path(tempfile.mkdtemp(prefix="drakonrhym_"))
    delivered = False
    try:
        async with _download_semaphore:
            logger.info("[%s] download url=%s pitch=%+.1f", req_id, url, pitch)
            source = await _download_audio(url, workdir, req_id)
            output = workdir / f"shifted_{req_id}.mp3"
            await _apply_pitch_shift(source, output, pitch_factor, req_id)

        filename = f"drakonrhym_{pitch:+.1f}st.mp3"
        headers = {"X-Pitch-Applied": f"{pitch:.1f}"}
        if remaining is not None:
            headers["X-Quota-Remaining"] = str(remaining)
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
        # The caller's quota was already consumed before we knew whether the
        # video was actually fetchable. Refund so failed downloads don't eat
        # their daily budget. Cancellations and unexpected 500s also refund.
        if user_sub is not None:
            await _download_rate.refund(user_sub)
        raise
    except asyncio.CancelledError:
        logger.info("[%s] request cancelled by client", req_id)
        if user_sub is not None:
            await _download_rate.refund(user_sub)
        raise
    except Exception as e:
        logger.exception("[%s] unexpected error", req_id)
        if user_sub is not None:
            await _download_rate.refund(user_sub)
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error (ref: {req_id}).",
        ) from e
    finally:
        if not delivered:
            _cleanup(workdir)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
