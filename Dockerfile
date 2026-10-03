FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg ca-certificates curl tini gosu \
    && rm -rf /var/lib/apt/lists/*

# JavaScript runtime used by yt-dlp for YouTube extraction
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY LICENSE .
COPY app ./app
COPY migrations ./migrations
COPY docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    TZ=Europe/Berlin \
    DATA_DIR=/data \
    CONFIG_DIR=/config

VOLUME ["/config", "/data"]
EXPOSE 8048

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8048/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8048"]
