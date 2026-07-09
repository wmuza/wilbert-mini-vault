"""PostgreSQL layer.

Postgres is the system of record: one row per uploaded file. Only metadata
lives here (name, size, type, where the bytes are). The bytes themselves go
to MinIO, which is what object storage is for. Keeping large blobs out of
the relational database keeps it fast and cheap to back up.

It also holds the durable copy of each file's `download_count`. Redis serves
and increments that number on the hot path; a background flusher mirrors it
here so the count survives a Redis restart. See `cache.py` for the two rules
that keep the pair consistent.

The aggregate queries at the bottom back the /metrics dashboard. They are the
part of the app where a relational database earns its keep: GROUP BY over a
generated date series is trivial here and awkward anywhere else in the stack.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, String, create_engine, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from . import config

engine = create_engine(config.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


class FileRecord(Base):
    __tablename__ = "files"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(255), default="application/octet-stream"
    )
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Where the bytes live inside the MinIO bucket
    object_key: Mapped[str] = mapped_column(String(600), unique=True, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    # Durable mirror of the Redis counter. Never read on the hot path.
    download_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )

    def to_dict(self) -> dict:
        """Metadata only.

        `download_count` is deliberately absent: this dict is what gets cached
        in Redis for 30 seconds, and a cached counter would be a stale counter.
        The live count is merged onto the response at read time instead.
        """
        return {
            "id": str(self.id),
            "filename": self.filename,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "object_key": self.object_key,
            "uploaded_at": self.uploaded_at.isoformat(),
        }


def init_db() -> None:
    """Create the table if it does not exist, then patch older databases.

    `create_all` only ever CREATEs; it will not ALTER a table that already
    exists, so a database created before `download_count` existed would be
    missing the column. The idempotent ALTER below covers that. This is
    exactly the gap Alembic fills in a real project.
    """
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE files "
                "ADD COLUMN IF NOT EXISTS download_count BIGINT NOT NULL DEFAULT 0"
            )
        )


def ping() -> bool:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return True


# ── Download counters: the durable side ──────────────────────────────────


def persisted_counts(ids) -> dict:
    """The durable count for each id, used to seed a cold Redis."""
    ids = [uuid.UUID(str(i)) for i in ids]
    if not ids:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, download_count FROM files WHERE id = ANY(:ids)"),
            {"ids": ids},
        ).all()
    return {str(row[0]): int(row[1]) for row in rows}


def apply_download_counts(counts: dict) -> int:
    """Mirror Redis' live counters into Postgres.

    GREATEST makes this monotonic: if Redis were ever restored from an older
    snapshot, the flush cannot drag the durable count backwards. It also makes
    the write idempotent, so a retried flush is harmless. Rows deleted in the
    meantime simply match nothing.
    """
    if not counts:
        return 0
    payload = [
        {"id": uuid.UUID(str(file_id)), "c": int(total)}
        for file_id, total in counts.items()
    ]
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE files SET download_count = GREATEST(download_count, :c) "
                "WHERE id = :id"
            ),
            payload,
        )
    return len(payload)


# ── Aggregates for /metrics ──────────────────────────────────────────────


def storage_stats() -> dict:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT COUNT(*)                                  AS count,
                       COALESCE(SUM(size_bytes), 0)              AS total_bytes,
                       COALESCE(AVG(size_bytes), 0)::bigint      AS avg_bytes,
                       COALESCE(MAX(size_bytes), 0)              AS largest_bytes
                FROM files
                """
            )
        ).mappings().one()
    return {k: int(v) for k, v in row.items()}


def by_type() -> list[dict]:
    """Files and bytes grouped into human-sized buckets, biggest first.

    Matching on split_part rather than LIKE keeps the SQL free of '%', which
    psycopg2 would otherwise treat as a parameter placeholder.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT kind,
                       COUNT(*)          AS count,
                       SUM(size_bytes)   AS bytes
                FROM (
                    SELECT CASE split_part(content_type, '/', 1)
                        WHEN 'image' THEN 'image'
                        WHEN 'video' THEN 'video'
                        WHEN 'audio' THEN 'audio'
                        ELSE CASE
                            WHEN content_type = 'application/pdf' THEN 'pdf'
                            WHEN content_type IN (
                                'application/zip', 'application/x-tar',
                                'application/gzip', 'application/x-7z-compressed'
                            ) THEN 'archive'
                            WHEN content_type IN (
                                'application/json', 'application/javascript',
                                'text/javascript', 'application/xml',
                                'text/xml', 'text/html', 'text/css'
                            ) THEN 'code'
                            WHEN split_part(content_type, '/', 1) = 'text' THEN 'text'
                            ELSE 'other'
                        END
                    END AS kind,
                    size_bytes
                    FROM files
                ) tagged
                GROUP BY kind
                ORDER BY bytes DESC
                """
            )
        ).mappings().all()
    return [
        {"kind": r["kind"], "count": int(r["count"]), "bytes": int(r["bytes"])}
        for r in rows
    ]


def uploads_by_day(days: int) -> list[dict]:
    """One row per day for the last `days` days, including the empty ones.

    generate_series supplies the calendar so days with no uploads still appear
    as zeroes; a plain GROUP BY would silently omit them and the chart would
    lie about the shape of the trend.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT d::date                        AS day,
                       COUNT(f.id)                    AS count,
                       COALESCE(SUM(f.size_bytes), 0) AS bytes
                FROM generate_series(
                        CURRENT_DATE - make_interval(days => :back),
                        CURRENT_DATE,
                        '1 day'
                     ) AS d
                LEFT JOIN files f
                       ON f.uploaded_at::date = d::date
                GROUP BY d
                ORDER BY d
                """
            ),
            {"back": days - 1},
        ).mappings().all()
    return [
        {"day": r["day"].isoformat(), "count": int(r["count"]), "bytes": int(r["bytes"])}
        for r in rows
    ]


def server_info() -> dict:
    with engine.connect() as conn:
        version = conn.execute(text("SHOW server_version")).scalar()
        size = conn.execute(
            text("SELECT pg_database_size(current_database())")
        ).scalar()
        rows = conn.execute(text("SELECT COUNT(*) FROM files")).scalar()
    return {
        "version": version,
        "database_size_bytes": int(size),
        "file_rows": int(rows),
    }
