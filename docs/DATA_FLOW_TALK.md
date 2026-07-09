# Mini Vault — data flow walkthrough

**Audience:** intermediate backend engineers
**Duration:** ~45 min (35 talk + 10 Q&A)
**Goal:** everyone leaves able to answer *"when I upload a file, what touches what, and why is it split that way?"*

The talk is built around **four request flows**, not four technologies. Engineers already
know what a cache is; what they don't know is *why this app draws the line where it does*.
Every section pairs a **why** (the design pressure) with a **how** (the code), then proves
it with a **live demo** against a real running service.

---

## Pre-flight — do this 10 minutes before you present

```bash
docker compose down -v

# Widen the write-behind window to 30s (default is 5s).
# Slide 6 depends on this: at 5s the flusher fires before you can finish typing
# the next command, and the whole "Postgres is behind on purpose" beat is lost.
COUNTER_FLUSH_SECONDS=30 docker compose up --build -d

sleep 15
curl -s localhost:8000/health                 # expect all three "ok"
curl -s localhost:8000/metrics | grep -o '"flush_interval_seconds":[0-9]*'   # confirm 30
```

Afterwards, `docker compose up -d api` puts it back to 5s.

Open these tabs in advance:

1. <http://localhost:8000> — the app
2. <http://localhost:9001> — MinIO console (`minioadmin` / `minioadmin`)
3. A terminal, split three ways:
   - `docker compose exec postgres psql -U vault -d vault`
   - `docker compose exec redis redis-cli`
   - a free shell for `curl`

Seed a little history so the charts aren't empty:

```bash
for i in 1 2 3; do echo "sample $i" > s$i.txt; curl -s -F "file=@s$i.txt" localhost:8000/files > /dev/null; done
```

> Leave **one** upload for the live demo. The demo is the upload.

---

## Slide 1 — The problem (3 min)

> "Store a file and remember three things about it: the bytes, the facts, and how often
> people grab it. Each of those has a different shape, and one database is wrong for at
> least two of them."

Put the tension on the board before naming any technology:

| The thing | Its shape | What it needs |
|---|---|---|
| The bytes | 1 KB … 5 GB, opaque | Cheap, streamable, replicated storage |
| The facts (name, size, type, when) | Small, structured, queryable | Transactions, indexes, `GROUP BY` |
| The download count | Tiny, changes constantly | Sub-millisecond increments, must survive a restart |

**Ask the room:** "Put all three in Postgres. What breaks first?"
(Answers you're fishing for: table bloat and backup cost from the blobs; a write
transaction per download to bump an integer.)

That question *is* the architecture. The rest of the talk is the answer.

---

## Slide 2 — The map (4 min)

```
                    ┌──────────────────── docker compose network ────────────────────┐
                    │                                                                │
                    │   ┌──────────────────────────────────────┐                     │
  Browser ──────────┼──►│           FastAPI  (api:8000)         │                     │
   :8000            │   │                                       │                     │
                    │   │  serves the compiled Angular bundle   │                     │
                    │   │  at /  — same origin, no CORS         │                     │
                    │   └───┬──────────────┬──────────────┬─────┘                     │
                    │       │              │              │                           │
                    │       ▼              ▼              ▼                           │
                    │  ┌─────────┐   ┌──────────┐   ┌──────────┐                      │
                    │  │PostgreSQL│   │  MinIO   │   │  Redis   │                      │
                    │  │  :5432   │   │  :9000   │   │  :6379   │                      │
                    │  ├─────────┤   ├──────────┤   ├──────────┤                      │
                    │  │metadata │   │ the      │   │ list     │                      │
                    │  │rows     │   │ bytes    │   │ cache    │                      │
                    │  │+ durable│   │ (bucket: │   │ + live   │                      │
                    │  │  counts │   │ uploads) │   │  counters│                      │
                    │  └─────────┘   └──────────┘   └──────────┘                      │
                    │       ▲                            │                            │
                    │       └────── write-behind ────────┘                            │
                    │              (every 5s, batched)                                │
                    └────────────────────────────────────────────────────────────────┘
```

**Two things to say out loud:**

- **Service names are hostnames.** The API connects to `postgres:5432`, not
  `localhost:5432`. Compose gives every service a DNS name on a private network. From
  your laptop those names mean nothing — you use `localhost` + the published port.
- **The arrow pointing back to Postgres is the interesting one.** Park it; it's Slide 6.

---

## Slide 3 — Who does what, and what lives where (6 min)

| System | Holds | Why *there* and not somewhere else |
|---|---|---|
| **PostgreSQL** | One row per file: `id`, `filename`, `content_type`, `size_bytes`, `object_key`, `uploaded_at`, `download_count` | It's the system of record. Structured data you query, join and back up. It is also the **only** service here that can `GROUP BY`, so every dashboard aggregate comes from it. |
| **MinIO** | The actual bytes, as objects keyed `<uuid>/<filename>` in the `uploads` bucket | Object storage is built for large opaque blobs: cheap, streamable, replicated. Blobs in a relational DB bloat the table and make every backup enormous. MinIO is S3-compatible — this code runs against real AWS S3 by changing only the endpoint and credentials. |
| **Redis** | `files:all` (cached listing), `downloads:<id>` (live counters), `downloads:dirty` (a set), `stats:*` (hit/miss + event counters) | In-memory. An atomic `INCR` costs microseconds; the equivalent Postgres write transaction costs milliseconds. Redis serves the read path and absorbs the write path. |
| **FastAPI** | Nothing — it's the only thing clients talk to | Types come from Python type hints, and those same hints generate `/docs` for free. |
| **Angular** | Nothing — UI state only | Compiled into the API image, served at `/`. One origin, one port, no CORS. |

**The key insight, stated plainly:**

> The `object_key` column is the join between two databases. Postgres knows the *name* of
> the bytes; MinIO knows the bytes. Nothing else connects them.

Note the UUID prefix on the object key. **Ask:** "Why not just store it as `report.pdf`?"
(Because two people uploading `report.pdf` would collide. The UUID prefix makes every key
unique without touching the filename the user sees.)

**Live demo — show the join:**

```bash
# Terminal: psql
SELECT filename, size_bytes, object_key FROM files LIMIT 3;
```

```bash
# Terminal: a free shell — the same objects, from the other side
docker compose exec minio mc alias set local http://localhost:9000 minioadmin minioadmin
docker compose exec minio mc ls --recursive local/uploads
```

Point at one row and its matching object. Same UUID. Two systems.

---

## Slide 4 — Flow 1: Upload (7 min)

**The why:** one request, three services, and the ordering is a decision, not an accident.

```
POST /files
   │
   ├─(1)─► MinIO      put object at <uuid>/<filename>
   │                  ── bytes first: if this fails, nothing else has happened yet
   │
   ├─(2)─► Postgres   INSERT metadata row (object_key points at the bytes)
   │
   └─(3)─► Redis      DEL files:all
                      ── invalidate, so the next read cannot serve a stale listing
```

**Why bytes first?** If MinIO fails, we return an error and no row exists — the system is
consistent. Reverse the order and a Postgres row could point at bytes that were never
written: a dangling reference, and a 500 for anyone who tries to download it.

**Ask:** "What's still not safe here?" — If step 2 fails after step 1 succeeds, you have an
**orphaned object** in MinIO with no row. That's the honest trade-off: we leak an object
rather than serve a broken row. Cleaning it up is what a reconciliation job is for. (The
dashboard already surfaces this: it counts Postgres rows and MinIO objects *independently*
and warns when they drift.)

**Live demo:**

1. Drag a file onto <http://localhost:8000>. Watch the toast.
2. **Postgres** — the facts landed:
   ```sql
   SELECT filename, size_bytes, content_type, download_count FROM files ORDER BY uploaded_at DESC LIMIT 1;
   ```
3. **MinIO console** (:9001) → `uploads` bucket → the new object. Show the UUID folder.
4. **Redis** — the cache was invalidated:
   ```
   TTL files:all
   ```
   Expect **`-2`** (key does not exist). Say it: *"-2 means gone. The upload deleted it."*

---

## Slide 5 — Flow 2: List, and the cache-aside pattern (7 min)

**The why:** reads dominate. Serve them from memory, but never let a write leave a stale
copy behind.

```
GET /files
   │
   ├─► Redis GET files:all ──── HIT ──► return {"source": "redis-cache"}
   │                                     (+ INCR stats:cache:hit)
   │
   └────────────────────────── MISS ──► Postgres SELECT …
                                        Redis SETEX files:all 30s
                                        return {"source": "postgres"}
                                        (+ INCR stats:cache:miss)
```

The `"source"` field is a teaching device baked into the response. Call it twice and watch
it flip.

**Two safety nets, not one:**
- **Invalidate on write** (delete the key) — simple, and always correct.
- **A 30-second TTL** — in case an invalidation is ever missed. Belt and braces.

**The subtle bit — say this slowly:**

> The listing is cached. The **download counts are not.** They're merged onto the response
> from Redis with a single `MGET` at read time. If we cached the counter alongside the
> metadata, we'd serve a number up to 30 seconds out of date — and the bug would look like
> *"the counter is slow"* rather than *"the counter is cached."*

**Live demo:**

```bash
curl -s localhost:8000/files | grep -o '"source":"[a-z-]*"'   # "postgres"     ← miss
curl -s localhost:8000/files | grep -o '"source":"[a-z-]*"'   # "redis-cache"  ← hit
```

```
# redis-cli — the cache now exists, and is counting down
TTL files:all      →  29, 28, 27 …
GET files:all      →  the JSON blob (note: NO download_count in it)
```

Then upload anything and immediately run `TTL files:all` again → **`-2`**. The write blew
the cache away.

Finish on the dashboard: the **Cache hit rate** tile is computed from `stats:cache:hit`
and `stats:cache:miss` — a *measured* number, not an estimate.

```
# redis-cli
GET stats:cache:hit
GET stats:cache:miss
```

---

## Slide 6 — Flow 3: Download, and the counter problem (10 min) ← *the centerpiece*

Start with the naive version, because it's what everyone would write:

```python
redis.incr(f"downloads:{file_id}")   # fast!
```

**Then break it, live.** Ask: "What happens when Redis restarts?"

- Every counter resets to zero.
- The next download `INCR`s a missing key → it comes back as **1**.
- A file with 57 downloads now reads 1. Silently. Forever.

That is a real bug this app used to have. Here's the fix.

### The design: Redis is live, Postgres is durable

```
GET /files/{id}/download
   │
   ├─► Postgres   SELECT object_key  (where are the bytes?)
   │
   ├─► MinIO      stream the object out in 64 KB chunks ──────► client
   │
   └─► AFTER the last byte is sent:
           seed the counter from Postgres if Redis is missing it
           Redis INCR downloads:<id>
           Redis SADD downloads:dirty <id>

   ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
   every 5s, a background task (the "write-behind flusher"):
           MULTI: SMEMBERS downloads:dirty; DEL downloads:dirty
           MGET the counters
           Postgres: UPDATE files SET download_count = GREATEST(download_count, :n)
```

**Four rules, each defending against one failure. Walk them one at a time:**

| Rule | Defends against |
|---|---|
| Counter is **seeded from Postgres before it's ever read or incremented** | A wiped Redis restarting a counter at 1 |
| Flush writes **`GREATEST(postgres, redis)`** | A stale Redis (restored from an old snapshot) dragging the durable count *backwards*. Also makes a retried flush harmless — it's idempotent. |
| Draining `downloads:dirty` is one **`MULTI/EXEC`** | An `INCR` landing mid-flush. It just re-marks its id and gets picked up next tick — increments are *deferred*, never *lost*. |
| The `INCR` fires **after the last byte is streamed** | Counting downloads that were merely *started*. A client that disconnects halfway doesn't count. |

**Why write-behind at all?** Downloads pay only for an in-memory `INCR`. The durable write
happens once per batch, not once per download. Postgres sees one `UPDATE` every 5 seconds
regardless of whether you served 10 downloads or 10,000.

### Live demo — the money shot

> Requires the **30-second flush window** from pre-flight. At the default 5s the flusher
> fires while you're still typing, Postgres already agrees with Redis, and the entire
> point of the slide evaporates. (Ask me how I know.)

```bash
ID=$(curl -s localhost:8000/files | python -c "import sys,json;print(json.load(sys.stdin)['files'][0]['id'])" | tr -d '\r')

# 1. Download it four times
for i in 1 2 3 4; do curl -s -o /dev/null localhost:8000/files/$ID/download; done

# 2. Redis knows immediately
curl -s localhost:8000/files/$ID | grep -o '"download_count":[0-9]*'    # 4
```
```
# redis-cli — the live counter, and the id queued for flushing
GET downloads:<id>          →  "4"
SMEMBERS downloads:dirty    →  1) "<id>"
```
```sql
-- psql — Postgres has NOT caught up yet. This is the point.
SELECT filename, download_count FROM files WHERE id = '<id>';   -- still 0
```

**Pause here.** "Redis says 4. Postgres says 0. Both are correct — Postgres is thirty
seconds behind *on purpose*. Nothing is lost; it's queued in that dirty set."

```bash
sleep 32
```
```sql
-- psql — now it's mirrored
SELECT filename, download_count FROM files WHERE id = '<id>';   -- 4
```
```
# redis-cli — the dirty set drained itself
SMEMBERS downloads:dirty    →  (empty array)
```

**Now destroy Redis in front of them:**

```
# redis-cli
FLUSHALL
```
```bash
curl -s localhost:8000/files/$ID | grep -o '"download_count":[0-9]*'   # 4  ← reseeded from Postgres
curl -s -o /dev/null localhost:8000/files/$ID/download
curl -s localhost:8000/files/$ID | grep -o '"download_count":[0-9]*'   # 5  ← NOT 1
```

> *"We just lost the entire cache layer and the counter didn't even flinch."*

---

## Slide 7 — Flow 4: Delete (2 min)

```
DELETE /files/{id}
   ├─► MinIO      remove object      (bytes first, again)
   ├─► Postgres   DELETE row
   └─► Redis      DEL files:all      (invalidate listing)
                  DEL downloads:<id> (drop the counter)
                  SREM downloads:dirty <id>
```

**Ask:** "Why must we drop the counter?" — Otherwise a new file that happens to reuse the
id would inherit a phantom count. (It won't, since ids are UUIDs — but leaking a key per
deleted file forever is its own bug.)

Note the flusher is safe here regardless: its `UPDATE … WHERE id = …` simply matches zero
rows if the file is already gone.

---

## Slide 8 — The dashboard as a teaching tool (3 min)

`GET /metrics` asks each service about *itself*:

- **Postgres** → aggregates: totals, avg/largest size, uploads per day, breakdown by type.
  `generate_series` supplies the calendar so days with zero uploads still appear — a plain
  `GROUP BY` would omit them and the chart would lie about the shape of the trend.
- **Redis** → the live counters, the hit/miss stats, plus its own `INFO` (memory, uptime, keys).
- **MinIO** → object count and bytes held, counted from the bucket.

**Show the drift check.** `files.count` (Postgres rows) and `minio.objects` are counted
*independently*. If they disagree, the panel says so instead of hiding it — that's your
orphaned-object detector from Slide 4.

Point at **"N awaiting flush to Postgres"** on the *Downloads served* tile. That number is
`SCARD downloads:dirty`. It's the write-behind queue, on screen, in real time.

---

## Slide 9 — Trade-offs we chose (and would revisit) (4 min)

Be honest about the edges. This is what makes intermediate engineers trust the talk.

| Choice | Cost | When you'd change it |
|---|---|---|
| Invalidate the whole listing on every write | A busy vault is a permanent cache miss | Per-item caching, or write-through, once writes outpace reads |
| `create_all` + an idempotent `ALTER TABLE` | Not a migration strategy | Alembic, the moment a second person touches the schema |
| Write-behind counters (5s window) | Up to 5s of counts lost if the API is `kill -9`'d | Shorten the interval, or `INCR` Postgres directly if the count is billing-critical |
| Bytes stream *through* the API | The API is in the data path for every download | Presigned URLs — but note the trap: a URL generated inside Docker contains `minio:9000`, which the browser can't resolve. Needs a public signing endpoint. |
| Orphaned objects on a partial upload | Wasted storage | A reconciliation job over the drift the dashboard already reports |

---

## Q&A — questions you will get

**"Why not just put the count in Postgres with `UPDATE … SET count = count + 1`?"**
You can, and for low traffic you should — it's simpler. It costs a write transaction, a row
lock, and WAL per download. Under load, every download of the same popular file serializes
on that row. Redis absorbs that contention in memory.

**"Isn't `GREATEST` hiding a bug?"**
It's making the write *monotonic and idempotent*. A counter only ever goes up, so
`GREATEST` is the correct merge function for it. If it ever fires, that means Redis was
behind — which is exactly the case we want to survive rather than propagate.

**"What if two API replicas run the flusher at once?"**
Both drain the dirty set atomically, so each id goes to exactly one of them. Both write
absolute values with `GREATEST`, so even overlapping writes converge. It's safe by
construction — though a single leader would waste fewer queries.

**"Why is the frontend inside the API image?"**
One origin means no CORS, one port to expose, one thing to deploy. The Node toolchain
stays in the build stage, so the shipped image carries only static files — no npm.

**"Why is Postgres on host port 5433?"**
Because most of us already have a Postgres on 5432. Same reason MinIO's S3 API is on 9002.

---

## The one-sentence takeaway

> **Each store holds the shape of data it's good at** — bytes in MinIO, facts in Postgres,
> hot counters in Redis — **and the only hard part is the seam between them**, which here is
> the `object_key` column and a write-behind flusher that can lose a race but never a count.
