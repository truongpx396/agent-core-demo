# Custom CPU inference microservice (the `ml_service` package under docker/ml-service/, entry point main.py) — hosts
# this app's small, latency-critical ONNX models: reranking (originally
# the only one) and prompt-injection classification (added alongside it,
# reusing the same container/batching engine rather than standing up a
# third service). See the package's batching.py docstring for why each model
# lives here instead of TEI (architecture-unsupported for MiniLM; no
# notion of a plain classifier like Prompt Guard at all) or a serving
# framework (BentoML/Ray Serve/Triton — each ruled out for a concrete,
# verified reason, also in that docstring). Mirrors the root Dockerfile's
# own shape (slim base, libgomp1 for onnxruntime, non-root appuser) since
# it has the same runtime dependency family (onnxruntime + tokenizers),
# just a much smaller, single-purpose requirements.txt.
#
# Built like the root Dockerfile, for the same reasons (ENGINEERING_PATTERNS.md pattern 76): the base image and uv are pinned by
# digest in their own stages, the dependencies are installed by uv into a virtualenv in a throwaway `builder` stage with a cache
# mount, and the LAST stage, `runtime`, copies that virtualenv in and ships no installer (not uv, not pip). The two digests below
# are the root Dockerfile's, and tests/core/test_dockerfile_build.py fails if they ever differ. Needs BuildKit.

# ---- base: the Python image, pinned by digest (see the root Dockerfile for why and how it is bumped) ------------------------
FROM python:3.13-slim@sha256:57b29a7c050c4e679e4bea572e14b3965cf4b5839de2da49d55e49ffc4024f39 AS base

# ---- uv: the installer, pinned by digest (see the root Dockerfile) ----------------------------------------------------------
FROM ghcr.io/astral-sh/uv:0.12.24@sha256:3af4716e991d6956a41e573eab705d0ee08500cd829ed30293eb8472f372c65a AS uv

# ---- builder: dependencies only, never shipped ------------------------------------------------------------------------------
FROM base AS builder

COPY --from=uv /uv /usr/local/bin/uv

# The root Dockerfile's reasons: bytecode written at build time, copy not hard link across the cache mount, the base image's own
# interpreter and never a downloaded one.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

RUN uv venv --python /usr/local/bin/python "$VIRTUAL_ENV"

# requirements.txt is bind-mounted, not COPY'd, so the step reruns exactly when it changes; the wheel cache persists between builds.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=docker/ml-service/requirements.txt,target=/tmp/requirements.txt \
    uv pip install -r /tmp/requirements.txt

# ---- runtime: what ships ----------------------------------------------------------------------------------------------------
FROM base AS runtime

# `apt-get upgrade -y` before the install — same base image as the root
# Dockerfile, same reasoning: picks up security patches for packages
# already in the `python:3.13-slim` layer, not just what this line
# explicitly installs. See that file's own comment for the real CVE this
# caught. curl is the HEALTHCHECK's client.
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

# No pip in the shipped image (the root Dockerfile says why: its vendored certifi is what Trivy reads as a second certifi).
RUN python -m pip uninstall -y pip setuptools wheel

COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# fastembed/huggingface_hub cache both downloaded ONNX models under $HOME
# on first use — set BEFORE the chown below, same reasoning as the root
# Dockerfile's own HOME/chown ordering.
ENV HOME=/home/appuser

# The whole service directory: the `ml_service` package, the `main.py` shim `uvicorn main:app` imports, `models.toml` (the declared
# text models) and `heads/` (classifier heads, specs/014). It used to be the single file main.py; the logic became an importable
# package so tests and the head trainer can use the very code the service serves with.
COPY docker/ml-service/ ./

# The named volume mounted at /home/appuser/.cache (docker-compose.yml)
# only inherits this chown correctly if the directory already exists at
# mount time — huggingface_hub doesn't create it until the first download,
# which is too late (the volume driver seeds a NEW empty volume's
# ownership from whatever's already at that path in the image, root:root
# if nothing is). Verified directly: omitting this mkdir produces
# "Permission denied (os error 13)" from hf-xet's first cache write.
RUN mkdir -p /home/appuser/.cache && chown -R appuser:appuser /home/appuser /app
USER appuser

EXPOSE 80

# start_period generous enough to cover a cold pull of BOTH models on
# first startup (reranker + prompt-guard), not just one.
HEALTHCHECK --interval=10s --timeout=5s --start-period=45s --retries=12 \
    CMD curl -sf http://localhost:80/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80"]
