# Running Mini Vault locally

The whole stack — Angular UI, FastAPI, PostgreSQL, MinIO, Redis — comes up with one
command. You do not need Node, Python, or a database installed on your machine.

---

## 1. Prerequisites

| You need | Version | Check with | Notes |
|---|---|---|---|
| **Docker Desktop** | 4.x (Compose v2) | `docker version` | The only hard requirement. |
| Disk space | ~2 GB | — | Images: python:3.12-slim, node:24-alpine, postgres:16, redis:7, minio. |
| Free ports | 8000, 9001, 9002, 5433, 6379 | see §7 | Compose already dodges the two common clashes. |

Optional, and **only** if you want to edit code with hot reload:

| You need | Version | For |
|---|---|---|
| Node | 22.22.3+ or 24+ | The Angular dev server (`ng serve` on :4200). Angular v22 requires it. |
| Python | 3.12 | Running `uvicorn --reload` outside Docker. |

> Verify Docker is actually running before you start. `docker version` must print a
> **Server** section — if it only prints Client, Docker Desktop isn't up.

---

## 2. Start the app

```bash
cd mini-vault
docker compose up --build
```

That's it. `--build` is needed the first time and after any code change; plain
`docker compose up` is enough otherwise.

**What happens, in order:**

1. **Node build stage** compiles the Angular app to static files (~5s).
2. **Python stage** installs dependencies and copies the compiled bundle to `app/web`.
3. Postgres, Redis and MinIO start and run their healthchecks.
4. The API waits — `depends_on: condition: service_healthy` holds it back until all three
   answer. The app *also* retries on its own (`wait_for_services` in `app/main.py`),
   because containers start fast and the databases inside them get ready slowly.
5. On first boot the API creates the `files` table and the `uploads` bucket.

First build takes 1–3 minutes. Subsequent builds are seconds — Docker caches the
dependency layers, and both `requirements.txt` and `package-lock.json` are copied before
the source so editing code never triggers a reinstall.

Add `-d` to run detached (background): `docker compose up --build -d`.

---

## 3. Open it

| URL | What it is |
|---|---|
| <http://localhost:8000> | **The app.** Angular UI: upload, list, download, delete, live metrics dashboard. |
| <http://localhost:8000/docs> | Swagger UI, generated from the Python type hints. |
| <http://localhost:8000/metrics> | Raw JSON behind the dashboard. |
| <http://localhost:9001> | MinIO console — login `minioadmin` / `minioadmin`. |

There is one frontend and it is served by FastAPI on port 8000. Same origin as the API,
so there is no CORS to configure and no second server to start.

---

## 4. Smoke test (60 seconds)

```bash
echo "hello econet" > note.txt

# Upload — all three services do work on this one request
curl -F "file=@note.txt" localhost:8000/files

# List twice and watch the cache-aside pattern flip
curl -s localhost:8000/files | grep -o '"source":"[a-z-]*"'   # "postgres"    (cache miss)
curl -s localhost:8000/files | grep -o '"source":"[a-z-]*"'   # "redis-cache" (cache hit)

# Health of all three backing services
curl localhost:8000/health
```

Grab the `id` from the upload response, then:

```bash
ID=<paste-the-id>
curl -OJ localhost:8000/files/$ID/download   # streams the bytes back out of MinIO
curl localhost:8000/files/$ID                # download_count, live from Redis
curl -X DELETE localhost:8000/files/$ID      # 204
```

> **Windows/Git Bash gotcha.** If you script this and pipe UUIDs through Python, Python
> emits `\r\n` on Windows. The trailing `\r` gets baked into the URL and curl silently
> rejects it — the request never reaches the server and you'll think the API dropped it.
> Pipe through `tr -d '\r'`.

---

## 5. Stopping and resetting

```bash
Ctrl+C                  # if running in the foreground
docker compose down     # remove containers, KEEP the data
docker compose down -v  # remove containers AND wipe all data (Postgres, MinIO, Redis)
```

Data lives in three named volumes (`pgdata`, `miniodata`, `redisdata`), so files and
download counts survive a normal `down`/`up` cycle. Use `-v` when you want a clean slate.

---

## 6. Configuration

Everything is an environment variable, set in `docker-compose.yml` under `api:`:

| Variable | Default | What it controls |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://vault:vault@postgres:5432/vault` | Postgres connection. |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection. |
| `MINIO_ENDPOINT` | `minio:9000` | S3 endpoint. |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | `minioadmin` | Credentials. |
| `MINIO_BUCKET` | `uploads` | Bucket name; created on boot if missing. |
| `CACHE_TTL_SECONDS` | `30` | Lifetime of the cached file listing. |
| `COUNTER_FLUSH_SECONDS` | `5` | How often Redis download counters mirror into Postgres. |
| `METRICS_HISTORY_DAYS` | `14` | Width of the uploads-per-day chart. |

The last three read from your shell, so you can override them without editing the file:

```bash
COUNTER_FLUSH_SECONDS=30 CACHE_TTL_SECONDS=120 docker compose up -d
```

Handy when demoing: a 30-second flush window makes the write-behind lag visible
(`docs/DATA_FLOW_TALK.md` relies on it). `docker compose up -d api` restores the defaults.

Note the hostnames: **inside** the Compose network, `postgres`, `redis` and `minio` are
DNS names. From your laptop those names mean nothing — you use `localhost` plus the
*published* port. Confusing these two worlds is the most common first-week Docker mistake.

---

## 7. Ports, and why two are unusual

| Service | Host port | Container port | Why |
|---|---|---|---|
| API + UI | 8000 | 8000 | — |
| Postgres | **5433** | 5432 | 5432 is usually taken by a locally installed Postgres. |
| Redis | 6379 | 6379 | — |
| MinIO (S3 API) | **9002** | 9000 | 9000 is a popular default for other local tools. |
| MinIO (console) | 9001 | 9001 | — |

To connect a GUI client (TablePlus, DBeaver) to the database, use
`localhost:5433`, user `vault`, password `vault`, database `vault`.

---

## 8. Development loops

### Editing Angular (hot reload)

The image bakes the compiled bundle in, so `docker compose up` needs no Node. For fast
reload while editing the UI, run the dev server against the containerized API:

```bash
docker compose up -d          # API on :8000
cd frontend
npm install
npm start                     # http://localhost:4200
```

The app picks its API base from the port it's served on: relative URLs when FastAPI
serves it, `http://localhost:8000` under `ng serve`. CORS is open, so no proxy is needed.

### Editing Python (hot reload)

Keep the three backing services in Docker, run the API on your machine:

```bash
docker compose up postgres redis minio -d

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # points at localhost, not the compose hostnames
uvicorn app.main:app --reload
```

A plain checkout has no compiled bundle, so `/` answers **503 with a hint** instead of the
UI. That is expected — run the Angular dev server on `:4200` alongside it.

> `.env.example` points MinIO at `localhost:9000`, but Compose publishes it on **9002**.
> Set `MINIO_ENDPOINT=localhost:9002` in your `.env` when running the API outside Docker.

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `port is already allocated` | Something owns 8000/5433/6379/9001/9002. | Stop it, or change the left-hand side of the `ports:` mapping. |
| `/` returns 503 with "Angular bundle is not present" | You're running `uvicorn` from a checkout, not the image. | Expected — use `docker compose up`, or run `npm start` on :4200. |
| API exits with "postgres never became reachable" | Backing service unhealthy. | `docker compose ps` to see which; `docker compose logs postgres`. |
| UI loads but shows no data | API unreachable from the browser. | `curl localhost:8000/health`. Check the health pills in the header. |
| Download counts look wrong after `down -v` | You wiped the volumes. | Expected — counts live in Postgres + Redis, both were erased. |
| Frontend changes don't appear | The image has the old bundle. | `docker compose up --build` (rebuild), not just `up`. |
| `docker compose up` rebuilds npm every time | You added files that bust the cache layer. | `frontend/node_modules` and `frontend/dist` are in `.dockerignore` — keep them there. |

Useful:

```bash
docker compose ps                    # status + health of every container
docker compose logs -f api           # follow the API log
docker compose logs api | tail -50   # recent requests
docker compose restart api           # restart just the API
```
