#!/usr/bin/env bash
# Back up one game server's data to a tarball.
#
#   bash bootstrap/backup.sh minecraft
#   bash bootstrap/backup.sh cs2
#   bash bootstrap/backup.sh minecraft --keep 10
#
# This is the single-game version, kept because it is the quick thing to reach
# for before a risky change to one server. vm/backup-all.sh is the complete set
# -- databases, configs and every world -- and is what runs nightly.
#
# It writes inside the VM. Getting the result onto a different physical disk is
# vm/backup-pull.ps1's job, which copies to D:. A backup that lives only on the
# machine it is backing up is not one.
#
# Minecraft is quiesced rather than stopped: RCON `save-off` + `save-all flush`
# gets a consistent world without kicking anyone. A tar taken mid-chunk-write
# restores to a world with holes in it, which is worse than no backup because
# you will not find out until you load it.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

DEST="${LANTERN_BACKUP_DIR:-/var/backups/lantern}"
KEEP=5
GAME="${1:-}"
[ "${2:-}" = "--keep" ] && KEEP="${3:-5}"

declare -A GAMES=( [cs2]="LANtern CS2" [minecraft]="LANtern Minecraft" )

# Stardew is not a Pelican server; its saves live in a named compose volume.
if [ "$GAME" = "stardew" ]; then
  DEST="${LANTERN_BACKUP_DIR:-/var/backups/lantern}"
  mkdir -p "$DEST" || { echo "cannot write to ${DEST}" >&2; exit 1; }
  OUT="${DEST}/stardew-$(date -u +%Y%m%d-%H%M%S).tar.zst"
  echo "  archiving the saves volume -> ${OUT}"
  docker run --rm -v lantern-stardew_saves:/s:ro -v "${DEST}:/out" alpine sh -c "
    apk add --no-cache zstd tar >/dev/null 2>&1
    cd /s && tar -c . | zstd -3 -T0 -q -o /out/$(basename "$OUT")
  " || { echo "  FAILED" >&2; exit 1; }
  [ -s "$OUT" ] || { echo "  FAILED: empty archive" >&2; rm -f "$OUT"; exit 1; }
  echo "  wrote $(du -h "$OUT" | cut -f1)"
  mapfile -t old < <(ls -1t "${DEST}/stardew-"*.tar.zst 2>/dev/null | tail -n +$((KEEP + 1)))
  [ "${#old[@]}" -gt 0 ] && { echo "  pruning ${#old[@]} old backup(s)"; rm -f "${old[@]}"; }
  exit 0
fi

if [ -z "${GAMES[$GAME]:-}" ]; then
  echo "usage: backup.sh <cs2|minecraft|stardew> [--keep N]" >&2
  exit 2
fi
NAME="${GAMES[$GAME]}"

tinker() {
  docker compose exec -T panel php artisan tinker --execute="$1" 2>/dev/null | grep -vE '^\s*$'
}

UUID=$(tinker "\$s = \App\Models\Server::where('name','${NAME}')->first(); echo \$s ? \$s->uuid : '';" \
       | tail -1 | tr -d '[:space:]')
[ -n "$UUID" ] || { echo "no server named '${NAME}'" >&2; exit 1; }

read -r RCON_PW RCON_PORT <<EOF
$(tinker "
  \$s = \App\Models\Server::where('name','${NAME}')->firstOrFail();
  \$e = [];
  foreach (\$s->variables as \$v) { \$e[\$v->env_variable] = \$v->server_value ?? \$v->default_value; }
  echo (\$e['RCON_PASSWORD'] ?? '-').' '.(\$e['RCON_PORT'] ?? '0');
" | tail -1)
EOF

mkdir -p "$DEST" || { echo "cannot write to ${DEST}" >&2; exit 1; }
STAMP=$(date -u +%Y%m%d-%H%M%S)
OUT="${DEST}/${GAME}-${STAMP}.tar.zst"

if ! server_running=$(docker inspect -f '{{.State.Running}}' "$UUID" 2>/dev/null) \
   || { [ "$server_running" != true ] && [ "$server_running" != false ]; }; then
  echo "could not determine whether ${GAME} is running; refusing the archive" >&2
  exit 1
fi
running=0
[ "$server_running" = true ] && running=1

rcon() {
  printf '%s' "$RCON_PW" \
    | python3 bootstrap/mc-rcon.py --password-stdin 127.0.0.1 "$RCON_PORT" "$1" 2>/dev/null
}

quiesced=0
restore_saves() {
  if [ "$quiesced" = "1" ]; then
    # Consume the recovery intent before the authoritative attempt. If this
    # fails, the EXIT trap must preserve the failure rather than retrying with
    # a result that cannot be represented by this one-shot backup command.
    quiesced=0
    echo "  re-enabling saves"
    if rcon "save-on" >/dev/null; then
      return 0
    else
      echo "  FAILED: saves could not be re-enabled; restart Minecraft before play resumes" >&2
      return 1
    fi
  fi
}
finish() {
  local status=$?
  trap - EXIT
  restore_saves || status=1
  exit "$status"
}
trap finish EXIT

if [ "$GAME" = "minecraft" ] && [ "$running" = "1" ] && [ "$RCON_PW" != "-" ]; then
  echo "  quiescing the world"
  # A nonzero client result does not prove Minecraft missed the request. Set
  # recovery intent before transmission so the EXIT path sends one save-on
  # even when the save-off response is lost.
  quiesced=1
  if ! rcon "save-off" >/dev/null; then
    echo "  FAILED: save-off did not succeed; refusing a live world archive" >&2
    exit 1
  fi
  if ! rcon "save-all flush" >/dev/null; then
    echo "  FAILED: save-all flush did not succeed; refusing a live world archive" >&2
    exit 1
  fi
  sleep 3
elif [ "$running" = "1" ]; then
  if [ "$GAME" = "minecraft" ]; then
    echo "  FAILED: Minecraft is running without RCON credentials; refusing a live world archive" >&2
    exit 1
  fi
  echo "  note: ${GAME} is running and cannot be quiesced -- backup may be inconsistent"
fi

echo "  archiving ${UUID} -> ${OUT}"
# --exclude the pack archive and mod jars for Minecraft: 6 GB of them are
# byte-identical to what the installer re-downloads, and what is actually
# irreplaceable is the world.
EXCLUDES=()
if [ "$GAME" = "minecraft" ]; then
  EXCLUDES=(--exclude=./mods --exclude=./libraries --exclude=./.serverpack.zip)
  if [ "$running" = "0" ]; then
    if ! state_recheck=$(docker inspect -f '{{.State.Running}}' "$UUID" 2>/dev/null) \
       || [ "$state_recheck" != false ]; then
      echo "  FAILED: Minecraft started before the offline archive" >&2
      exit 1
    fi
  fi
fi

docker run --rm \
  -v /var/lib/pelican/volumes:/v:ro \
  -v "${DEST}:/out" \
  alpine sh -c "
    apk add --no-cache zstd tar >/dev/null 2>&1
    cd /v/${UUID} && tar -c ${EXCLUDES[*]} . | zstd -3 -T0 -q -o /out/$(basename "$OUT")
  "
rc=$?

if [ "$GAME" = "minecraft" ] && [ "$running" = "0" ]; then
  if ! state_recheck=$(docker inspect -f '{{.State.Running}}' "$UUID" 2>/dev/null) \
     || [ "$state_recheck" != false ]; then
    echo "  FAILED: Minecraft state changed during the offline archive" >&2
    rm -f "$OUT"
    exit 1
  fi
fi

if [ "$rc" != "0" ] || [ ! -s "$OUT" ]; then
  echo "  FAILED (exit ${rc})" >&2
  rm -f "$OUT"
  exit 1
fi

size=$(du -h "$OUT" | cut -f1)
echo "  wrote ${size}"

# Verify the archive is readable before trusting it. An unverified backup is a
# guess, and you find out it was wrong at the worst possible moment.
if zstd -t "$OUT" >/dev/null 2>&1 || docker run --rm -v "${DEST}:/out:ro" alpine sh -c \
     "apk add --no-cache zstd >/dev/null 2>&1 && zstd -t /out/$(basename "$OUT")" >/dev/null 2>&1; then
  echo "  archive verified"
else
  echo "  WARNING: archive failed its integrity check" >&2
  exit 1
fi

# Do this before retention so a backup whose save recovery failed cannot evict
# a previous known-good restore point.
restore_saves || exit 1

# Retention
mapfile -t old < <(ls -1t "${DEST}/${GAME}-"*.tar.zst 2>/dev/null | tail -n +$((KEEP + 1)))
if [ "${#old[@]}" -gt 0 ]; then
  printf '  pruning %d old backup(s), keeping %d\n' "${#old[@]}" "$KEEP"
  rm -f "${old[@]}"
fi

ls -1t "${DEST}/${GAME}-"*.tar.zst 2>/dev/null | head -"$KEEP" | sed 's|^|    |'
