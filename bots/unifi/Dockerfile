# Build from the repo root:  docker build -f bots/unifi/Dockerfile -t periscope-unifi .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 periscope \
 && mkdir -p /data && chown periscope:periscope /data

WORKDIR /app
COPY core ./core
RUN pip install ./core
COPY bots/unifi ./bot
RUN pip install ./bot

USER periscope
VOLUME ["/data"]

# GET /health on WEBHOOK_PORT returns 200 once connected to Discord.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${WEBHOOK_PORT:-8080}/health" || exit 1

CMD ["python", "-m", "periscope_unifi"]
