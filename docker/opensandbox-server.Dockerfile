# opensandbox-server, containerized (docker-compose.yml's opt-in `sandbox`
# profile, `make sandbox-up`, GRAPH_PATTERNS.md pattern 50) — no official
# image exists (verified against OpenSandbox's own docs/components/server.md,
# which documents only PyPI/`uv pip install` installation), so this is a
# small wrapper around the same pip package `make sandbox-up`'s predecessor
# ran via `uvx` as a bare host process.
#
# Needs the host Docker daemon to create sandbox containers — this image
# gets that via a bind-mounted host /var/run/docker.sock (docker-compose.yml),
# NOT Docker-in-Docker. Same privilege the old bare `uvx` host process
# already had as whatever user ran it; containerizing this doesn't add a new
# escalation, just a different packaging of the same access, so no attempt
# here to run as a non-root user the way the app's own Dockerfile does.
FROM python:3.13-slim

RUN pip install --no-cache-dir opensandbox-server==0.2.3

COPY docker/opensandbox-server.toml /config/sandbox.toml

EXPOSE 8090

CMD ["opensandbox-server", "--config", "/config/sandbox.toml"]
