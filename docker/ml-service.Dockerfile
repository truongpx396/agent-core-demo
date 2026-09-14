# Custom CPU inference microservice (docker/ml-service/main.py) — hosts
# this app's small, latency-critical ONNX models: reranking (originally
# the only one) and prompt-injection classification (added alongside it,
# reusing the same container/batching engine rather than standing up a
# third service). See that file's own module docstring for why each model
# lives here instead of TEI (architecture-unsupported for MiniLM; no
# notion of a plain classifier like Prompt Guard at all) or a serving
# framework (BentoML/Ray Serve/Triton — each ruled out for a concrete,
# verified reason, also in that docstring). Mirrors the root Dockerfile's
# own shape (slim base, libgomp1 for onnxruntime, non-root appuser) since
# it has the same runtime dependency family (onnxruntime + tokenizers),
# just a much smaller, single-purpose requirements.txt.
FROM python:3.13-slim AS base

# `apt-get upgrade -y` before the install — same base image as the root
# Dockerfile, same reasoning: picks up security patches for packages
# already in the `python:3.13-slim` layer, not just what this line
# explicitly installs. See that file's own comment for the real CVE this
# caught.
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

COPY docker/ml-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# fastembed/huggingface_hub cache both downloaded ONNX models under $HOME
# on first use — set BEFORE the chown below, same reasoning as the root
# Dockerfile's own HOME/chown ordering.
ENV HOME=/home/appuser

COPY docker/ml-service/main.py .

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
