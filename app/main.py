"""Mini Vault: a tiny file storage API that exercises the whole stack.

Request flows (the part worth understanding):

  POST /files            bytes -> MinIO, metadata row -> Postgres,
                         list cache invalidated -> Redis
  GET  /files            Redis cache hit? return it : read Postgres, cache it.
                         Live download counts are merged on from Redis, never
                         cached, so a 30s-old listing never shows a 30s-old count.
  GET  /files/{id}       row from Postgres + live download count from Redis
  GET  /files/{id}/download
                         lookup in Postgres, stream bytes from MinIO, count the
                         download once the bytes have actually gone out
  DELETE /files/{id}     object out of MinIO, row out of Postgres,
                         cache + counter cleared in Redis
  GET  /health           pings all three services
  GET  /metrics          cross-service dashboard data (the Angular UI reads this)

The Angular single-page app is served at / from the bundle baked into the image;
interactive API docs are auto-generated at /docs.
"""

import asyncio
import contextlib
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import cache, config, db, storage

log = logging.getLogger("mini_vault")

# The built Angular bundle. Docker copies it here in the image build; when you
# run uvicorn straight off a checkout it is absent and the dev server on :4200
# takes over instead.
WEB_DIR = Path(__file__).parent / "web"


def wait_for_services(retries: int = 30, delay: float = 1.0) -> None:
    """Block until Postgres, Redis and MinIO all answer.

    docker-compose healthchecks usually make this unnecessary, but it makes
    startup robust everywhere, including plain `uvicorn` on a laptop where
    the containers may still be warming up.
    """
    for name, probe in {"postgres": db.ping, "redis": cache.ping, "minio": storage.ping}.items():
        for attempt in range(1, retries + 1):
            try:
                probe()
                break
            except Exception:
                if attempt == retries:
                    raise RuntimeError(f"{name} never became reachable")
                time.sleep(delay)


# ── The download counter: Redis is live, Postgres is durable ─────────────


def seed_counters(ids) -> dict:
    """Return id -> live count, creating any counter Redis is missing.

    Seeding matters: a bare INCR against a Redis that lost its data would
    restart the counter at 1 and the UI would show a file with 57 downloads
    as having 1. Reading the durable value from Postgres first makes the
    counter survive a Redis restart.
    """
    counts = cache.get_downloads_many(ids)
    missing = [file_id for file_id, value in counts.items() if value is None]
    if missing:
        persisted = db.persisted_counts(missing)
        bases = {file_id: persisted.get(file_id, 0) for file_id in missing}
        cache.seed_downloads(bases)
        counts.update(bases)
    return {file_id: (value or 0) for file_id, value in counts.items()}


def flush_download_counts() -> int:
    """Mirror the counters Redis has moved into Postgres. Runs on a timer.

    This is the write-behind half of the pattern: downloads pay only for an
    in-memory INCR, and the durable write happens once per batch instead of
    once per download. Nothing is lost if it fails — the ids go back in the
    dirty set and the next tick retries them.
    """
    ids = cache.drain_dirty()
    if not ids:
        return 0
    totals = cache.get_downloads_many(ids)
    live = {file_id: value for file_id, value in totals.items() if value is not None}
    if not live:
        return 0
    try:
        return db.apply_download_counts(live)
    except Exception:
        cache.mark_dirty(list(live))
        raise


async def _counter_flusher() -> None:
    while True:
        await asyncio.sleep(config.COUNTER_FLUSH_SECONDS)
        try:
            # Blocking psycopg2/redis calls belong off the event loop.
            written = await asyncio.to_thread(flush_download_counts)
            if written:
                log.info("flushed %d download counter(s) to postgres", written)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("download counter flush failed; will retry")


@asynccontextmanager
async def lifespan(_: FastAPI):
    wait_for_services()
    db.init_db()            # create the files table if missing
    storage.ensure_bucket() # create the uploads bucket if missing

    flusher = asyncio.create_task(_counter_flusher())
    try:
        yield
    finally:
        flusher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await flusher
        # One last write-behind so a clean shutdown never strands a counter.
        try:
            await asyncio.to_thread(flush_download_counts)
        except Exception:
            log.exception("final download counter flush failed")


app = FastAPI(
    title="Mini Vault",
    version="2.0.0",
    description="A deliberately small file vault: FastAPI + PostgreSQL + MinIO + Redis.",
    lifespan=lifespan,
)

# Allow the Angular dev server (localhost:4200) to call this API. When the app
# is served from this same origin in Docker, CORS never comes into play.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    """Ping every backing service and report per-service status."""
    checks = {"postgres": db.ping, "redis": cache.ping, "minio": storage.ping}
    services = {}
    for name, probe in checks.items():
        try:
            probe()
            services[name] = "ok"
        except Exception as exc:
            services[name] = f"error: {exc.__class__.__name__}"
    healthy = all(v == "ok" for v in services.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "services": services},
    )


@app.get("/metrics")
def metrics():
    """Everything the dashboard draws, gathered from all four services.

    Postgres answers the aggregates (it is the only thing here that can GROUP BY),
    Redis answers the counters and its own runtime stats, MinIO reports what it
    actually holds. `files.count` and `minio.objects` are counted independently,
    so a drift between them is visible rather than hidden.
    """
    files = db.storage_stats()
    redis_stats = cache.stats()

    with db.SessionLocal() as session:
        rows = session.query(db.FileRecord.id, db.FileRecord.filename).all()
    names = {str(row[0]): row[1] for row in rows}
    counts = seed_counters(list(names))

    top = sorted(
        (
            {"id": file_id, "filename": names[file_id], "downloads": total}
            for file_id, total in counts.items()
        ),
        key=lambda item: item["downloads"],
        reverse=True,
    )[:5]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "downloads": {
            # Sum of the live per-file counters, so it matches the file rows.
            "total": sum(counts.values()),
            # Counters incremented since the last mirror into Postgres.
            "pending_flush": cache.pending_flush_count(),
            "flush_interval_seconds": config.COUNTER_FLUSH_SECONDS,
            "top": top,
        },
        "cache": redis_stats["cache"],
        # Lifetime totals; unlike downloads.total these survive a file deletion.
        "events": redis_stats["events"],
        "by_type": db.by_type(),
        "uploads_by_day": db.uploads_by_day(config.METRICS_HISTORY_DAYS),
        "services": {
            "postgres": db.server_info(),
            "redis": cache.server_info(),
            "minio": storage.bucket_stats(),
        },
    }


@app.post("/files", status_code=201)
async def upload_file(file: UploadFile = File(...)):
    """Upload: the one request where all three services do work."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    filename = file.filename or "unnamed"
    content_type = file.content_type or "application/octet-stream"
    # Prefix with a UUID so two uploads of "report.pdf" never collide.
    object_key = f"{uuid.uuid4()}/{filename}"

    # 1. Bytes into MinIO first: if this fails, nothing else happened yet.
    storage.put(object_key, data, content_type)

    # 2. Metadata row into Postgres.
    with db.SessionLocal() as session:
        record = db.FileRecord(
            filename=filename,
            content_type=content_type,
            size_bytes=len(data),
            object_key=object_key,
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        payload = record.to_dict()

    # 3. Drop the cached listing so the next GET /files sees this upload.
    cache.invalidate_files()
    cache.incr_uploads()
    payload["download_count"] = 0
    return payload


@app.get("/files")
def list_files():
    """List: the cache-aside pattern, with a `source` field so you can watch it.

    Only the metadata is cached. Download counts are merged on afterwards
    straight from Redis, which costs one MGET and keeps them exact even when
    the listing itself came out of a 30-second-old cache entry.
    """
    cached = cache.get_cached_files()
    if cached is not None:
        files, source = cached, "redis-cache"
    else:
        with db.SessionLocal() as session:
            rows = (
                session.query(db.FileRecord)
                .order_by(db.FileRecord.uploaded_at.desc())
                .all()
            )
            files = [row.to_dict() for row in rows]
        cache.set_cached_files(files)  # metadata only — see FileRecord.to_dict
        source = "postgres"

    counts = seed_counters([f["id"] for f in files])
    for entry in files:
        entry["download_count"] = counts.get(entry["id"], 0)

    return {"source": source, "count": len(files), "files": files}


@app.get("/files/{file_id}")
def file_details(file_id: uuid.UUID):
    """Metadata from Postgres, live download count from Redis."""
    with db.SessionLocal() as session:
        record = session.get(db.FileRecord, file_id)
        if record is None:
            raise HTTPException(status_code=404, detail="File not found")
        payload = record.to_dict()
    payload["download_count"] = seed_counters([file_id])[str(file_id)]
    return payload


@app.get("/files/{file_id}/download")
def download_file(file_id: uuid.UUID):
    """Look up the key in Postgres, stream the bytes out of MinIO."""
    with db.SessionLocal() as session:
        record = session.get(db.FileRecord, file_id)
        if record is None:
            raise HTTPException(status_code=404, detail="File not found")
        filename, content_type, object_key = (
            record.filename,
            record.content_type,
            record.object_key,
        )

    obj = storage.get_stream(object_key)

    def finish() -> None:
        """Runs once the response body has been fully sent.

        Counting here rather than before the stream means the number reflects
        downloads that actually completed, not ones that were merely started.
        """
        try:
            obj.close()
            obj.release_conn()
        finally:
            seed_counters([file_id])  # never let INCR restart a lost counter at 1
            cache.incr_downloads(file_id)

    return StreamingResponse(
        obj.stream(64 * 1024),
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        # Release the MinIO connection once the response finishes streaming.
        background=BackgroundTask(finish),
    )


@app.delete("/files/{file_id}", status_code=204)
def delete_file(file_id: uuid.UUID):
    with db.SessionLocal() as session:
        record = session.get(db.FileRecord, file_id)
        if record is None:
            raise HTTPException(status_code=404, detail="File not found")
        storage.remove(record.object_key)
        session.delete(record)
        session.commit()
    cache.invalidate_files()
    cache.clear_downloads(file_id)
    cache.incr_deletes()
    return None


# ── The frontend ─────────────────────────────────────────────────────────


class SpaStaticFiles(StaticFiles):
    """Serve the Angular bundle, falling back to index.html on a miss.

    Without the fallback a client-side route would 404 on a hard refresh,
    because only index.html exists on disk.
    """

    async def get_response(self, path: str, scope):
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            return await super().get_response("index.html", scope)
        if response.status_code == 404:
            return await super().get_response("index.html", scope)
        return response


if WEB_DIR.is_dir():
    # Mounted last on purpose: Starlette matches routes in registration order,
    # so every API route above wins before this catch-all sees the request.
    app.mount("/", SpaStaticFiles(directory=WEB_DIR, html=True), name="web")
else:

    @app.get("/", include_in_schema=False)
    def frontend_missing():
        return JSONResponse(
            status_code=503,
            content={
                "detail": "The Angular bundle is not present in this image.",
                "hint": "Run `docker compose up --build`, or serve the UI with "
                        "`cd frontend && npm install && npm start` on :4200.",
                "api_docs": "/docs",
            },
        )
