import asyncio
import logging
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("drakonrhym")

app = FastAPI(title="DrakonRhymServer", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


async def _run_subprocess(cmd: list[str]) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout, stderr


async def _download_audio(url: str, workdir: Path) -> Path:
    output_template = str(workdir / "source.%(ext)s")
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--no-warnings",
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
    code, _, stderr = await _run_subprocess(cmd)
    if code != 0:
        logger.error("yt-dlp failed: %s", stderr.decode(errors="replace"))
        raise HTTPException(status_code=400, detail="Failed to download audio from the given URL.")

    candidates = list(workdir.glob("source.*"))
    if not candidates:
        raise HTTPException(status_code=500, detail="Downloaded file not found.")
    return candidates[0]


async def _apply_pitch_shift(src: Path, dst: Path, pitch_factor: float) -> None:
    # rubberband filter shifts pitch while preserving tempo, matching the
    # RubberBand-style WASM processor used by the DrakonRhym browser extension.
    filter_chain = f"rubberband=pitch={pitch_factor:.6f}"
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
    code, _, stderr = await _run_subprocess(cmd)
    if code != 0:
        logger.error("ffmpeg failed: %s", stderr.decode(errors="replace"))
        raise HTTPException(status_code=500, detail="Failed to apply pitch shift.")


def _cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/download")
async def download(
    url: str = Query(..., description="YouTube URL to process"),
    semitones: int = Query(0, ge=-24, le=24, description="Pitch shift in semitones"),
    cents: int = Query(0, ge=-100, le=100, description="Additional pitch shift in cents"),
):
    if not _is_valid_url(url):
        raise HTTPException(status_code=400, detail="Invalid URL.")

    total_cents = semitones * 100 + cents
    pitch_factor = 2 ** (total_cents / 1200)

    workdir = Path(tempfile.mkdtemp(prefix="drakonrhym_"))
    try:
        source = await _download_audio(url, workdir)
        output = workdir / f"shifted_{uuid.uuid4().hex}.mp3"
        await _apply_pitch_shift(source, output, pitch_factor)

        filename = f"drakonrhym_{semitones:+d}st_{cents:+d}c.mp3"
        return FileResponse(
            path=output,
            media_type="audio/mpeg",
            filename=filename,
            background=BackgroundTask(_cleanup, workdir),
        )
    except HTTPException:
        _cleanup(workdir)
        raise
    except Exception as e:
        _cleanup(workdir)
        logger.exception("Unexpected error during download.")
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
