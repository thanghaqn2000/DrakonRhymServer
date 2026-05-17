# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

DrakonRhymServer is a single-file FastAPI service that downloads a YouTube audio track via `yt-dlp`, applies a pitch shift through FFmpeg, and streams the resulting MP3 back to the caller. All HTTP handling, subprocess orchestration, and cleanup live in `main.py` — there is no package layout, no DB, no auth.

## Common commands

Local development (requires Python 3.11+ and `ffmpeg` + `ffprobe` on `PATH`):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Docker:

```bash
docker build -t drakonrhym-server .
docker run --rm -p 8000:8000 drakonrhym-server
```

Smoke-test an endpoint:

```bash
curl -L -o out.mp3 "http://localhost:8000/download?url=<youtube_url>&semitones=2&cents=50"
curl http://localhost:8000/health
```

There is no test suite yet. If you add one, prefer `pytest` + `httpx.AsyncClient` against the FastAPI app object directly so subprocesses can be mocked.

## Architecture notes

- **Request lifecycle (`/download`)**: each request gets its own `tempfile.mkdtemp(prefix="drakonrhym_")` workdir. `yt-dlp` writes `source.<ext>` into it, then `ffmpeg` writes `shifted_<uuid>.mp3`. The response is a `FileResponse` with a Starlette `BackgroundTask` that removes the workdir **after** the file is fully sent. On any exception path the workdir is removed synchronously in the `finally`/`except` block — keep both paths in sync if you refactor.
- **Subprocess discipline**: all external commands go through `_run_subprocess`, which uses `asyncio.create_subprocess_exec` so the event loop is never blocked. Do not switch to `subprocess.run` or `os.system`; that would serialize all in-flight requests.
- **Pitch shift formula**: `pitch_factor = 2 ** ((semitones*100 + cents) / 1200)`, applied as the FFmpeg filter `rubberband=pitch={pitch_factor}`. This preserves tempo while shifting pitch, matching the RubberBand-style WASM processor used by the DrakonRhym browser extension (`extensions-ee-pitch-changer-s` in `smartProcessor.bundle.js`). Do not switch to `asetrate+aresample` — that changes tempo and produces a different output than the extension.
- **FFmpeg requirement**: the `rubberband` filter is mandatory. Debian's `ffmpeg` package (used in the Docker image) ships with `librubberband` linked in. If you change the base image, verify `ffmpeg -filters | grep rubberband` still succeeds.
- **Error mapping**: `_download_audio` → HTTP 400 (treated as caller-supplied bad URL); `ffprobe`/`ffmpeg` failures → HTTP 500. URL validation is intentionally loose (`http`/`https` + non-empty netloc) and defers the real check to `yt-dlp`.
- **CORS**: wide open (`allow_origins=["*"]`) by design for early development. This must be tightened before any public deployment — do not assume the current setting is safe.

## Conventions

- Keep the service single-file unless a concrete second concern appears (e.g. a queue, persistence). Premature package layout would obscure the request flow.
- New endpoints should follow the same pattern: validate input → allocate temp workdir → run subprocesses via `_run_subprocess` → return `FileResponse` with a cleanup `BackgroundTask`.
- The `.gitignore` excludes `drakonrhym_*/` and stray audio files at the repo root; do not commit sample downloads.
