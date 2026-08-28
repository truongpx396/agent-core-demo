# One image, three roles — the API service and both queue workers
# (app/turns/agent_worker.py, app/ingestion/ingest_worker.py) are the same codebase and the
# same dependency set, just a different entrypoint command. docker-compose.yml
# builds this once and overrides `command:` per service rather than
# maintaining three near-identical Dockerfiles.
#
# Installs from requirements-lock.txt (not requirements.txt) — the exact,
# transitively-pinned freeze checked into this repo (see that file's own
# header for how it's regenerated) — so a build today and a build next year
# resolve to the identical dependency graph. requirements.txt stays the
# human-edited source of truth; this is what actually gets installed.
#
# This container is NOT how `make serve`/`make chat` run today (those stay
# host processes for fast --reload iteration, see Makefile) — it's the
# deployable artifact for anyone running this stack for real, wired up as
# opt-in docker-compose services (`docker compose --profile app up -d`,
# `make up-app`) alongside the existing infra-only `make up` default.
FROM python:3.13-slim AS base

# libgomp1: onnxruntime's runtime dependency (fastembed's BM25/rerank
# models) — not bundled in its wheel, and the slim base doesn't ship it.
# Nothing else here needs a system package: psycopg[binary]/lxml/pillow
# all ship manylinux wheels for this base image's platform.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

COPY requirements-lock.txt .
RUN pip install --no-cache-dir -r requirements-lock.txt

COPY app/ ./app/

# fastembed/huggingface_hub cache their downloaded BM25/rerank models under
# $HOME on first use (app/retrieval/embeddings.py) — appuser needs a real, writable
# HOME for that, not root's.
ENV HOME=/home/appuser
RUN chown -R appuser:appuser /home/appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/ready', timeout=3)" || exit 1

# The API service is the default role; agent-worker/ingest-worker override
# this in docker-compose.yml (`command: ["python", "-m", "app.turns.agent_worker"]`
# / `app.ingestion.ingest_worker`) — neither of those binds a port, so EXPOSE/
# HEALTHCHECK above are meaningful for this default role only.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
