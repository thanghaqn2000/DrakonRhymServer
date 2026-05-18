import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import uuid
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
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
ALLOWED_ORIGINS = _origins_env("DRAKON_ALLOWED_ORIGINS", "*")
ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}

_download_semaphore = asyncio.Semaphore(MAX_CONCURRENT)

app = FastAPI(title="DrakonRhymServer", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "X-Pitch-Applied"],
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
    return proc.returncode or 0, stdout, stderr


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
async def home_page() -> FileResponse:
    return FileResponse(BASE_DIR / "UI" / "HomePage.html", media_type="text/html")


@app.get("/download", include_in_schema=False)
async def download_page() -> FileResponse:
    return FileResponse(BASE_DIR / "UI" / "DownloadPage.html", media_type="text/html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/metadata")
async def metadata(url: str = Query(..., description="YouTube URL")):
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
):
    if not _is_valid_youtube_url(url):
        raise HTTPException(
            status_code=400,
            detail="Invalid URL — only YouTube domains are accepted.",
        )

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
        response = FileResponse(
            path=output,
            media_type="audio/mpeg",
            filename=filename,
            background=BackgroundTask(_cleanup, workdir),
            headers={"X-Pitch-Applied": f"{pitch:.1f}"},
        )
        delivered = True
        return response
    except HTTPException:
        raise
    except asyncio.CancelledError:
        logger.info("[%s] request cancelled by client", req_id)
        raise
    except Exception as e:
        logger.exception("[%s] unexpected error", req_id)
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
