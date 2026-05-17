# DrakonRhymServer

A small FastAPI service that downloads audio from a YouTube URL, applies a pitch shift (semitones + cents), and returns the resulting MP3.

## Endpoints

### `GET /health`
Health check.

```json
{ "status": "ok" }
```

### `GET /download`
Download a YouTube audio track, pitch-shifted by the given amount.

| Query param | Type | Range       | Default | Description                                |
|-------------|------|-------------|---------|--------------------------------------------|
| `url`       | str  | required    | —       | YouTube URL                                |
| `semitones` | int  | `-24..24`   | `0`     | Pitch shift in semitones                   |
| `cents`     | int  | `-100..100` | `0`     | Additional pitch shift in cents            |

The pitch factor is computed as:

```
total_cents = semitones * 100 + cents
pitch_factor = 2 ** (total_cents / 1200)
```

The shift is applied via FFmpeg using `asetrate={sr*pitch_factor},aresample={sr}`. This shifts pitch while also changing duration (faster for upshifts, slower for downshifts) — a simple, dependency-free approach.

Example:

```
curl -L -o out.mp3 "http://localhost:8000/download?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ&semitones=2&cents=50"
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

## Notes

- CORS is currently wide open (`allow_origins=["*"]`) — lock this down before deploying publicly.
- Each request uses a unique temp directory, which is removed after the response is sent (via Starlette `BackgroundTask`).
- The pitch-shift method changes tempo together with pitch. If true pitch-only shifting is needed later, swap to `rubberband` filter (requires `librubberband` in the FFmpeg build).
