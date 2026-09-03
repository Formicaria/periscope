# periscope v2 — one image, every service. Build from the repo root:  docker build -t periscope .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PERISCOPE_ROOT=/app

RUN apt-get update && apt-get install -y --no-install-recommends curl git && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 periscope

WORKDIR /app
COPY core ./core
COPY web ./web
COPY bots ./bots
RUN pip install ./core ./web && for b in bots/*/; do pip install "./$b"; done \
 && mkdir -p /app/config /app/data && chown -R periscope:periscope /app

USER periscope
VOLUME ["/app/config", "/app/data"]
EXPOSE 8080 8090
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 CMD curl -fsS http://127.0.0.1:8090/healthz || exit 1
CMD ["python", "-m", "periscope"]
