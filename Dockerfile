FROM python:3.11-slim

# System deps for psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent data volume (SQLite + DuckDB files, connections.json)
ENV DATA_DIR=/data
RUN mkdir -p /data

EXPOSE 8000

# PORT env var is set by Railway; fallback to 8000 locally
CMD uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
