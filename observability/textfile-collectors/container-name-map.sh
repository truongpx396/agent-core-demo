#!/bin/sh
# Publishes an id -> name mapping for every running container as a
# node-exporter textfile-collector metric (container_id_name_map), so
# Prometheus/Grafana can join it against cAdvisor's cgroup-id-only metrics
# via `* on(id) group_left(name) container_id_name_map`. Exists because
# cAdvisor's own docker-container-factory can't resolve names on this host
# (needs containerd, which Docker Desktop for Mac doesn't expose to
# containers — see docker-compose.yml's cadvisor service comment and
# google/cadvisor#3772) — this sidecar gets the same names a plain
# `docker ps` already has, over docker.sock, with no cAdvisor involvement.
#
# Atomic write (write to .tmp, then mv) so node-exporter's textfile
# collector — which polls this directory on its own schedule — never reads
# a half-written file mid-update.
set -eu

OUT_DIR="${OUT_DIR:-/textfile-collector}"
INTERVAL="${INTERVAL:-15}"

while true; do
  {
    echo "# HELP container_id_name_map Maps a container's cAdvisor cgroup id (cadvisor's own id label) to its actual name, from \`docker ps\`."
    echo "# TYPE container_id_name_map gauge"
    docker ps --no-trunc --format '{{.ID}} {{.Names}}' | while read -r id name; do
      echo "container_id_name_map{id=\"/docker/${id}\",name=\"${name}\"} 1"
    done
  } > "${OUT_DIR}/container_names.prom.tmp"
  mv "${OUT_DIR}/container_names.prom.tmp" "${OUT_DIR}/container_names.prom"
  sleep "${INTERVAL}"
done
