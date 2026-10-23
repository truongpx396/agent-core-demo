# One image, three roles — the API service and both queue workers
# (app/job_queue/agent_worker.py, app/ingestion/ingest_worker.py) are the same codebase and the
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
#
# Stages (ENGINEERING_PATTERNS.md pattern 76). `base` is the one place the Python image is pinned and `uv` the one place the
# installer is; `builder` installs the lock into a virtualenv with uv and is thrown away; `runtime`, the LAST stage and so what
# `docker build .` produces, is what ships: the base, the OS package the app needs, that virtualenv copied in, and the app.
# Nothing that installs software is in the shipped image: not uv, not pip. Needs BuildKit (the default builder since Docker 23):
# `RUN --mount` is BuildKit's. docker/ml-service.Dockerfile, the other image production runs, is built the same way and pinned
# to the same two digests (tests/core/test_dockerfile_build.py keeps them equal).

# ---- base: the Python image, pinned by digest -------------------------------------------------------------------------------
# The multi-architecture INDEX of python:3.13-slim (linux/amd64 and linux/arm64, read with `docker manifest inspect`) as of
# 2026-10-23. It floated before, on purpose, for fresh OS patches; a floating tag also means two builds of one commit can differ,
# and a scanner result can change under a PR that touched nothing. What keeps it fresh now: Dependabot proposes a new digest weekly
# (.github/dependabot.yml, with a cooldown), `apt-get upgrade` below runs whenever that layer is rebuilt, and CI's Trivy image gate
# fails if the pinned base ages into a fixable CRITICAL. Bump it by merging that PR, or by hand: take the new index digest,
# change it here and in docker/ml-service.Dockerfile, and run `docker build .`.
FROM python:3.13-slim@sha256:57b29a7c050c4e679e4bea572e14b3965cf4b5839de2da49d55e49ffc4024f39 AS base

# ---- uv: the installer, as a single static binary from its own image, pinned by digest ---------------------------------------
# The multi-architecture index of 0.12.24 (linux/amd64 and linux/arm64, read with `docker buildx imagetools inspect`): the build
# downloads and runs it, so a moving tag would let an upstream change decide what runs here, which is the reason the LiteLLM image
# is pinned the same way. 0.12.24 rather than the newest because the 0.13 line was days old when this was written (Dependabot's
# cooldown now applies that rule to its own bumps). It is copied into the builder stage only. As its own named stage so that
# Dependabot, which reads `FROM` lines, can see and bump it.
FROM ghcr.io/astral-sh/uv:0.12.24@sha256:3af4716e991d6956a41e573eab705d0ee08500cd829ed30293eb8472f372c65a AS uv

# ---- builder: dependencies only, never shipped ------------------------------------------------------------------------------
FROM base AS builder

COPY --from=uv /uv /usr/local/bin/uv

# COMPILE_BYTECODE: write the .pyc files now, so the first import in a container does not (a non-root process in a container
#   would otherwise compile on every start, since it cannot write under the root-owned virtualenv).
# LINK_MODE=copy: the cache mount below is a different filesystem from /opt/venv, where uv's default hard links cannot work
#   (it falls back to copying anyway, and warns); asking for the copy says so.
# PYTHON_DOWNLOADS=never: use the base image's interpreter, the one the runtime stage runs, never a downloaded one.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

RUN uv venv --python /usr/local/bin/python "$VIRTUAL_ENV"

# The lock is bind-mounted rather than COPY'd (no layer holds it) and BuildKit still keys the cache on its CONTENT, so this
# step reruns exactly when the lock changes and not otherwise. The cache mount keeps uv's wheel cache BETWEEN builds in the
# builder (it is never part of an image), so a lock change downloads only the packages that changed instead of all 189:
# `pip install --no-cache-dir`, which this replaces, re-downloaded everything on every miss.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=requirements-lock.txt,target=/tmp/requirements-lock.txt \
    uv pip install -r /tmp/requirements-lock.txt

# The billing package (spec 011): installed from its own directory, NOT through the lock file, which is a machine-generated freeze of a
# real install of requirements.txt. --no-deps because everything it needs is already in the lock above. It is COPY'd, not
# bind-mounted, because building it writes into its source tree; and the old `rm -rf ./packages` has nothing left to do, since this
# stage is not the one that ships.
COPY packages/billing/ /tmp/billing/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --no-deps /tmp/billing

# ---- runtime: what ships -----------------------------------------------------------------------------------------------------
FROM base AS runtime

# libgomp1: onnxruntime's runtime dependency (fastembed's BM25/rerank
# models) — not bundled in its wheel, and the slim base doesn't ship it.
# Nothing else here needs a system package for the Python deps themselves:
# psycopg[binary]/lxml/pillow all ship manylinux wheels for this base
# image's platform. No headless-browser install here — crawl4ai now runs
# in its own dockerized server (docker-compose.yml's `crawl4ai` service,
# GRAPH_PATTERNS.md pattern 50); app/ingestion/web_crawler.py talks to it
# over HTTP, so this image never launches a browser itself.
#
# `apt-get upgrade -y` before the install: pulls security patches for
# packages already baked into the `python:3.13-slim` base layer (perl-base,
# not anything this Dockerfile itself installs) — CI's `trivy` job scans
# the built image's actual OS layer, not just this Dockerfile's own
# instructions, and caught a real fixed-CRITICAL CVE here
# (CVE-2026-13221) that a plain `apt-get install` alone never touches,
# since it only ever installs/upgrades the packages named on its own
# command line.
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

# No pip in the shipped image. The base image's pip vendors its OWN copy of certifi (pip/_vendor/certifi), which Trivy's image
# scan reads as a second certifi that the lock cannot touch (GHSA-6q48-6c7w-9gw2 was exactly that); this Dockerfile used to
# upgrade pip to get a fresh bundled copy. Nothing in the running app installs anything (the sandbox prompts tell the model NOT to
# pip install), and the virtualenv below is made by uv and has no pip of its own, so the shipped image simply carries none: the
# vendored certifi, and any future pip finding, are gone with it. tests/core/test_trivy_ignore.py pins this.
RUN python -m pip uninstall -y pip setuptools wheel

# The dependencies, as built above. The same path in both stages (a virtualenv's scripts name their interpreter by absolute
# path) and the same base image, so the interpreter it links to is the one it was built for.
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# fastembed/huggingface_hub cache their downloaded BM25/rerank models under
# $HOME on first use — set BEFORE the final chown, so anything written as
# root above still ends up owned by appuser.
ENV HOME=/home/appuser

COPY app/ ./app/

# Everything the app opens at runtime that is NOT under app/. Each of these
# degrades QUIETLY when absent, so an image built without them boots healthy and
# is simply missing the feature — which is how this image shipped for a while,
# with no skills, no subagents (so no `run_subagent`) and no sandbox tools:
#   skills/, subagents/ — the catalogs `load_skills()` / `load_subagents()` scan
#       (`SKILLS_DIR` / `SUBAGENTS_DIR`, relative to WORKDIR); an absent
#       directory is `{}`, not an error.
#   scripts/ — `opensandbox_mcp_bridge.py`, which `load_sandbox_tools()` spawns
#       with this interpreter (no bridge: zero sandbox tools, with only a
#       logged warning), plus `python -m scripts.index_skills` / `seed` and the
#       cron jobs (`ops_digest`, `followup_sweep`, `tool_call_dedup_sweep`) that
#       an operator runs inside the container. The whole package ships rather
#       than a hand-picked list that would drift the next time one is added.
# tests/test_dockerfile_runtime_files.py fails if one of these stops being copied
# (or is dropped by .dockerignore); CI's `docker-build` job runs the built image
# to prove they actually landed.
COPY skills/ ./skills/
COPY subagents/ ./subagents/
COPY scripts/ ./scripts/

# Both caches above were written as root (HOME already pointed at
# /home/appuser, but the process creating them still ran as root) —
# appuser needs a writable HOME, and needs to actually OWN what's already
# in it, before USER switches below.
RUN chown -R appuser:appuser /home/appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/ready', timeout=3)" || exit 1

# The API service is the default role; agent-worker/ingest-worker override
# this in docker-compose.yml (`command: ["python", "-m", "app.job_queue.agent_worker"]`
# / `app.ingestion.ingest_worker`) — neither of those binds a port, so EXPOSE/
# HEALTHCHECK above are meaningful for this default role only. A compose service
# inherits the image's HEALTHCHECK unless it says otherwise, so each worker also sets
# `healthcheck: *worker-healthcheck` (disabled) in both compose files; without it every
# worker reports `unhealthy` for a port it never opens.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
