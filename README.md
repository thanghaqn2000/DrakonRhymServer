# DrakonRhymServer

A small FastAPI service that downloads audio from a YouTube URL, applies a pitch shift, and returns the resulting MP3. The pitch shift is performed with FFmpeg's `rubberband` filter and is tuned to match the audio produced by the [DrakonRhym browser extension](https://github.com/thanghaqn2000/DrakonRhym).

## Endpoints

### `GET /health`
Health check.

```json
{ "status": "ok" }
```

### `GET /download`
Download a YouTube audio track, pitch-shifted by the given amount.

| Query param | Type  | Range       | Default | Description                                   |
|-------------|-------|-------------|---------|-----------------------------------------------|
| `url`       | str   | required    | —       | YouTube URL                                   |
| `pitch`     | float | `-6.0..6.0` | `0.0`   | Pitch shift in semitones (rounded to step 0.1)|

`pitch` mirrors the slider in the DrakonRhym browser extension: one value in semitones (with one decimal place — `0.1` ≈ 10 cents). The server rounds to step `0.1` and computes:

```
pitch_factor = 2 ** (pitch / 12)
```

The shift is applied via FFmpeg's `rubberband` filter, which preserves the original tempo while shifting pitch.

Only YouTube URLs are accepted (`youtube.com`, `www.youtube.com`, `m.youtube.com`, `music.youtube.com`, `youtu.be`); other domains return HTTP 400.

The applied pitch is echoed in the `X-Pitch-Applied` response header (browser-readable through CORS) so callers can confirm what value was used after server-side rounding.

Example:

```
curl -L -D - -o out.mp3 \
  "http://localhost:8000/download?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ&pitch=2.5"
```

## Run locally

Requires Python 3.11+ and `ffmpeg` available on `PATH`.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open the interactive docs at <http://localhost:8000/docs>.

## Run with Docker

```bash
docker build -t drakonrhym-server .
docker run --rm -p 8000:8000 drakonrhym-server
```

## Configuration

Environment variables (all optional):

| Variable                    | Default | Description                                  |
|-----------------------------|---------|----------------------------------------------|
| `PORT`                      | `8000`  | Bind port when running via `python main.py`  |
| `DRAKON_MAX_CONCURRENT`     | `2`     | Max concurrent `/download` requests          |
| `DRAKON_YT_DLP_TIMEOUT`     | `300`   | `yt-dlp` timeout (seconds) — returns 504     |
| `DRAKON_FFMPEG_TIMEOUT`     | `600`   | `ffmpeg` timeout (seconds) — returns 504     |

## Notes

- CORS is currently wide open (`allow_origins=["*"]`) — lock this down before deploying publicly.
- Each request uses a unique temp directory, which is removed after the response is sent (via Starlette `BackgroundTask`); on errors and client disconnects the directory is removed in a `finally` block.
- Requires an FFmpeg build with `librubberband` (Debian's `ffmpeg` package and Homebrew's `ffmpeg` both include it by default).
