FROM python:3.11-slim

ARG DENO_VERSION=2.7.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip \
    && arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
        amd64) deno_target="x86_64-unknown-linux-gnu" ;; \
        arm64) deno_target="aarch64-unknown-linux-gnu" ;; \
        *) echo "Unsupported architecture for Deno: $arch" >&2; exit 1 ;; \
    esac \
    && curl -fsSL "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${deno_target}.zip" -o /tmp/deno.zip \
    && unzip -q /tmp/deno.zip -d /usr/local/bin \
    && rm /tmp/deno.zip \
    && deno --version \
    && ffmpeg -hide_banner -filters 2>/dev/null | grep -q rubberband \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py db.py cache.py ./
COPY UI/ ./UI/
COPY assets/ ./assets/
COPY db/ ./db/

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
