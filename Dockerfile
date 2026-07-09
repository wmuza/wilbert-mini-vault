# Two stages: Node builds the Angular bundle, Python serves it alongside the API.
# The Node toolchain (and node_modules) stays in the build stage, so the shipped
# image carries only the compiled static files — no npm, no source.
#
# The other three services (postgres, redis, minio) use official images straight
# from Docker Hub; only our own code needs a build.

# ── Stage 1: build the frontend ──────────────────────────────────────────
FROM node:24-alpine AS web

WORKDIR /web

# Copy the lockfile first so Docker's layer cache skips a reinstall when only
# the Angular source changed. `npm ci` installs exactly what the lockfile pins.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# ── Stage 2: the FastAPI service ─────────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /code

# Same layer-caching trick for the Python dependencies.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# app/web is where main.py looks for the single-page app. Its absence is what
# makes a plain `uvicorn app.main:app` checkout fall back to the :4200 dev server.
COPY --from=web /web/dist/frontend/browser ./app/web

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
