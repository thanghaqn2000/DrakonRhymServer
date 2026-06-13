# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project

DrakonRhymServer is a single-file FastAPI service that downloads a YouTube audio track via `yt-dlp`, applies a pitch shift through FFmpeg, and streams the resulting MP3 back to the caller. All HTTP handling, subprocess orchestration, and cleanup live in `main.py` — there is no package layout, no DB, no auth.

## Common commands

Local development (requires Python 3.11+ and `ffmpeg` on `PATH`):

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
curl -L -o out.mp3 "http://localhost:8000/download?url=<youtube_url>&pitch=2.5"
curl http://localhost:8000/health
```

There is no test suite yet. If you add one, prefer `pytest` + `httpx.AsyncClient` against the FastAPI app object directly so subprocesses can be mocked.

## Architecture notes

- **Request lifecycle (`/download`)**: each request gets its own `tempfile.mkdtemp(prefix="drakonrhym_")` workdir and an 8-char `req_id` used for logging and the output filename. `yt-dlp` writes `source.<ext>` into the workdir, then `ffmpeg` writes `shifted_<req_id>.mp3`. The response is a `FileResponse` with a Starlette `BackgroundTask` that removes the workdir **after** the file is fully sent. On any other path — exception, client disconnect (`CancelledError`) — the `finally` block removes the workdir, guarded by a `delivered` flag so we never double-clean a directory the BackgroundTask owns.
- **Subprocess discipline**: all external commands go through `_run_subprocess`, which uses `asyncio.create_subprocess_exec` + `asyncio.wait_for` so the event loop is never blocked and runaway processes are killed. Two timeouts are tunable via env vars: `DRAKON_YT_DLP_TIMEOUT` (default 300s) and `DRAKON_FFMPEG_TIMEOUT` (default 600s). On timeout the subprocess is killed and HTTP 504 is returned. On `CancelledError` (client disconnect) the subprocess is killed and the cancel re-raised. Do not switch to `subprocess.run` or `os.system`; that would serialize requests and lose the cancel/timeout behaviour.
- **Concurrency limit**: `_download_semaphore` caps concurrent `/download` requests at `DRAKON_MAX_CONCURRENT` (default 2). yt-dlp + rubberband are CPU/disk heavy; without this an idle box becomes saturated by two or three long videos.
- **Pitch input**: a single `pitch` float in semitones, range `-6.0..6.0`, rounded server-side to step `0.1`. This mirrors the extension's slider (`popup-simple.js`: `min=-6, max=6, step=0.1`). The extension internally splits this into `pitchValueSemitones` (integer part) and `pitchValueCents` (fractional × 100) before passing to its WASM processor, but the math is equivalent — do not re-introduce two separate params on the backend; one float is the source of truth.
- **Pitch shift formula**: `pitch_factor = 2 ** (pitch / 12)`, applied as the FFmpeg filter `rubberband=pitch=…:pitchq=quality:transients=crisp:detector=compound:phase=laminar`. This preserves tempo while shifting pitch, matching the RubberBand-style WASM processor used by the extension (`extensions-ee-pitch-changer-s` in `smartProcessor.bundle.js`). The quality flags are tuned for offline rendering — the worklet uses faster realtime settings, so output is not bit-identical, but it is the closest librubberband can get without the latency constraint. Do not switch to `asetrate+aresample` — that changes tempo and produces audibly different output.
- **FFmpeg requirement**: the `rubberband` filter is mandatory. Debian's `ffmpeg` package (used in the Docker image) ships with `librubberband` linked in. If you change the base image, verify `ffmpeg -filters | grep rubberband` still succeeds.
- **URL validation**: only the YouTube domains in `ALLOWED_HOSTS` (`youtube.com`, `www.youtube.com`, `m.youtube.com`, `music.youtube.com`, `youtu.be`) are accepted; anything else is rejected with 400 before `yt-dlp` is invoked. This limits the bandwidth/server-time risk from arbitrary URLs.
- **Error mapping**: `_download_audio` → HTTP 400 (treated as caller-supplied bad URL); ffmpeg failures → HTTP 500; subprocess timeouts → HTTP 504. yt-dlp does not currently distinguish "video unavailable" (caller error) from "rate-limited / network down" (upstream error) — both become 400. If false-healthy behaviour becomes a problem, parse stderr for known phrases (`HTTP Error 429`, `Sign in to confirm`, `This video is unavailable`).
- **Pitch echo header**: the server snaps `pitch` to step 0.1 server-side and returns the applied value in the `X-Pitch-Applied` response header (also via `Content-Disposition` filename). Both headers are added to `expose_headers` so the browser extension can read them through CORS.
- **CORS**: wide open (`allow_origins=["*"]`) by design for early development. This must be tightened before any public deployment — do not assume the current setting is safe.

## Conventions

- Keep the service single-file unless a concrete second concern appears (e.g. a queue, persistence). Premature package layout would obscure the request flow.
- New endpoints should follow the same pattern: validate input → allocate temp workdir → run subprocesses via `_run_subprocess` → return `FileResponse` with a cleanup `BackgroundTask`.
- The `.gitignore` excludes `drakonrhym_*/` and stray audio files at the repo root; do not commit sample downloads.
