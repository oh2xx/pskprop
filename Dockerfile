# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm

# Pi4 works with this image on both 64-bit and (via buildx) 32-bit
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PSKPROP_CONFIG_DIR=/data

WORKDIR /app

# System deps (very small); curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install -r requirements.txt

# App code
COPY app.py ./app.py
COPY static/ ./static/

# Non-root user
RUN useradd -m appuser \
 && mkdir -p /data \
 && chown -R appuser:appuser /app /data

USER appuser

EXPOSE 8080

# Simple healthcheck
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s CMD curl -fsS http://localhost:8080/ || exit 1

CMD ["python","-m","uvicorn","app:app","--host","0.0.0.0","--port","8080"]
