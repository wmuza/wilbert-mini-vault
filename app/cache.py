"""Redis layer.

Redis does three jobs here, which between them cover most of what Redis is
used for in real systems:

1. Cache (cache-aside pattern): GET /files checks Redis first. On a miss it
   reads Postgres, stores the JSON in Redis with a short TTL, and returns it.
   Any write (upload/delete) deletes the cached key so readers never see
   stale data for long. The TTL is a safety net in case an invalidation is
   ever missed.

2. Live download counters. Redis holds the *authoritative current* count and
   serves every read of it, because an atomic INCR in memory costs
   microseconds where a Postgres write transaction costs milliseconds.
   Postgres holds a durable mirror, updated by a write-behind flusher (see
   `flush_download_counts` in main.py). Two rules keep the two stores honest:

     - A counter key is *seeded* from Postgres before it is ever read or
       incremented, so a Redis that lost its data cannot restart a counter
       from zero.
     - The flush writes GREATEST(postgres, redis), so a stale Redis can never
       drag the durable count backwards.

   Every id whose counter moved is put in a `dirty` set; the flusher drains
   that set instead of rewriting every row.

3. Plain counters for observability: cache hits/misses (so the hit rate on
   the dashboard is measured, not guessed) and lifetime upload/download/delete
   totals.
"""

import json

import redis

from . import config

r = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)

FILES_KEY = "files:all"
DIRTY_KEY = "downloads:dirty"

STAT_CACHE_HIT = "stats:cache:hit"
STAT_CACHE_MISS = "stats:cache:miss"
STAT_UPLOADS = "stats:uploads"
STAT_DOWNLOADS = "stats:downloads"
STAT_DELETES = "stats:deletes"


def _dl_key(file_id) -> str:
    return f"downloads:{file_id}"


def _int(key: str) -> int:
    value = r.get(key)
    return int(value) if value else 0


# ── 1. The file-listing cache ────────────────────────────────────────────


def get_cached_files() -> list | None:
    """Read the cached listing, recording the hit or miss as we go."""
    raw = r.get(FILES_KEY)
    if raw:
        r.incr(STAT_CACHE_HIT)
        return json.loads(raw)
    r.incr(STAT_CACHE_MISS)
    return None


def set_cached_files(files: list) -> None:
    r.setex(FILES_KEY, config.CACHE_TTL_SECONDS, json.dumps(files))


def invalidate_files() -> None:
    r.delete(FILES_KEY)


def listing_ttl_remaining() -> int:
    """Seconds left on the cached listing; 0 when there is no cached listing.

    Redis answers -2 for a missing key and -1 for a key with no expiry.
    """
    return max(r.ttl(FILES_KEY), 0)


# ── 2. Download counters ─────────────────────────────────────────────────


def seed_downloads(bases: dict) -> None:
    """Create counter keys Redis does not have yet, from the Postgres values.

    SET NX only writes when the key is absent, so this can never clobber a
    counter that is already live, even if two requests seed concurrently.
    """
    if not bases:
        return
    pipe = r.pipeline()
    for file_id, base in bases.items():
        pipe.set(_dl_key(file_id), int(base), nx=True)
    pipe.execute()


def get_downloads_many(ids) -> dict:
    """Map id -> count. A value of None means Redis has no key: seed it."""
    ids = [str(i) for i in ids]
    if not ids:
        return {}
    values = r.mget([_dl_key(i) for i in ids])
    return {i: (int(v) if v is not None else None) for i, v in zip(ids, values)}


def get_downloads(file_id) -> int | None:
    value = r.get(_dl_key(file_id))
    return int(value) if value is not None else None


def incr_downloads(file_id) -> int:
    """Count one completed download and mark the id for the next flush."""
    pipe = r.pipeline()
    pipe.incr(_dl_key(file_id))
    pipe.sadd(DIRTY_KEY, str(file_id))
    pipe.incr(STAT_DOWNLOADS)
    total, _, _ = pipe.execute()
    return total


def clear_downloads(file_id) -> None:
    pipe = r.pipeline()
    pipe.delete(_dl_key(file_id))
    pipe.srem(DIRTY_KEY, str(file_id))
    pipe.execute()


def drain_dirty() -> list[str]:
    """Atomically take the set of ids whose counters moved since the last flush.

    Read-then-delete runs inside one MULTI/EXEC. An INCR that lands after the
    EXEC re-adds its id, so it is simply picked up by the next flush: an
    increment is never dropped, only ever deferred.
    """
    pipe = r.pipeline()  # redis-py pipelines are MULTI/EXEC by default
    pipe.smembers(DIRTY_KEY)
    pipe.delete(DIRTY_KEY)
    members, _ = pipe.execute()
    return list(members)


def mark_dirty(ids) -> None:
    """Put ids back in the dirty set, e.g. when a flush failed to reach Postgres."""
    if ids:
        r.sadd(DIRTY_KEY, *[str(i) for i in ids])


def pending_flush_count() -> int:
    return r.scard(DIRTY_KEY)


# ── 3. Observability counters ────────────────────────────────────────────


def incr_uploads() -> None:
    r.incr(STAT_UPLOADS)


def incr_deletes() -> None:
    r.incr(STAT_DELETES)


def stats() -> dict:
    hits, misses = _int(STAT_CACHE_HIT), _int(STAT_CACHE_MISS)
    looked_up = hits + misses
    return {
        "cache": {
            "hits": hits,
            "misses": misses,
            # Measured, not estimated: every GET /files increments one of the two.
            "hit_rate": round(hits / looked_up, 4) if looked_up else 0.0,
            "ttl_seconds": config.CACHE_TTL_SECONDS,
            "listing_ttl_remaining": listing_ttl_remaining(),
        },
        "events": {
            "uploads": _int(STAT_UPLOADS),
            "downloads": _int(STAT_DOWNLOADS),
            "deletes": _int(STAT_DELETES),
        },
    }


def server_info() -> dict:
    info = r.info()
    return {
        "version": info.get("redis_version"),
        "used_memory_bytes": info.get("used_memory"),
        "used_memory_human": info.get("used_memory_human"),
        "connected_clients": info.get("connected_clients"),
        "uptime_seconds": info.get("uptime_in_seconds"),
        "total_commands_processed": info.get("total_commands_processed"),
        # Redis' own keyspace stats, distinct from our application-level ones above.
        "keyspace_hits": info.get("keyspace_hits"),
        "keyspace_misses": info.get("keyspace_misses"),
        "keys": r.dbsize(),
    }


def ping() -> bool:
    return bool(r.ping())
