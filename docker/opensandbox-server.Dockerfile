# opensandbox-server, containerized (docker-compose.yml's opt-in `sandbox`
# profile, `make sandbox-up`, GRAPH_PATTERNS.md pattern 50) — no official
# image exists (verified against OpenSandbox's own docs/components/server.md,
# which documents only PyPI/`uv pip install` installation), so this is a
# small wrapper around the same pip package `make sandbox-up`'s predecessor
# ran via `uvx` as a bare host process.
#
# Needs the host Docker daemon to create sandbox containers — this image
# gets that via a bind-mounted host /var/run/docker.sock (docker-compose.yml),
# NOT Docker-in-Docker.
FROM python:3.13-slim

RUN pip install --no-cache-dir opensandbox-server==0.2.3

COPY docker/opensandbox-server.toml /config/sandbox.toml

# Non-root, but still able to reach /var/run/docker.sock — verified
# directly (`docker run -v /var/run/docker.sock:... alpine ls -la
# /var/run/docker.sock`) that on this repo's actual target environment
# (Docker Desktop for Mac) the socket is owned `root:root` mode
# `srw-rw----`, i.e. group-readable/writable by gid 0. Adding this user to
# that group (not making it uid 0) grants socket access without the
# process also being root for everything else — a real, verified
# reduction in blast radius for a vuln in opensandbox-server's own code,
# even though the socket itself remains an equally privileged resource
# either way (this is about the PROCESS's default identity, not about
# what the socket itself can do). Portability note, disclosed rather than
# hidden: some Linux hosts own the socket `root:docker` with a NON-zero
# docker-group gid instead — on such a host this exact `usermod -aG root`
# line would need to target that GID instead (e.g. `usermod -aG docker`,
# or a build-arg matching the host's actual docker-group gid) for this
# same access to work; not attempted here since this repo's own real
# environment doesn't need it.
RUN useradd --create-home --uid 1000 opensandbox \
    && usermod -aG root opensandbox \
    && mkdir -p /data \
    && chown opensandbox:opensandbox /data
# The `chown /data` above only takes effect on a FRESH `opensandbox-data`
# volume (Docker copies the image's own directory ownership into a named
# volume on its first-ever use only) — verified directly this matters: an
# already-populated volume from a prior root-based run of this same
# service keeps its existing root:root ownership regardless, and the
# server fails to start (`sqlite3.OperationalError: attempt to write a
# readonly database`) until that pre-existing data is re-owned by hand
# (`docker run --rm -v opensandbox-data:/data alpine chown -R 1000:1000
# /data`) — a one-time step for an existing deployment upgrading past this
# Dockerfile change, not something a fresh `make sandbox-up` ever hits.

EXPOSE 8090

# /health verified directly: no auth required, real 200 {"status":"healthy"}
# — no curl in this slim image, so urllib instead (same pattern the app's
# own Dockerfile already uses for its own HEALTHCHECK).
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8090/health', timeout=3)" || exit 1

USER opensandbox
CMD ["opensandbox-server", "--config", "/config/sandbox.toml"]
