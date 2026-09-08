# Base image for the ops domain's sandbox tools (app/domains/ops/
# sandbox_session.py, GRAPH_PATTERNS.md pattern 50) — referenced by
# OPS_SANDBOX_IMAGE (app/core/config.py). The sandbox has NO network access
# by design (run_command_in_sandbox's own tool description) — a real
# security boundary, not a gap to work around — so a package it needs has
# to be baked in here rather than pip-installed at call time.
#
# numpy pinned to the SAME version already pinned for the app itself
# (requirements-lock.txt); pandas has no existing pin elsewhere in this
# repo, so pinned to its own current latest stable (verified against PyPI,
# not guessed). Kept to these two, not a broader science stack — real,
# requested needs (recomputing a percentile from raw readings, the ops
# system prompt's own example; tabular data wrangling), not speculative.
# Add more here, the same way, if another real need shows up.
#
# Built via `make ops-sandbox-build`, NOT a docker-compose service — this
# is an IMAGE opensandbox-server references by tag when creating ephemeral
# sandbox containers (via the host Docker socket it already has, see
# docker/opensandbox-server.Dockerfile), not a long-running service itself.
FROM python:3.12-slim
RUN pip install --no-cache-dir numpy==2.5.3 pandas==3.0.5
