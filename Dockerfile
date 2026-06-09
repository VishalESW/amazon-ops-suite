FROM python:3.12-slim

WORKDIR /app

# Build deps (some wheels may need a compiler). Slim image keeps it small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY . .

# Runtime folders. /app/data is mounted as a persistent volume in Dokploy
# (it holds suite.db with encrypted account tokens — must survive redeploys).
RUN mkdir -p data uploads outputs archive

EXPOSE 8000

# app.py exposes a module-level `app` object (app = create_app()).
# IMPORTANT: 1 worker only. The background job store (utils/jobs.py) is an
# in-process dict; multiple workers would not share jobs ("unknown job" on
# status polls). Concurrency is handled by threads within the single worker,
# plus the daemon threads the job runner spawns.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "300", "app:app"]
