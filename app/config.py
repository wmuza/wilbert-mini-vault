"""Central configuration.

Every value can be overridden with an environment variable. That is how
docker-compose points the API at its sibling containers (hostnames like
"postgres", "redis", "minio" resolve on the compose network), while the
defaults below work for running everything directly on localhost.
"""

import os

from dotenv import load_dotenv

load_dotenv()  # picks up a local .env file when running outside Docker

# PostgreSQL: the system of record for file metadata
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg2://vault:vault@localhost:5432/vault"
)

# Redis: cache and counters
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "30"))

# How often the write-behind flusher mirrors Redis download counters into
# Postgres. Lower = smaller window of loss if Redis dies; higher = fewer writes.
COUNTER_FLUSH_SECONDS = int(os.getenv("COUNTER_FLUSH_SECONDS", "5"))

# How many days of upload history the /metrics chart covers.
METRICS_HISTORY_DAYS = int(os.getenv("METRICS_HISTORY_DAYS", "14"))

# MinIO: S3-compatible object storage for the file bytes themselves
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "uploads")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"
