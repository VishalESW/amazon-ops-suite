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
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120", "app:app"]
