from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

import cache


logger = logging.getLogger("drakonrhym.youtube_external")

VIDEO_DOWNLOAD_API_URL = "https://p.savenow.to/api/v2/download"
TUNELIO_BASE_URL = "https://tunelio.dev"
CAPTAPI_BASE_URL = "https://api.captapi.com/v1"
DEFAULT_HTTP_TIMEOUT = 30
PROGRESS_POLL_INTERVAL_SECONDS = 2
PROGRESS_TIMEOUT_SECONDS = 90
STAGE_RACE_DELAY_SECONDS = float(os.getenv("DRAKON_STAGE_RACE_DELAY_SECONDS", "3"))
STAGE_RACE_FAMILY_COUNT = max(1, int(os.getenv("DRAKON_STAGE_RACE_FAMILY_COUNT", "2")))


@dataclass
class DownloadResult:
    """Result of a YouTube audio download attempt.

    Attributes:
        path: Local path to the downloaded audio file, or None if not yet materialized.
        provider: Always "youtube" for this service.
        title: Video title, if available.
        duration: Video duration in seconds.
        filesize: File size in bytes.
        external_provider: Name of the external provider that served the download, e.g. "video-download-api".
        video_id: YouTube video ID (11 characters).
        thumbnail: URL to video thumbnail.
        channel: Channel/video uploader name.
        duration_string: Human-readable duration, e.g. "5:30".
        view_count: Number of views.
        download_url: Resolved download URL from the provider.
        source_ext: File extension of the downloaded source, e.g. "mp3".
        download_strategy: Strategy label for logging/metrics, e.g. "scored", "staged_race", "cache_hint".
        resolve_seconds: Time spent resolving the download URL (provider API call).
        materialize_seconds: Time spent downloading the file from the resolved URL.
    """
    path: Path | None
    provider: str
    title: str | None
    duration: int | None
    filesize: int | None
    external_provider: str | None
    video_id: str | None
    thumbnail: str | None
    channel: str | None
    duration_string: str | None
    view_count: int | None
    download_url: str | None
    source_ext: str | None
    download_strategy: str | None = None
    resolve_seconds: float | None = None
    materialize_seconds: float | None = None


class ErrorKind(str, Enum):
    CREDITS = "credits"
    CLOUDFLARE = "cloudflare"
    FORBIDDEN = "forbidden"
    TIMEOUT = "timeout"
    BOT_BLOCKED = "bot_blocked"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    FATAL_USER = "fatal_user"
    OTHER = "other"


_COOLDOWN_SECONDS: dict[ErrorKind, int] = {
    ErrorKind.CLOUDFLARE: 120,
    ErrorKind.FORBIDDEN: 60,
    ErrorKind.TIMEOUT: 30,
    ErrorKind.BOT_BLOCKED: 180,
    ErrorKind.EMPTY: 45,
    ErrorKind.UNAVAILABLE: 90,
}


@dataclass
class AttemptSpec:
    provider_name: str
    api_key: str
    keys: list[str]
    mode: str
    family_index: int
    key_index: int


@dataclass
class ProviderAttemptState:
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    failure_streak: int = 0
    cooldown_until: float = 0.0
    avg_latency_ms: float = 0.0
    last_error_kind: ErrorKind | None = None
    success_count: int = 0


class ProviderHealthRegistry:
    """In-process health/scoring state per provider key and mode."""

    def __init__(self) -> None:
        self._states: dict[str, ProviderAttemptState] = {}

    def _key(self, provider_name: str, api_key: str, mode: str) -> str:
        return f"{mode}:{provider_name}:{api_key}"

    def get(self, provider_name: str, api_key: str, mode: str) -> ProviderAttemptState:
        return self._states.setdefault(self._key(provider_name, api_key, mode), ProviderAttemptState())

    def record_success(self, provider_name: str, api_key: str, mode: str, latency_s: float) -> None:
        state = self.get(provider_name, api_key, mode)
        now = time.monotonic()
        latency_ms = max(latency_s * 1000.0, 1.0)
        if state.success_count:
            state.avg_latency_ms = (state.avg_latency_ms * state.success_count + latency_ms) / (
                state.success_count + 1
            )
        else:
            state.avg_latency_ms = latency_ms
        state.success_count += 1
        state.last_success_at = now
        state.failure_streak = 0
        state.cooldown_until = 0.0
        state.last_error_kind = None

    def record_failure(
        self,
        provider_name: str,
        api_key: str,
        mode: str,
        error_kind: ErrorKind,
        *,
        latency_s: float,
    ) -> None:
        state = self.get(provider_name, api_key, mode)
        now = time.monotonic()
        state.last_failure_at = now
        state.failure_streak += 1
        state.last_error_kind = error_kind
        cooldown = _COOLDOWN_SECONDS.get(error_kind, 0)
        if error_kind == ErrorKind.CREDITS:
            cooldown = 0
        if cooldown:
            state.cooldown_until = now + cooldown

    def score(self, provider_name: str, api_key: str, mode: str, *, family_index: int, key_index: int) -> float:
        state = self.get(provider_name, api_key, mode)
        now = time.monotonic()
        score = 1000.0 - family_index * 10.0 - key_index
        if state.last_success_at:
            age = now - state.last_success_at
            if age < 300:
                score += 500.0 * (1.0 - age / 300.0)
        if state.cooldown_until > now:
            score -= 10_000.0 + (state.cooldown_until - now)
        score -= state.failure_streak * 75.0
        if state.avg_latency_ms:
            score -= min(state.avg_latency_ms / 100.0, 200.0)
        return score

    def in_cooldown(self, provider_name: str, api_key: str, mode: str) -> bool:
        return self.get(provider_name, api_key, mode).cooldown_until > time.monotonic()


_HEALTH = ProviderHealthRegistry()


class ProviderDownloadError(RuntimeError):
    def __init__(self, provider: str, message: str, *, error_kind: ErrorKind = ErrorKind.OTHER):
        super().__init__(message)
        self.provider = provider
        self.message = message
        self.error_kind = error_kind


class ProviderCreditExhausted(ProviderDownloadError):
    pass


def _env_keys(prefix: str, legacy_name: str | None = None) -> list[str]:
    keys: list[str] = []
    for idx in range(1, 5):
        value = os.getenv(f"{prefix}_{idx}", "").strip()
        if value:
            keys.append(value)
    if legacy_name:
        legacy = os.getenv(legacy_name, "").strip()
        if legacy and legacy not in keys:
            keys.append(legacy)
    return keys


def _first_env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _key_label(provider_name: str, api_key: str, keys: list[str]) -> str:
    try:
        idx = keys.index(api_key) + 1
    except ValueError:
        idx = 0
    suffix = api_key[-4:] if len(api_key) >= 4 else "****"
    if idx:
        return f"{provider_name} key {idx}/{len(keys)} (...{suffix})"
    return f"{provider_name} (...{suffix})"


def _key_fingerprint(api_key: str) -> str:
    return api_key[-4:] if len(api_key) >= 4 else api_key


def classify_error(exc: BaseException) -> ErrorKind:
    if isinstance(exc, ProviderCreditExhausted):
        return ErrorKind.CREDITS
    message = getattr(exc, "message", str(exc)).lower()
    if isinstance(exc, ProviderDownloadError) and exc.error_kind != ErrorKind.OTHER:
        return exc.error_kind
    if any(token in message for token in ("error code: 1010", "cloudflare", "cf-ray")):
        return ErrorKind.CLOUDFLARE
    if "403" in message or "forbidden" in message:
        return ErrorKind.FORBIDDEN
    if any(token in message for token in ("timed out", "timeout", "deadline")):
        return ErrorKind.TIMEOUT
    if _contains_bot_signal(message):
        return ErrorKind.BOT_BLOCKED
    if any(
        token in message
        for token in (
            "video unavailable",
            "private video",
            "has been removed",
            "this video is not available",
            "invalid url",
        )
    ):
        return ErrorKind.FATAL_USER
    if "empty" in message:
        return ErrorKind.EMPTY
    if any(token in message for token in ("unavailable", "not found", "404")):
        return ErrorKind.UNAVAILABLE
    return ErrorKind.OTHER


def _coerce_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _duration_string(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _nested_get(data: object, *paths: tuple[str, ...]) -> object | None:
    for path in paths:
        current = data
        found = True
        for key in path:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                found = False
                break
        if found and current not in (None, ""):
            return current
    return None


def _extract_video_id(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.netloc or "").lower().removeprefix("www.").removeprefix("m.").removeprefix("music.")
    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/", 1)[0]
        return candidate if re.match(r"^[A-Za-z0-9_-]{11}$", candidate) else None
    if host == "youtube.com":
        if parsed.path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [None])[0]
            return candidate if candidate and re.match(r"^[A-Za-z0-9_-]{11}$", candidate) else None
        match = re.match(r"^/(?:shorts|embed|v)/([A-Za-z0-9_-]{11})", parsed.path)
        if match:
            return match.group(1)
    return None


def _extension_from_url(url: str, fallback: str = "mp4") -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix.lower().lstrip(".")
    return suffix or fallback


def _contains_credit_signal(status_code: int | None, payload: object) -> bool:
    if status_code == 402:
        return True
    text = json.dumps(payload, ensure_ascii=False).lower() if payload is not None else ""
    for token in (
        "insufficient_credits",
        "payment_required",
        "out of credits",
        "not enough credits",
        "no credits",
    ):
        if token in text:
            return True
    return False


def _contains_bot_signal(message: str) -> bool:
    text = message.lower()
    return any(
        token in text
        for token in (
            "not a bot",
            "sign in to confirm",
            "confirm you're not a bot",
            "the page needs to be reloaded",
        )
    )


def _http_get_json(url: str, *, headers: dict[str, str] | None = None, timeout: int = DEFAULT_HTTP_TIMEOUT) -> tuple[int, dict]:
    request = Request(url, headers=headers or {})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw else {}
            return response.status, payload
    except Exception as e:
        status = getattr(e, "code", None)
        body = getattr(e, "read", None)
        if callable(body):
            raw = body().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {"error": raw}
            return int(status or 500), payload
        raise


def _download_to_file(url: str, destination: Path, *, timeout: int = DEFAULT_HTTP_TIMEOUT) -> int:
    """Download a URL to a file, only accepting http/https schemes."""
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported URL scheme: {scheme!r}. Only http and https are allowed.")
    request = Request(url)
    with urlopen(request, timeout=timeout) as response, destination.open("wb") as output:
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
            total += len(chunk)
    return total


def _rename_result(path: Path, ext: str | None) -> Path:
    target = path.with_name(f"{path.stem}.{(ext or path.suffix.lstrip('.') or 'mp4')}")
    if target == path:
        return path
    path.replace(target)
    return target


class YouTubeDownloadService:
    def __init__(
        self,
        *,
        provider_attempt: Callable[[str, str, str, str], Awaitable[DownloadResult]] | None = None,
        ytdlp_metadata_fetcher: Callable[[str], Awaitable[DownloadResult]] | None = None,
        ytdlp_downloader: Callable[[str, Path, str], Awaitable[DownloadResult]] | None = None,
        video_download_api_keys: list[str] | None = None,
        tunelio_api_key: str | None = None,
        captapi_api_keys: list[str] | None = None,
        health: ProviderHealthRegistry | None = None,
    ):
        self._provider_attempt = provider_attempt or self._provider_attempt_real
        self._ytdlp_metadata_fetcher = ytdlp_metadata_fetcher
        self._ytdlp_downloader = ytdlp_downloader
        self._health = health or _HEALTH
        self._providers = [
            ("video-download-api", (video_download_api_keys if video_download_api_keys else [])),
            ("tunelio", [tunelio_api_key] if tunelio_api_key else []),
            ("captapi", (captapi_api_keys if captapi_api_keys else [])),
        ]

    @classmethod
    def from_env(
        cls,
        *,
        ytdlp_metadata_fetcher: Callable[[str], Awaitable[DownloadResult]] | None = None,
        ytdlp_downloader: Callable[[str, Path, str], Awaitable[DownloadResult]] | None = None,
    ) -> "YouTubeDownloadService":
        video_download_api_keys = _env_keys("VIDEO_DOWNLOAD_API_KEY", "VIDEO_DOWNLOAD_API_KEY")
        tunelio_api_key = _first_env("TUNELIO_API_KEY")
        captapi_api_keys = _env_keys("CAPTAPI_API_KEY", "CAPTAPI_API_KEY")
        return cls(
            ytdlp_metadata_fetcher=ytdlp_metadata_fetcher,
            ytdlp_downloader=ytdlp_downloader,
            video_download_api_keys=video_download_api_keys if video_download_api_keys else None,
            tunelio_api_key=tunelio_api_key if tunelio_api_key else None,
            captapi_api_keys=captapi_api_keys if captapi_api_keys else None,
        )

    def is_external_enabled(self) -> bool:
        return any(key for _provider, keys in self._providers for key in keys)

    async def fetch_metadata(self, url: str) -> DownloadResult:
        async def _fallback() -> DownloadResult:
            if self._ytdlp_metadata_fetcher is None:
                raise ProviderDownloadError("yt-dlp", "Could not read video metadata.")
            return await self._ytdlp_metadata_fetcher(url)

        return await self._run_chain(url, "metadata", fallback=_fallback)

    def _schedule_attempts(self, mode: str) -> list[AttemptSpec]:
        specs: list[AttemptSpec] = []
        for family_index, (provider_name, keys) in enumerate(self._providers):
            active_keys = [key for key in keys if key]
            for key_index, api_key in enumerate(active_keys):
                specs.append(
                    AttemptSpec(
                        provider_name=provider_name,
                        api_key=api_key,
                        keys=active_keys,
                        mode=mode,
                        family_index=family_index,
                        key_index=key_index,
                    )
                )

        scored = sorted(
            specs,
            key=lambda spec: self._health.score(
                spec.provider_name,
                spec.api_key,
                spec.mode,
                family_index=spec.family_index,
                key_index=spec.key_index,
            ),
            reverse=True,
        )

        now = time.monotonic()
        cooled = [s for s in scored if not self._health.in_cooldown(s.provider_name, s.api_key, s.mode)]
        if cooled:
            return cooled
        return scored

    def _best_attempt_per_family(self, attempts: list[AttemptSpec]) -> list[AttemptSpec]:
        seen: set[str] = set()
        best: list[AttemptSpec] = []
        for spec in attempts:
            if spec.provider_name in seen:
                continue
            seen.add(spec.provider_name)
            best.append(spec)
        return best

    async def _record_source_hint(self, result: DownloadResult, api_key: str) -> None:
        if not result.video_id or not result.download_url:
            return
        await cache.set_source_hint(
            result.video_id,
            {
                "provider": result.external_provider,
                "key_fingerprint": _key_fingerprint(api_key),
                "download_url": result.download_url,
                "source_ext": result.source_ext,
                "title": result.title,
            },
        )

    async def _try_source_hint(
        self,
        video_id: str,
        workdir: Path,
        req_id: str,
    ) -> DownloadResult | None:
        hint = await cache.get_source_hint(video_id)
        if not hint or not hint.get("download_url"):
            return None

        provider_name = hint.get("provider")
        fingerprint = hint.get("key_fingerprint", "")
        api_key = ""
        for pname, keys in self._providers:
            if pname != provider_name:
                continue
            for key in keys:
                if key and _key_fingerprint(key) == fingerprint:
                    api_key = key
                    break
            if not api_key and keys:
                api_key = next((k for k in keys if k), "")
            break

        label = f"cache_hint:{provider_name or 'unknown'}"
        attempt_started = time.monotonic()
        logger.info("[%s] trying %s for download", req_id, label)
        result = DownloadResult(
            path=None,
            provider="youtube",
            title=hint.get("title"),
            duration=None,
            filesize=None,
            external_provider=provider_name,
            video_id=video_id,
            thumbnail=None,
            channel=None,
            duration_string=None,
            view_count=None,
            download_url=hint.get("download_url"),
            source_ext=hint.get("source_ext") or "mp3",
            download_strategy="cache_hint",
        )
        try:
            materialize_started = time.monotonic()
            result = await self.materialize_download(result, workdir)
            result.materialize_seconds = time.monotonic() - materialize_started
            result.resolve_seconds = 0.0
            latency = time.monotonic() - attempt_started
            if provider_name and api_key:
                self._health.record_success(provider_name, api_key, "download", latency)
            logger.info(
                "[%s] download succeeded via %s in %.1fs (filesize=%s)",
                req_id,
                label,
                latency,
                result.filesize,
            )
            return result
        except Exception as e:
            error_kind = classify_error(e)
            if provider_name and api_key:
                self._health.record_failure(
                    provider_name,
                    api_key,
                    "download",
                    error_kind,
                    latency_s=time.monotonic() - attempt_started,
                )
            logger.warning("[%s] %s failed after %.1fs: %s", req_id, label, time.monotonic() - attempt_started, e)
            await cache.clear_source_hint(video_id)
            return None

    async def _execute_metadata_attempt_safe(
        self, spec: AttemptSpec, url: str, errors: list[str]
    ) -> DownloadResult | None:
        label = _key_label(spec.provider_name, spec.api_key, spec.keys)
        attempt_started = time.monotonic()
        logger.info("trying %s for %s", label, spec.mode)
        try:
            result = await self._provider_attempt(spec.provider_name, spec.api_key, url, spec.mode)
            latency = time.monotonic() - attempt_started
            self._health.record_success(spec.provider_name, spec.api_key, spec.mode, latency)
            logger.info("%s succeeded via %s in %.1fs", spec.mode.capitalize(), label, latency)
            return result
        except ProviderCreditExhausted as e:
            error_kind = ErrorKind.CREDITS
            self._health.record_failure(
                spec.provider_name, spec.api_key, spec.mode, error_kind, latency_s=time.monotonic() - attempt_started
            )
            logger.warning("%s exhausted for %s after %.1fs: %s", label, spec.mode, time.monotonic() - attempt_started, e.message)
            errors.append(f"{spec.provider_name}:credits")
            return None
        except Exception as e:
            error_kind = classify_error(e)
            self._health.record_failure(
                spec.provider_name, spec.api_key, spec.mode, error_kind, latency_s=time.monotonic() - attempt_started
            )
            if error_kind == ErrorKind.FATAL_USER:
                raise ProviderDownloadError(spec.provider_name, getattr(e, "message", str(e)), error_kind=error_kind) from e
            logger.warning("%s failed for %s after %.1fs: %s", label, spec.mode, time.monotonic() - attempt_started, e)
            errors.append(f"{spec.provider_name}:{getattr(e, 'message', e)}")
            return None

    async def _execute_download_attempt(
        self,
        spec: AttemptSpec,
        url: str,
        workdir: Path,
        req_id: str,
        *,
        strategy: str,
        errors: list[str],
    ) -> DownloadResult | None:
        label = _key_label(spec.provider_name, spec.api_key, spec.keys)
        attempt_started = time.monotonic()
        logger.info("[%s] trying %s for download (%s)", req_id, label, strategy)
        try:
            resolve_started = time.monotonic()
            result = await self._provider_attempt(spec.provider_name, spec.api_key, url, "download")
            resolve_seconds = time.monotonic() - resolve_started
            materialize_seconds = 0.0
            if result.path is None:
                logger.info("[%s] %s resolved download_url, fetching file", req_id, label)
                materialize_started = time.monotonic()
                result = await self.materialize_download(result, workdir)
                materialize_seconds = time.monotonic() - materialize_started
            latency = time.monotonic() - attempt_started
            self._health.record_success(spec.provider_name, spec.api_key, "download", latency)
            result.download_strategy = strategy
            result.resolve_seconds = resolve_seconds
            result.materialize_seconds = materialize_seconds
            await self._record_source_hint(result, spec.api_key)
            logger.info(
                "[%s] download succeeded via %s in %.1fs (strategy=%s, filesize=%s)",
                req_id,
                label,
                latency,
                strategy,
                result.filesize,
            )
            return result
        except ProviderCreditExhausted as e:
            self._health.record_failure(
                spec.provider_name, spec.api_key, "download", ErrorKind.CREDITS, latency_s=time.monotonic() - attempt_started
            )
            logger.warning("[%s] %s exhausted after %.1fs: %s", req_id, label, time.monotonic() - attempt_started, e.message)
            errors.append(f"{spec.provider_name}:credits")
            return None
        except Exception as e:
            error_kind = classify_error(e)
            self._health.record_failure(
                spec.provider_name, spec.api_key, "download", error_kind, latency_s=time.monotonic() - attempt_started
            )
            if error_kind == ErrorKind.FATAL_USER:
                raise ProviderDownloadError(spec.provider_name, getattr(e, "message", str(e)), error_kind=error_kind) from e
            logger.warning("[%s] %s failed after %.1fs: %s", req_id, label, time.monotonic() - attempt_started, e)
            errors.append(f"{spec.provider_name}:{getattr(e, 'message', e)}")
            return None

    async def download_audio(self, url: str, workdir: Path, req_id: str) -> DownloadResult:
        errors: list[str] = []
        chain_started = time.monotonic()
        video_id = _extract_video_id(url)

        if video_id:
            hinted = await self._try_source_hint(video_id, workdir, req_id)
            if hinted is not None:
                return hinted

        attempts = self._schedule_attempts("download")
        if not attempts:
            if self._ytdlp_downloader is None:
                raise ProviderDownloadError("yt-dlp", "Failed to download audio from the given URL.")
            logger.info("[%s] trying yt-dlp fallback for download", req_id)
            result = await self._ytdlp_downloader(url, workdir, req_id)
            result.download_strategy = "ytdlp_fallback"
            return result

        remaining = list(attempts)
        tried_keys: set[tuple[str, str]] = set()

        # Stage 1: best single attempt
        first = remaining.pop(0)
        tried_keys.add((first.provider_name, first.api_key))
        result = await self._execute_download_attempt(first, url, workdir, req_id, strategy="scored", errors=errors)
        if result is not None:
            result.resolve_seconds = result.resolve_seconds or 0.0
            logger.info("[%s] download chain finished in %.1fs", req_id, time.monotonic() - chain_started)
            return result

        # Stage 2: staged race across other provider families (one key each)
        first_family = first.provider_name
        family_leaders = [
            spec
            for spec in self._best_attempt_per_family(remaining)
            if spec.provider_name != first_family and (spec.provider_name, spec.api_key) not in tried_keys
        ][:STAGE_RACE_FAMILY_COUNT]

        if family_leaders:
            logger.info("[%s] staged race across %d provider families", req_id, len(family_leaders))

            async def _race_one(spec: AttemptSpec) -> DownloadResult | None:
                # Use a unique subdirectory so concurrent providers cannot
                # overwrite each other's output.
                race_dir = Path(tempfile.mkdtemp(prefix=f"drakonrhym_race_{spec.provider_name}_", dir=workdir))
                try:
                    return await self._execute_download_attempt(
                        spec, url, race_dir, req_id, strategy="staged_race", errors=errors
                    )
                finally:
                    shutil.rmtree(race_dir, ignore_errors=True)

            race_tasks = {asyncio.create_task(_race_one(spec)): spec for spec in family_leaders}
            for spec in family_leaders:
                tried_keys.add((spec.provider_name, spec.api_key))
            remaining = [
                s
                for s in remaining
                if (s.provider_name, s.api_key) not in {(spec.provider_name, spec.api_key) for spec in family_leaders}
            ]

            pending = set(race_tasks.keys())
            race_deadline = time.monotonic() + STAGE_RACE_DELAY_SECONDS
            winner: DownloadResult | None = None
            while pending and winner is None:
                timeout = max(0.0, race_deadline - time.monotonic())
                if timeout <= 0:
                    break
                done, pending = await asyncio.wait(
                    pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    break
                for task in done:
                    try:
                        raced = task.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        spec = race_tasks[task]
                        logger.warning("[%s] staged race task failed for %s: %s", req_id, spec.provider_name, e)
                        continue
                    if raced is not None:
                        winner = raced
                        break

            if pending:
                # Restore cancelled attempts back to remaining so stage 3 can retry them.
                for task in pending:
                    task.cancel()
                    spec = race_tasks.get(task)
                    if spec is not None:
                        tried_keys.discard((spec.provider_name, spec.api_key))
                        if spec not in remaining:
                            remaining.append(spec)
                await asyncio.gather(*pending, return_exceptions=True)

            if winner is not None:
                logger.info("[%s] download chain finished in %.1fs", req_id, time.monotonic() - chain_started)
                return winner

        # Stage 3: remaining attempts in scored order
        for spec in remaining:
            if (spec.provider_name, spec.api_key) in tried_keys:
                continue
            tried_keys.add((spec.provider_name, spec.api_key))
            result = await self._execute_download_attempt(spec, url, workdir, req_id, strategy="scored", errors=errors)
            if result is not None:
                logger.info("[%s] download chain finished in %.1fs", req_id, time.monotonic() - chain_started)
                return result

        logger.info(
            "[%s] external provider chain exhausted for download after %.1fs: %s",
            req_id,
            time.monotonic() - chain_started,
            "; ".join(errors) or "none",
        )
        if self._ytdlp_downloader is None:
            raise ProviderDownloadError("yt-dlp", "Failed to download audio from the given URL.")
        fallback_started = time.monotonic()
        logger.info("[%s] trying yt-dlp fallback for download", req_id)
        result = await self._ytdlp_downloader(url, workdir, req_id)
        result.download_strategy = "ytdlp_fallback"
        logger.info(
            "[%s] download succeeded via yt-dlp fallback in %.1fs (chain %.1fs)",
            req_id,
            time.monotonic() - fallback_started,
            time.monotonic() - chain_started,
        )
        return result

    async def _run_chain(
        self,
        url: str,
        mode: str,
        *,
        fallback: Callable[[], Awaitable[DownloadResult]],
    ) -> DownloadResult:
        errors: list[str] = []
        chain_started = time.monotonic()
        attempts = self._schedule_attempts(mode)
        for spec in attempts:
            result = await self._execute_metadata_attempt_safe(spec, url, errors)
            if result is not None:
                logger.info("%s chain finished in %.1fs", mode.capitalize(), time.monotonic() - chain_started)
                return result
        logger.info(
            "external provider chain exhausted for %s after %.1fs: %s",
            mode,
            time.monotonic() - chain_started,
            "; ".join(errors) or "none",
        )
        fallback_started = time.monotonic()
        logger.info("trying yt-dlp fallback for %s", mode)
        result = await fallback()
        logger.info(
            "%s succeeded via yt-dlp fallback in %.1fs (chain %.1fs)",
            mode.capitalize(),
            time.monotonic() - fallback_started,
            time.monotonic() - chain_started,
        )
        return result

    async def _provider_attempt_real(self, provider_name: str, api_key: str, url: str, mode: str) -> DownloadResult:
        if provider_name == "video-download-api":
            if mode == "metadata":
                return await self._video_download_api_metadata(api_key, url)
            return await self._video_download_api_download(api_key, url)
        if provider_name == "tunelio":
            if mode == "metadata":
                return await self._tunelio_metadata(api_key, url)
            return await self._tunelio_download(api_key, url)
        if provider_name == "captapi":
            if mode == "metadata":
                return await self._captapi_metadata(api_key, url)
            return await self._captapi_download(api_key, url)
        raise ProviderDownloadError(provider_name, "Unsupported provider.")

    async def _video_download_api_metadata(self, api_key: str, url: str) -> DownloadResult:
        payload = await asyncio.to_thread(self._video_download_api_resolve, api_key, url, "mp3")
        return self._normalize_video_download_api(payload, url, mode="metadata")

    async def _video_download_api_download(self, api_key: str, url: str) -> DownloadResult:
        payload = await asyncio.to_thread(self._video_download_api_resolve, api_key, url, "mp3")
        return self._normalize_video_download_api(payload, url, mode="download")

    def _video_download_api_resolve(self, api_key: str, url: str, format_name: str) -> dict:
        query = urlencode(
            {
                "format": format_name,
                "url": url,
                "apikey": api_key,
                "worker_prepare": "1",
            }
        )
        status, payload = _http_get_json(f"{VIDEO_DOWNLOAD_API_URL}?{query}", timeout=DEFAULT_HTTP_TIMEOUT)
        if _contains_credit_signal(status, payload):
            raise ProviderCreditExhausted("video-download-api", "out of credits")
        if status >= 400:
            raise ProviderDownloadError("video-download-api", str(payload))

        ready_url = _nested_get(payload, ("url",), ("download_url",), ("data", "url"), ("data", "download_url"))
        if isinstance(ready_url, str) and ready_url:
            payload["resolved_url"] = ready_url
            return payload

        progress_url = _nested_get(payload, ("progress_url",), ("data", "progress_url"))
        if isinstance(progress_url, str) and progress_url:
            return self._poll_progress(progress_url, provider_name="video-download-api")

        raise ProviderDownloadError("video-download-api", "Missing download URL.")

    def _poll_progress(self, progress_url: str, *, provider_name: str) -> dict:
        deadline = time.monotonic() + PROGRESS_TIMEOUT_SECONDS
        for _ in range(int(PROGRESS_TIMEOUT_SECONDS / PROGRESS_POLL_INTERVAL_SECONDS) + 1):
            status, payload = _http_get_json(progress_url, timeout=DEFAULT_HTTP_TIMEOUT)
            if _contains_credit_signal(status, payload):
                raise ProviderCreditExhausted(provider_name, "out of credits")
            ready_url = _nested_get(payload, ("url",), ("download_url",), ("data", "url"), ("data", "download_url"))
            if isinstance(ready_url, str) and ready_url:
                payload["resolved_url"] = ready_url
                return payload
            progress = _coerce_int(_nested_get(payload, ("progress",), ("data", "progress")))
            if progress is not None and progress >= 1000:
                break
            time.sleep(PROGRESS_POLL_INTERVAL_SECONDS)
            if time.monotonic() > deadline:
                break
        raise ProviderDownloadError(provider_name, "Timed out waiting for download URL.")

    def _normalize_video_download_api(self, payload: dict, source_url: str, *, mode: str) -> DownloadResult:
        download_url = _nested_get(payload, ("resolved_url",), ("url",), ("download_url",), ("data", "url"))
        title = _nested_get(payload, ("title",), ("data", "title"), ("filename",), ("data", "filename"))
        duration = _coerce_int(_nested_get(payload, ("duration",), ("data", "duration")))
        thumbnail = _nested_get(payload, ("thumbnail",), ("data", "thumbnail"))
        if mode == "metadata" and not any([title, duration, thumbnail, download_url]):
            raise ProviderDownloadError("video-download-api", "Metadata response was empty.")
        return DownloadResult(
            path=None,
            provider="youtube",
            title=str(title) if title is not None else None,
            duration=duration,
            filesize=_coerce_int(_nested_get(payload, ("filesize",), ("size",), ("data", "filesize"), ("data", "size"))),
            external_provider="video-download-api",
            video_id=_extract_video_id(source_url),
            thumbnail=str(thumbnail) if thumbnail is not None else None,
            channel=None,
            duration_string=_duration_string(duration),
            view_count=None,
            download_url=str(download_url) if isinstance(download_url, str) and download_url else None,
            source_ext=_extension_from_url(str(download_url), fallback="mp3") if download_url else "mp3",
        )

    async def _tunelio_metadata(self, api_key: str, url: str) -> DownloadResult:
        def _fetch() -> dict:
            query = urlencode({"url": url})
            status, payload = _http_get_json(
                f"{TUNELIO_BASE_URL}/info?{query}",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=DEFAULT_HTTP_TIMEOUT,
            )
            if _contains_credit_signal(status, payload):
                raise ProviderCreditExhausted("tunelio", "out of credits")
            if status >= 400:
                raise ProviderDownloadError("tunelio", str(payload))
            return payload

        payload = await asyncio.to_thread(_fetch)
        return self._normalize_tunelio_metadata(payload, url)

    async def _tunelio_download(self, api_key: str, url: str) -> DownloadResult:
        def _fetch() -> dict:
            query = urlencode({"url": url, "quality": "mp3"})
            status, payload = _http_get_json(
                f"{TUNELIO_BASE_URL}/create?{query}",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=DEFAULT_HTTP_TIMEOUT,
            )
            if _contains_credit_signal(status, payload):
                raise ProviderCreditExhausted("tunelio", "out of credits")
            if status >= 400:
                raise ProviderDownloadError("tunelio", str(payload))
            return payload

        payload = await asyncio.to_thread(_fetch)
        if str(payload.get("status", "")).lower() not in {"", "ok", "success"} and not payload.get("url"):
            raise ProviderDownloadError("tunelio", str(payload.get("status") or payload))
        return DownloadResult(
            path=None,
            provider="youtube",
            title=payload.get("title"),
            duration=_coerce_int(payload.get("duration")),
            filesize=_coerce_int(payload.get("file_size")),
            external_provider="tunelio",
            video_id=_extract_video_id(url),
            thumbnail=payload.get("thumbnail"),
            channel=payload.get("author") or payload.get("channel"),
            duration_string=_duration_string(_coerce_int(payload.get("duration"))),
            view_count=_coerce_int(payload.get("view_count")),
            download_url=payload.get("url"),
            source_ext="mp3",
        )

    def _normalize_tunelio_metadata(self, payload: dict, source_url: str) -> DownloadResult:
        duration = _coerce_int(payload.get("duration"))
        return DownloadResult(
            path=None,
            provider="youtube",
            title=payload.get("title"),
            duration=duration,
            filesize=None,
            external_provider="tunelio",
            video_id=_extract_video_id(source_url),
            thumbnail=payload.get("thumbnail"),
            channel=payload.get("author") or payload.get("channel"),
            duration_string=payload.get("duration_string") or _duration_string(duration),
            view_count=_coerce_int(payload.get("view_count")),
            download_url=None,
            source_ext=None,
        )

    async def _captapi_metadata(self, api_key: str, url: str) -> DownloadResult:
        def _fetch() -> dict:
            query = urlencode({"url": url})
            status, payload = _http_get_json(
                f"{CAPTAPI_BASE_URL}/youtube/video-details?{query}",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=DEFAULT_HTTP_TIMEOUT,
            )
            if _contains_credit_signal(status, payload):
                raise ProviderCreditExhausted("captapi", "out of credits")
            if status >= 400 or payload.get("success") is False:
                raise ProviderDownloadError("captapi", str(payload))
            return payload

        payload = await asyncio.to_thread(_fetch)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        duration = _coerce_int(_nested_get(data, ("duration",), ("lengthSeconds",)))
        return DownloadResult(
            path=None,
            provider="youtube",
            title=_nested_get(data, ("title",)),
            duration=duration,
            filesize=None,
            external_provider="captapi",
            video_id=_extract_video_id(url) or _nested_get(data, ("videoId",), ("id",)),
            thumbnail=_nested_get(data, ("thumbnail",), ("thumbnailUrl",)),
            channel=_nested_get(data, ("channel",), ("author",), ("ownerChannelName",)),
            duration_string=_nested_get(data, ("durationString",)) or _duration_string(duration),
            view_count=_coerce_int(_nested_get(data, ("viewCount",), ("views",))),
            download_url=None,
            source_ext=None,
        )

    async def _captapi_download(self, api_key: str, url: str) -> DownloadResult:
        def _fetch() -> dict:
            query = urlencode({"url": url})
            status, payload = _http_get_json(
                f"{CAPTAPI_BASE_URL}/youtube/video-download?{query}",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=DEFAULT_HTTP_TIMEOUT,
            )
            if _contains_credit_signal(status, payload):
                raise ProviderCreditExhausted("captapi", "out of credits")
            if status >= 400 or payload.get("success") is False:
                raise ProviderDownloadError("captapi", str(payload))
            return payload

        payload = await asyncio.to_thread(_fetch)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        download_url = _nested_get(data, ("downloadUrl",), ("url",))
        if not download_url:
            raise ProviderDownloadError("captapi", "Missing download URL.")
        return DownloadResult(
            path=None,
            provider="youtube",
            title=_nested_get(data, ("title",)),
            duration=_coerce_int(_nested_get(data, ("duration",))),
            filesize=_coerce_int(_nested_get(data, ("sizeBytes",), ("filesize",))),
            external_provider="captapi",
            video_id=_extract_video_id(url) or _nested_get(data, ("videoId",), ("id",)),
            thumbnail=_nested_get(data, ("thumbnail",), ("thumbnailUrl",)),
            channel=_nested_get(data, ("channel",), ("author",)),
            duration_string=_duration_string(_coerce_int(_nested_get(data, ("duration",)))),
            view_count=_coerce_int(_nested_get(data, ("viewCount",), ("views",))),
            download_url=str(download_url),
            source_ext=_nested_get(data, ("format",)) or _extension_from_url(str(download_url), fallback="mp3"),
        )

    async def materialize_download(self, result: DownloadResult, workdir: Path, *, stem: str = "source") -> DownloadResult:
        if result.path is not None:
            return result
        if not result.download_url:
            raise ProviderDownloadError(result.external_provider or "unknown", "Missing resolved download URL.")

        ext = result.source_ext or "mp4"
        destination = workdir / f"{stem}.{ext}"

        def _download() -> int:
            try:
                return _download_to_file(result.download_url or "", destination, timeout=DEFAULT_HTTP_TIMEOUT)
            except Exception as e:
                message = str(e)
                error_kind = classify_error(e)
                if "403" in message.lower() or "forbidden" in message.lower():
                    error_kind = ErrorKind.FORBIDDEN
                raise ProviderDownloadError(
                    result.external_provider or "unknown",
                    message,
                    error_kind=error_kind,
                ) from e

        filesize = await asyncio.to_thread(_download)
        if filesize <= 0:
            destination.unlink(missing_ok=True)
            raise ProviderDownloadError(result.external_provider or "unknown", "Downloaded file was empty.")

        result.path = destination
        result.filesize = filesize
        return result

