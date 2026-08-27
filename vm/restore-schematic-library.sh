#!/usr/bin/env bash
# Restore a typed schematic-library backup without touching Minecraft or Wings.

set -euo pipefail

VIEWER_IMAGE='ghcr.io/scotsgamez/create-schematic-viewer:v1.0.1@sha256:d5501af9de95f9b89484ae4e4dbea098b0cdd3e86af3b19e50976855b533444c'
ALPINE_IMAGE='alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc'
STACK=${LANTERN_STACK:-/opt/lantern/stack}
BACKUP_ROOT=${LANTERN_BACKUP_DIR:-/var/backups/lantern}
VOLUME=lantern-schematic-viewer-data

VIEWER_WAS_RUNNING=false
VIEWER_STOPPED=false
RESTART_ALLOWED=true
LIVE_MUTATION_STARTED=false
LIVE_VOLUME_VALID=true
STAGING_VOLUME=
ARCHIVE_LIST=
SAFETY_TMP_DIR=
SAFETY_TMP_ARCHIVE=
SAFETY_TMP_CHECKSUM=
SAFETY_COPY=
SAFETY_PUBLISHED=false
VIEWER_UID=
VIEWER_GID=
READINESS_URL=

usage() {
  echo "usage: $0 /absolute/path/to/schematic-viewer-data.tgz --confirm-replace" >&2
  exit 2
}

clear_live_volume() {
  docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --user "$VIEWER_UID:$VIEWER_GID" \
    --volume "$VOLUME":/live \
    "$ALPINE_IMAGE" \
    sh -ec 'find /live -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +'
}

copy_validated_volume() {
  docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --user "$VIEWER_UID:$VIEWER_GID" \
    --volume "$VOLUME":/live \
    --volume "$STAGING_VOLUME":/stage:ro \
    "$ALPINE_IMAGE" sh -ec 'cp -a /stage/restored/. /live/'
}

copy_safety_volume() {
  docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --user "$VIEWER_UID:$VIEWER_GID" \
    --volume "$VOLUME":/live \
    --volume "$SAFETY_TMP_DIR/restored":/safety:ro \
    "$ALPINE_IMAGE" sh -ec 'cp -a /safety/. /live/'
}

rollback_live_volume() {
  echo 'restoring the pre-restore schematic library' >&2
  if ! clear_live_volume; then
    echo 'CRITICAL: live volume could not be cleared for rollback; viewer will remain stopped' >&2
    RESTART_ALLOWED=false
    return 1
  fi
  if ! copy_safety_volume; then
    echo 'CRITICAL: safety copy could not be restored; viewer will remain stopped' >&2
    RESTART_ALLOWED=false
    return 1
  fi
  LIVE_VOLUME_VALID=true
}

wait_for_readiness() {
  local ready=false
  local _attempt
  for _attempt in $(seq 1 30); do
    if curl --silent --fail --noproxy '*' --max-time 3 "$READINESS_URL" >/dev/null; then
      ready=true
      break
    fi
    sleep 2
  done
  [ "$ready" = true ]
}

resolve_readiness_url() {
  local endpoint host port url_host
  endpoint=$(docker compose port minecraft-ui 8093)
  endpoint=${endpoint%%$'\n'*}
  case "$endpoint" in
    \[*\]:*)
      host=${endpoint#\[}
      host=${host%%\]*}
      port=${endpoint##*:}
      ;;
    *:*)
      host=${endpoint%:*}
      port=${endpoint##*:}
      ;;
    *) echo 'could not resolve the Minecraft UI published port' >&2; return 1 ;;
  esac
  case "$port" in
    '' | *[!0-9]*) echo 'Minecraft UI published an invalid port' >&2; return 1 ;;
  esac
  if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
    echo 'Minecraft UI published an invalid port' >&2
    return 1
  fi
  case "$host" in
    0.0.0.0) url_host=127.0.0.1 ;;
    ::) url_host='[::1]' ;;
    *:*) url_host="[$host]" ;;
    '') echo 'Minecraft UI published an empty host' >&2; return 1 ;;
    *) url_host=$host ;;
  esac
  READINESS_URL="http://${url_host}:${port}/readyz"
}

start_viewer_and_wait() {
  docker compose start schematic-viewer >/dev/null && wait_for_readiness
}

cleanup() {
  local status=$?
  trap - EXIT

  if [ "$LIVE_MUTATION_STARTED" = true ] && [ "$LIVE_VOLUME_VALID" != true ] \
     && [ -n "$SAFETY_TMP_DIR" ]; then
    rollback_live_volume || status=1
  fi

  if [ "$VIEWER_WAS_RUNNING" = true ] && [ "$VIEWER_STOPPED" = true ] \
     && [ "$RESTART_ALLOWED" = true ]; then
    if start_viewer_and_wait; then
      VIEWER_STOPPED=false
    else
      docker compose stop schematic-viewer >/dev/null 2>&1 || true
      RESTART_ALLOWED=false
      echo 'schematic-viewer restart did not become ready; viewer remains stopped' >&2
      status=1
    fi
  fi

  if [ -n "$STAGING_VOLUME" ]; then
    docker volume rm "$STAGING_VOLUME" >/dev/null 2>&1 || true
  fi
  [ -n "$ARCHIVE_LIST" ] && rm -f -- "$ARCHIVE_LIST"
  [ -n "$SAFETY_TMP_DIR" ] && rm -rf -- "$SAFETY_TMP_DIR"
  [ -n "$SAFETY_TMP_ARCHIVE" ] && rm -f -- "$SAFETY_TMP_ARCHIVE"
  [ -n "$SAFETY_TMP_CHECKSUM" ] && rm -f -- "$SAFETY_TMP_CHECKSUM"
  if [ -n "$SAFETY_COPY" ] && [ "$SAFETY_PUBLISHED" != true ]; then
    rm -f -- "$SAFETY_COPY" "$SAFETY_COPY.sha256"
  fi
  exit "$status"
}
trap cleanup EXIT

[ "$#" -eq 2 ] || usage
ARCHIVE=$1
[ "$2" = --confirm-replace ] || usage
case "$ARCHIVE" in
  /*) ;;
  *) echo 'archive path must be absolute' >&2; exit 2 ;;
esac

[ -f "$ARCHIVE" ] && [ ! -L "$ARCHIVE" ] || {
  echo 'archive must be a regular, non-symlink file' >&2
  exit 1
}

[ -d "$BACKUP_ROOT" ] || {
  echo "approved backup root does not exist: $BACKUP_ROOT" >&2
  exit 1
}
BACKUP_ROOT=$(realpath -e -- "$BACKUP_ROOT")
ARCHIVE=$(realpath -e -- "$ARCHIVE") || {
  echo 'archive does not exist' >&2
  exit 1
}
case "$ARCHIVE" in
  "$BACKUP_ROOT"/*) ;;
  *) echo "archive must be under approved backup root: $BACKUP_ROOT" >&2; exit 1 ;;
esac
[ -s "$ARCHIVE" ] || { echo "archive is empty: $ARCHIVE" >&2; exit 1; }

ARCHIVE_DIR=$(dirname "$ARCHIVE")
ARCHIVE_NAME=$(basename "$ARCHIVE")
case "$ARCHIVE_NAME" in
  *[!A-Za-z0-9._-]*) echo 'archive filename contains unsupported characters' >&2; exit 1 ;;
esac
CHECKSUM="$ARCHIVE.sha256"
[ -f "$CHECKSUM" ] && [ ! -L "$CHECKSUM" ] || {
  echo "SHA-256 companion is missing: $CHECKSUM" >&2
  exit 1
}
if ! awk -v name="$ARCHIVE_NAME" '
  $1 ~ /^[0-9a-f]{64}$/ && $2 == name { valid++ }
  END { exit valid == 1 && NR == 1 ? 0 : 1 }
' "$CHECKSUM"; then
  echo 'SHA-256 companion has an invalid record' >&2
  exit 1
fi
if ! (cd "$ARCHIVE_DIR" && sha256sum --check --strict --status "$(basename "$CHECKSUM")"); then
  echo 'schematic-library archive failed SHA-256 verification' >&2
  exit 1
fi

ARCHIVE_LIST=$(mktemp)
if ! tar -tzf "$ARCHIVE" > "$ARCHIVE_LIST"; then
  echo 'archive is not a readable gzip tar' >&2
  exit 1
fi
if awk '
  /^\// { bad=1 }
  {
    count=split($0, part, "/")
    for (i=1; i<=count; i++) if (part[i] == "..") bad=1
  }
  END { exit bad ? 0 : 1 }
' "$ARCHIVE_LIST"; then
  echo 'archive contains an absolute or parent-traversal path' >&2
  exit 1
fi

cd "$STACK"
docker volume inspect "$VOLUME" >/dev/null
resolve_readiness_url

# Discover the runtime identity from the exact released image. Restored files
# must remain writable by the non-root viewer process.
viewer_identity=$(docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --entrypoint sh "$VIEWER_IMAGE" \
  -ec 'printf "%s:%s\n" "$(id -u)" "$(id -g)"')
VIEWER_UID=${viewer_identity%%:*}
VIEWER_GID=${viewer_identity##*:}
case "$VIEWER_UID:$VIEWER_GID" in
  *[!0-9:]* | :* | *:) echo 'released viewer image has an invalid runtime identity' >&2; exit 1 ;;
esac

# Validate the candidate with the viewer's own restore command before stopping
# the live sidecar. It rejects missing markers, symlinks, and special entries.
STAGING_VOLUME="${VOLUME}-restore-$(date +%s)-$$"
docker volume create "$STAGING_VOLUME" >/dev/null
docker run --rm --network none --read-only --cap-drop ALL \
  --cap-add CHOWN --security-opt no-new-privileges --user 0:0 \
  --volume "$STAGING_VOLUME":/stage \
  "$ALPINE_IMAGE" chown "$VIEWER_UID:$VIEWER_GID" /stage
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user "$VIEWER_UID:$VIEWER_GID" \
  --volume "$STAGING_VOLUME":/stage \
  --volume "$ARCHIVE_DIR":/archive:ro \
  "$ALPINE_IMAGE" \
  sh -ec 'mkdir /stage/backup && tar --no-same-owner --no-same-permissions -C /stage/backup -xzf "/archive/$1"' \
  sh "$ARCHIVE_NAME"
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user "$VIEWER_UID:$VIEWER_GID" \
  --env DATA_DIR=/stage/restored \
  --volume "$STAGING_VOLUME":/stage \
  "$VIEWER_IMAGE" \
  node tools/library_data.js restore /stage/backup >/dev/null

viewer_container=$(docker compose ps -q schematic-viewer)
if [ -n "$viewer_container" ]; then
  viewer_running=$(docker inspect -f '{{.State.Running}}' "$viewer_container")
  case "$viewer_running" in
    true)
      VIEWER_WAS_RUNNING=true
      if docker compose stop schematic-viewer >/dev/null; then
        VIEWER_STOPPED=true
      else
        # A non-zero stop can still leave the container stopped. Always make
        # EXIT attempt an idempotent start because it was running beforehand.
        VIEWER_STOPPED=true
        echo 'schematic-viewer could not be stopped safely' >&2
        exit 1
      fi
      ;;
    false) ;;
    *) echo 'could not determine schematic-viewer running state' >&2; exit 1 ;;
  esac
fi

# Create a typed safety backup in an atomically allocated private directory.
umask 077
SAFETY_TMP_DIR=$(mktemp -d "$ARCHIVE_DIR/.pre-restore-schematic-viewer.XXXXXX")
chown "$VIEWER_UID:$VIEWER_GID" "$SAFETY_TMP_DIR"
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user "$VIEWER_UID:$VIEWER_GID" \
  --env DATA_DIR=/data \
  --volume "$VOLUME":/data:ro \
  --volume "$SAFETY_TMP_DIR":/safety \
  "$VIEWER_IMAGE" \
  node tools/library_data.js backup /safety/backup >/dev/null

# Validate the safety backup through the same typed restore path, producing a
# marker-free tree suitable for rollback while preserving the typed archive.
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user "$VIEWER_UID:$VIEWER_GID" \
  --env DATA_DIR=/safety/restored \
  --volume "$SAFETY_TMP_DIR":/safety \
  "$VIEWER_IMAGE" \
  node tools/library_data.js restore /safety/backup >/dev/null

safety_base=$(basename "$SAFETY_TMP_DIR")
SAFETY_COPY="$ARCHIVE_DIR/${safety_base#.}.tgz"
SAFETY_TMP_ARCHIVE=$(mktemp "$ARCHIVE_DIR/.safety-archive.XXXXXX.tgz")
SAFETY_TMP_CHECKSUM=$(mktemp "$ARCHIVE_DIR/.safety-checksum.XXXXXX.sha256")
tar -C "$SAFETY_TMP_DIR/backup" -czf "$SAFETY_TMP_ARCHIVE" .
safety_digest=$(sha256sum "$SAFETY_TMP_ARCHIVE" | awk '{print $1}')
printf '%s  %s\n' "$safety_digest" "$(basename "$SAFETY_COPY")" > "$SAFETY_TMP_CHECKSUM"
chmod 600 "$SAFETY_TMP_ARCHIVE" "$SAFETY_TMP_CHECKSUM"
mv -- "$SAFETY_TMP_ARCHIVE" "$SAFETY_COPY"
SAFETY_TMP_ARCHIVE=
mv -- "$SAFETY_TMP_CHECKSUM" "$SAFETY_COPY.sha256"
SAFETY_TMP_CHECKSUM=
SAFETY_PUBLISHED=true
echo "current library saved to $SAFETY_COPY"

LIVE_MUTATION_STARTED=true
LIVE_VOLUME_VALID=false
if ! clear_live_volume || ! copy_validated_volume; then
  echo 'failed to install the validated library; rolling back' >&2
  rollback_live_volume || exit 1
  if [ "$VIEWER_WAS_RUNNING" = true ]; then
    if start_viewer_and_wait; then
      VIEWER_STOPPED=false
    else
      docker compose stop schematic-viewer >/dev/null 2>&1 || true
      RESTART_ALLOWED=false
      echo 'CRITICAL: original library was restored but readiness failed; viewer remains stopped' >&2
    fi
  fi
  exit 1
fi
LIVE_VOLUME_VALID=true

if [ "$VIEWER_WAS_RUNNING" = true ]; then
  if start_viewer_and_wait; then
    VIEWER_STOPPED=false
  else
    echo 'restored library readiness failed; rolling back' >&2
    docker compose stop schematic-viewer >/dev/null 2>&1 || true
    VIEWER_STOPPED=true
    LIVE_VOLUME_VALID=false
    rollback_live_volume || exit 1
    if start_viewer_and_wait; then
      VIEWER_STOPPED=false
      echo 'original schematic library restored after readiness failure' >&2
    else
      docker compose stop schematic-viewer >/dev/null 2>&1 || true
      RESTART_ALLOWED=false
      echo 'CRITICAL: rollback completed but readiness failed; viewer remains stopped' >&2
    fi
    exit 1
  fi
fi

echo "schematic library restored from $ARCHIVE"
