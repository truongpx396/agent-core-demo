# The authlimit service image (specs/018-authlimit-edge-auth).
#
# Two stages, both pinned by digest (the repo's rule: a tag moves, a digest does not):
#   build    the Go toolchain compiles a static binary. Nothing from this stage ships except the two files copied out of it.
#   runtime  distroless static, non-root: no shell, no package manager, no curl, no libc. There is nothing in the final image to
#            run an attacker's commands with, which is also why the container healthcheck is a subcommand of the binary itself
#            (`/authlimit healthcheck`) and not `curl`.
#
# Build context is services/authlimit/ (this directory): the module does not import the rest of the repository.

FROM golang:1.26@sha256:37cfcca63222ec499bec5e8ad7aad52dbf7529db36d5812b8eae2f0621aa92d6 AS build
WORKDIR /src
# Dependencies first, so a source-only change does not re-download them.
COPY go.mod go.sum ./
RUN go mod download
COPY . .
# CGO off: a fully static binary that needs no libc. -trimpath drops the build machine's paths from the binary; -s -w drop the symbol
# and debug tables (smaller, and not needed to run).
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/authlimit ./cmd/authlimit \
 && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/echo ./cmd/echo

FROM gcr.io/distroless/static@sha256:18f4cc26c9ffa7f20a359298be7deadac5c607c8e03a02b8119f1a5fc2f82c99 AS runtime
COPY --from=build /out/authlimit /authlimit
# The stub upstream the smoke check puts behind the front door. A test fixture; the compose profile that starts it is development-only.
COPY --from=build /out/echo /echo
# `nonroot` is uid/gid 65532. The development compose file overrides the user so a key directory created by the host user is readable.
USER nonroot:nonroot
EXPOSE 8080
# Liveness only ("this process answers"); the compose file overrides it with `--ready` (can this replica decide a request) where the
# orchestrator should take a replica out of rotation. Exec form, because there is no shell to run a string form with. Trivy DS-0026 asks
# for an image to say how to check it, so an image run without compose is still checked.
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=5 CMD ["/authlimit", "healthcheck"]
ENTRYPOINT ["/authlimit"]
CMD ["serve"]
