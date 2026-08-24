#!/usr/bin/env bash
# Back up a game server's data to a tarball on the E: drive.
#
#   bash bootstrap/backup.sh minecraft
#   bash bootstrap/backup.sh cs2
#   bash bootstrap/backup.sh minecraft --keep 10
#
# Backups land OUTSIDE the WSL ext4 disk on purpose. The game data has to live
# in ext4 because drvfs cannot chown, but that puts every world inside a single
# .vhdx -- and a corrupted .vhdx takes the backups with it if they live there
# too. E: is a different filesystem on a different disk.
#
# Minecraft is quiesced rather than stopped: RCON `save-off` + `save-all flush`
# gets a consistent world without kicking anyone. A tar taken mid-chunk-write
# restores to a world with holes in it, which is worse than no backup because
# you will not find out until you load it.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

DEST="${LANTERN_BACKUP_DIR:-/mnt/e/lantern-backups}"
KEEP=5
GAME="${1:-}"
[ "${2:-}" = "--keep" ] && KEEP="${3:-5}"

declare -A GAMES=( [cs2]="LANtern CS2" [minecraft]="LANtern Minecraft" )

# Stardew is not a Pelican server; its saves live in a named compose volume.
if [ "$GAME" = "stardew" ]; then
  DEST="${LANTERN_BACKUP_DIR:-/mnt/e/lantern-backups}"
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

running=0
[ "$(docker inspect -f '{{.State.Status}}' "$UUID" 2>/dev/null)" = "running" ] && running=1

rcon() { python3 bootstrap/mc-rcon.py 127.0.0.1 "$RCON_PORT" "$RCON_PW" "$1" 2>/dev/null; }

quiesced=0
if [ "$GAME" = "minecraft" ] && [ "$running" = "1" ] && [ "$RCON_PW" != "-" ]; then
  echo "  quiescing the world"
  rcon "save-off" >/dev/null
  rcon "save-all flush" >/dev/null
  quiesced=1
  sleep 3
elif [ "$running" = "1" ]; then
  echo "  note: ${GAME} is running and cannot be quiesced -- backup may be inconsistent"
fi

# Restore saving no matter how this exits, or the world silently stops
# persisting until the next restart.
cleanup() {
  if [ "$quiesced" = "1" ]; then
    echo "  re-enabling saves"
    rcon "save-on" >/dev/null
  fi
}
trap cleanup EXIT

echo "  archiving ${UUID} -> ${OUT}"
# --exclude the pack archive and mod jars for Minecraft: 6 GB of them are
# byte-identical to what the installer re-downloads, and what is actually
# irreplaceable is the world.
EXCLUDES=()
if [ "$GAME" = "minecraft" ]; then
  EXCLUDES=(--exclude=./mods --exclude=./libraries --exclude=./.serverpack.zip)
fi

docker run --rm \
  -v /var/lib/pelican/volumes:/v:ro \
  -v "${DEST}:/out" \
  alpine sh -c "
    apk add --no-cache zstd tar >/dev/null 2>&1
    cd /v/${UUID} && tar -c ${EXCLUDES[*]} . | zstd -3 -T0 -q -o /out/$(basename "$OUT")
  "
rc=$?

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

# Retention
mapfile -t old < <(ls -1t "${DEST}/${GAME}-"*.tar.zst 2>/dev/null | tail -n +$((KEEP + 1)))
if [ "${#old[@]}" -gt 0 ]; then
  printf '  pruning %d old backup(s), keeping %d\n' "${#old[@]}" "$KEEP"
  rm -f "${old[@]}"
fi

ls -1t "${DEST}/${GAME}-"*.tar.zst 2>/dev/null | head -"$KEEP" | sed 's|^|    |'
