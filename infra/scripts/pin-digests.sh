#!/bin/sh
# Print the current sha256 digest of every third-party base image so you can pin them
# (needs network + docker buildx). Copy the results into .env (POSTGRES_IMAGE=..@sha256:..) or
# pass them as --build-arg (NODE_IMAGE / PYTHON_IMAGE / POSTGRES_ALPINE_IMAGE / BUSYBOX_IMAGE).
# Tags are the ones used by default in the repo; this script never edits files.
set -eu
IMAGES="${*:-postgres:16.6-bookworm postgres:16.6-alpine3.21 eclipse-mosquitto:2.0.20 caddy:2.10.0-alpine node:22.14.0-alpine3.21 python:3.12.8-slim-bookworm python:3.11-slim-bookworm busybox:1.37.0 timescale/timescaledb:2.17.2-pg16}"
for img in $IMAGES; do
  if d=$(docker buildx imagetools inspect "$img" --format '{{.Manifest.Digest}}' 2>/dev/null); then
    printf '%s@%s\n' "$img" "$d"
  else
    printf '# could not resolve %s (tag missing or no network)\n' "$img" >&2
  fi
done
