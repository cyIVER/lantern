#!/usr/bin/env bash
# Restore the named schematic-library volume without touching Minecraft or Wings.

set -euo pipefail

usage() {
  echo "usage: $0 /absolute/path/to/schematic-viewer-data.tgz --confirm-replace" >&2
  exit 2
}

[ "$#" -eq 2 ] || usage
ARCHIVE=$1
[ "$2" = --confirm-replace ] || usage
case "$ARCHIVE" in
  /*) ;;
  *) echo 'archive path must be absolute' >&2; exit 2 ;;
esac
[ -s "$ARCHIVE" ] || { echo "archive is missing or empty: $ARCHIVE" >&2; exit 1; }

STACK=${LANTERN_STACK:-/opt/lantern/stack}
VOLUME=lantern-schematic-viewer-data
ARCHIVE_DIR=$(dirname "$ARCHIVE")
ARCHIVE_NAME=$(basename "$ARCHIVE")
SAFETY_COPY="${ARCHIVE_DIR}/pre-restore-schematic-viewer-$(date -u +%Y%m%d-%H%M%S).tgz"
LIST=$(mktemp)
VIEWER_WAS_RUNNING=false
STAGING_VOLUME=
ALPINE_RESTORE_IMAGE='alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc'

cleanup() {
  rm -f "$LIST"
  if [ -n "$STAGING_VOLUME" ]; then
    docker volume rm "$STAGING_VOLUME" >/dev/null 2>&1 || true
  fi
  if [ "$VIEWER_WAS_RUNNING" = true ]; then
    docker compose start schematic-viewer >/dev/null
    VIEWER_WAS_RUNNING=false
  fi
}
trap cleanup EXIT

tar -tzf "$ARCHIVE" > "$LIST" || { echo 'archive is not a readable gzip tar' >&2; exit 1; }
if awk '
  /^\// { bad=1 }
  {
    count=split($0, part, "/")
    for (i=1; i<=count; i++) if (part[i] == "..") bad=1
  }
  END { exit bad ? 0 : 1 }
' "$LIST"; then
  echo 'archive contains an absolute or parent-traversal path' >&2
  exit 1
fi

cd "$STACK"
docker volume inspect "$VOLUME" >/dev/null

viewer_container=$(docker compose ps -q schematic-viewer 2>/dev/null || true)
if [ -n "$viewer_container" ] \
   && [ "$(docker inspect -f '{{.State.Running}}' "$viewer_container" 2>/dev/null)" = true ]; then
  VIEWER_WAS_RUNNING=true
  docker compose stop schematic-viewer >/dev/null
fi

umask 077
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges -v "$VOLUME":/v:ro "$ALPINE_RESTORE_IMAGE" \
  tar -C /v -czf - . > "$SAFETY_COPY"
[ -s "$SAFETY_COPY" ] || { echo 'could not create the pre-restore safety copy' >&2; exit 1; }
echo "current library saved to $SAFETY_COPY"

STAGING_VOLUME="${VOLUME}-staging-$(date +%s)"
docker volume create "$STAGING_VOLUME" >/dev/null

docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges -v "$STAGING_VOLUME":/staging \
  -v "$ARCHIVE_DIR":/restore:ro "$ALPINE_RESTORE_IMAGE" \
  tar -C /staging -xzf "/restore/$ARCHIVE_NAME" || {
  echo 'failed to extract archive to staging volume' >&2
  exit 1
}

if ! docker run --rm --network none --read-only --cap-drop ALL \
     --security-opt no-new-privileges -v "$STAGING_VOLUME":/staging:ro \
     "$ALPINE_RESTORE_IMAGE" \
     sh -ec 'test -n "$(find /staging -mindepth 1 -print -quit)"'; then
  echo 'staging volume is empty after extraction; archive may be invalid' >&2
  exit 1
fi

docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges -v "$VOLUME":/v "$ALPINE_RESTORE_IMAGE" \
  sh -ec 'find /v -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +' || {
  echo 'failed to clear live volume; restoring from safety copy' >&2
  docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges -v "$VOLUME":/v \
    -v "$ARCHIVE_DIR":/restore:ro "$ALPINE_RESTORE_IMAGE" \
    tar -C /v -xzf "/restore/$(basename "$SAFETY_COPY")" || {
    echo 'CRITICAL: safety copy restore also failed; manual recovery required' >&2
    exit 1
  }
  exit 1
}

docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges -v "$VOLUME":/v -v "$STAGING_VOLUME":/staging:ro \
  "$ALPINE_RESTORE_IMAGE" sh -ec 'cp -a /staging/. /v/' || {
  echo 'failed to copy staging to live volume; restoring from safety copy' >&2
  docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges -v "$VOLUME":/v \
    -v "$ARCHIVE_DIR":/restore:ro "$ALPINE_RESTORE_IMAGE" \
    tar -C /v -xzf "/restore/$(basename "$SAFETY_COPY")" || {
    echo 'CRITICAL: safety copy restore also failed; manual recovery required' >&2
    exit 1
  }
  exit 1
}

if ! docker run --rm --network none --read-only --cap-drop ALL \
     --security-opt no-new-privileges -v "$VOLUME":/v:ro \
     "$ALPINE_RESTORE_IMAGE" \
     sh -ec 'test -n "$(find /v -mindepth 1 -print -quit)"'; then
  echo 'restored volume is empty; use the pre-restore safety copy to recover' >&2
  exit 1
fi

if [ "$VIEWER_WAS_RUNNING" = true ]; then
  docker compose start schematic-viewer >/dev/null
  VIEWER_WAS_RUNNING=false
  ready=false
  for _ in $(seq 1 30); do
    if curl --silent --fail --max-time 3 http://127.0.0.1:8093/readyz >/dev/null; then
      ready=true
      break
    fi
    sleep 2
  done
  [ "$ready" = true ] || {
    echo 'viewer restarted but LANtern readiness did not recover' >&2
    exit 1
  }
fi

echo "schematic library restored from $ARCHIVE"
