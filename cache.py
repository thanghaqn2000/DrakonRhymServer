"""Redis-backed cache for pitch-shifted MP3 audio.

Key:    audio:{video_id}:{pitch_with_one_decimal}     e.g. audio:dQw4w9WgXcQ:+2.5
Value:  raw MP3 bytes
TTL:    DRAKON_CACHE_TTL_SECONDS (default 24h)

Disabled (every helper a no-op) when REDIS_URL is empty so DEV / local
development still works without running a Redis server.

Note: storing binary blobs in Redis means RAM grows with the cached set
(~5 MB per typical 3-min MP3 at 192 kbps). Configure Redis `maxmemory` and
`maxmemory-policy allkeys-lru` if you expect a long tail of videos. For
large-scale deployments, switch this module to "Redis-as-metadata, blobs
on disk" — same interface, different backing store.
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("drakonrhym.cache")

REDIS_URL = os.getenv("REDIS_URL", "").strip()
CACHE_TTL_SECONDS = int(os.getenv("DRAKON_CACHE_TTL_SECONDS", "86400"))

_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_client = None  # type: ignore[var-annotated]


def is_enabled() -> bool:
    return bool(REDIS_URL)


def _get_client():
    """Lazy-construct an async Redis client. Returns None when disabled."""
    global _client
    if not is_enabled():
        return None
    if _client is None:
        # Imported lazily so projects without Redis installed still import
        # this module cleanly (e.g. for tests).
        from redis.asyncio import Redis

        _client = Redis.from_url(REDIS_URL, decode_responses=False)
    return _client


def extract_video_id(url: str) -> str | None:
    """Pull the 11-char YouTube video ID out of a watch / youtu.be / music URL.

    Returns None for unknown URL shapes. Handles:
      https://www.youtube.com/watch?v=ID
      https://music.youtube.com/watch?v=ID
      https://m.youtube.com/watch?v=ID
      https://youtu.be/ID
      https://www.youtube.com/shorts/ID
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    host = (parsed.netloc or "").lower().removeprefix("www.").removeprefix("m.").removeprefix("music.")
    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/", 1)[0]
        return candidate if _YOUTUBE_ID_RE.match(candidate) else None
    if host in {"youtube.com"}:
        if parsed.path == "/watch":
            v = parse_qs(parsed.query).get("v", [None])[0]
            return v if v and _YOUTUBE_ID_RE.match(v) else None
        # /shorts/<id>, /embed/<id>, /v/<id>
        m = re.match(r"^/(?:shorts|embed|v)/([A-Za-z0-9_-]{11})", parsed.path)
        if m:
            return m.group(1)
    return None


def cache_key(video_id: str, pitch: float) -> str:
    return f"audio:{video_id}:{pitch:+.1f}"


async def get(video_id: str, pitch: float) -> bytes | None:
    """Return cached MP3 bytes for (video_id, pitch), or None on miss / disabled."""
    client = _get_client()
    if client is None:
        return None
    try:
        return await client.get(cache_key(video_id, pitch))
    except Exception:
        logger.exception("cache.get failed for %s pitch=%s", video_id, pitch)
        return None


async def set(video_id: str, pitch: float, data: bytes) -> None:
    """Store MP3 bytes with the configured TTL. No-op when disabled."""
    client = _get_client()
    if client is None:
        return
    try:
        await client.set(cache_key(video_id, pitch), data, ex=CACHE_TTL_SECONDS)
    except Exception:
        logger.exception("cache.set failed for %s pitch=%s", video_id, pitch)
