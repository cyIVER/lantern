#!/usr/bin/env bash
# Back up everything about LANtern that cannot be downloaded again.
#
# WHAT IS WORTH BACKING UP
#
# The stack occupies about 84 GB, and roughly 2.7 GB of that matters -- which
# compresses to a set of about 165 MB, so a backup that small is correct. CS2's
# game content is 67 GB that SteamCMD will fetch again on demand; Stardew's
# game install is another 271 MB of the same. What no download can replace:
#
#   panel database      the servers, the users, the node -- and cs2_weaponpaints,
#                       which is where every loadout and preset lives
#   /etc/pelican        Wings' node token. Without it the panel cannot re-adopt
#                       its own node and every server becomes uncontrollable
#   Minecraft world     the world, and nothing else in that directory matters
#   schematic library   user-curated schematics and their generated previews
#   CS2 cfg + addons    CounterStrikeSharp, WeaponPaints and their configuration
#   Stardew saves       the farm, plus the SMAPI config beside it
#   .env files          gitignored, so they exist nowhere else
#
# Consistency matters more than speed here. The database is dumped rather than
# copied: a tar of a live MariaDB datadir is a copy of a database mid-write,
# and it restores to something that looks fine until it does not. Minecraft is
# quiesced through RCON instead of stopped, so nobody gets kicked -- a tar
# taken mid-chunk-write restores a world with holes in it, which is worse than
# no backup because you find out weeks later.
#
# Run it on the VM. vm/backup-pull.ps1 copies the result to D: from Windows.

set -uo pipefail

DEST="${LANTERN_BACKUP_DIR:-/var/backups/lantern}"
KEEP="${LANTERN_BACKUP_KEEP:-7}"
STACK="${LANTERN_STACK:-/opt/lantern/stack}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="${DEST}/${STAMP}"

CS2_UUID=a530efc8-3095-4030-8ddb-c82c1d5c56fb
MC_UUID=2be9425c-1141-4181-b0a0-34f38d84fb7f

ok()   { printf '  \033[32m%s\033[0m\n' "$*"; }
bad()  { printf '  \033[31m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
step() { printf '\n\033[36m%s\033[0m\n' "$*"; }

FAILED=0
fail() { bad "$*"; FAILED=$((FAILED + 1)); }

# If the process is interrupted after the schematic sidecar is stopped, bring
# that sidecar back. This deliberately names only schematic-viewer: the
# Minecraft game, Wings, and the public Minecraft UI are never stopped here.
SCHEMATIC_VIEWER_WAS_RUNNING=false
restart_schematic_viewer() {
  if [ "$SCHEMATIC_VIEWER_WAS_RUNNING" = true ]; then
    if docker compose start schematic-viewer >/dev/null 2>&1; then
      note '  schematic-viewer restarted'
    else
      fail '  schematic-viewer did not restart'
    fi
    SCHEMATIC_VIEWER_WAS_RUNNING=false
  fi
}
trap restart_schematic_viewer EXIT

cd "$STACK" || { bad "no stack at $STACK"; exit 1; }
sudo mkdir -p "$OUT" && sudo chown "$(id -u):$(id -g)" "$OUT" || { bad "cannot write $OUT"; exit 1; }

step "Backing up to ${OUT}"

# ---------------------------------------------------------------- database
# A dump, not a volume copy. Also far smaller: the datadir is 228 MB, the dump
# of what matters is a couple of MB.
note 'panel + weaponpaints databases'
# Read the one value needed rather than sourcing .env. Sourcing it under
# `set -u` dies on the CurseForge API key, which contains a literal $2 that
# bash expands as an unset positional parameter -- an error that points at
# line 8 of .env and has nothing to do with line 8 of .env.
DB_ROOT_PASSWORD=$(grep -m1 '^DB_ROOT_PASSWORD=' .env | cut -d= -f2- | tr -d '\r')
# Strip surrounding quotes with parameter expansion rather than a tr that has
# to contain both quote characters and survive three levels of shell quoting.
DB_ROOT_PASSWORD="${DB_ROOT_PASSWORD%\"}"; DB_ROOT_PASSWORD="${DB_ROOT_PASSWORD#\"}"
DB_ROOT_PASSWORD="${DB_ROOT_PASSWORD%\'}"; DB_ROOT_PASSWORD="${DB_ROOT_PASSWORD#\'}"
if [ -z "${DB_ROOT_PASSWORD}" ]; then
  fail '  DB_ROOT_PASSWORD not found in stack/.env'
elif docker exec -e MYSQL_PWD="$DB_ROOT_PASSWORD" stack-database-1 \
     mariadb-dump -uroot --single-transaction --routines --events \
     --databases panel cs2_weaponpaints 2>/dev/null | gzip > "$OUT/databases.sql.gz"; then
  # Verify by looking inside, not by trusting the pipe: mysqldump happily
  # writes a truncated file and exits 0 when the connection drops.
  if zcat "$OUT/databases.sql.gz" 2>/dev/null | tail -5 | grep -q 'Dump completed'; then
    ok "  databases.sql.gz ($(du -h "$OUT/databases.sql.gz" | cut -f1))"
  else
    fail '  the dump is truncated -- no "Dump completed" marker'
  fi
else
  fail '  database dump failed'
fi

# ------------------------------------------------------------ wings config
note 'Wings node config'
if sudo tar -C /etc/pelican -czf "$OUT/etc-pelican.tgz" . 2>/dev/null \
   && sudo test -s "$OUT/etc-pelican.tgz"; then
  sudo chown "$(id -u):$(id -g)" "$OUT/etc-pelican.tgz"
  ok '  etc-pelican.tgz'
else
  fail '  /etc/pelican'
fi

# -------------------------------------------------------------- minecraft
note 'Minecraft world'
MC_DIR="/var/lib/pelican/volumes/${MC_UUID}"
if sudo test -d "$MC_DIR"; then
  # Quiesce through RCON if it is running, so the world is not tarred
  # mid-write. If it is not running there is nothing to quiesce.
  MC_UP=$(docker inspect -f '{{.State.Running}}' "$MC_UUID" 2>/dev/null || echo false)
  if [ "$MC_UP" = true ] && [ -x "${STACK}/bootstrap/mc-rcon.py" ]; then
    python3 "${STACK}/bootstrap/mc-rcon.py" 'save-off'        >/dev/null 2>&1
    python3 "${STACK}/bootstrap/mc-rcon.py" 'save-all flush'  >/dev/null 2>&1
    sleep 3
    note '  quiesced via RCON (nobody kicked)'
  fi
  sudo tar -C "$MC_DIR" -czf "$OUT/minecraft-world.tgz" \
       --exclude='./logs' --exclude='./crash-reports' world 2>/dev/null \
    && sudo chown "$(id -u):$(id -g)" "$OUT/minecraft-world.tgz" \
    && ok "  minecraft-world.tgz ($(du -h "$OUT/minecraft-world.tgz" | cut -f1))" \
    || fail '  Minecraft world'
  [ "$MC_UP" = true ] && python3 "${STACK}/bootstrap/mc-rcon.py" 'save-on' >/dev/null 2>&1
else
  note '  no Minecraft server directory -- skipping'
fi

# ------------------------------------------------------- schematic library
# The viewer is the only process that writes this volume. Stop just that
# private sidecar for a consistent archive; the :8093 UI stays up and reports
# the library unavailable for the few seconds this takes. Minecraft and Wings
# are unrelated and remain untouched.
note 'schematic library'
SCHEMATIC_VOLUME='lantern-schematic-viewer-data'
if docker volume inspect "$SCHEMATIC_VOLUME" >/dev/null 2>&1; then
  SCHEMATIC_VIEWER_CONTAINER=$(docker compose ps -q schematic-viewer 2>/dev/null || true)
  if [ -n "$SCHEMATIC_VIEWER_CONTAINER" ] \
     && [ "$(docker inspect -f '{{.State.Running}}' "$SCHEMATIC_VIEWER_CONTAINER" 2>/dev/null)" = true ]; then
    if docker compose stop schematic-viewer >/dev/null 2>&1; then
      SCHEMATIC_VIEWER_WAS_RUNNING=true
      note '  schematic-viewer stopped; Minecraft, Wings, and :8093 remain up'
    else
      fail '  schematic-viewer could not be stopped; refusing a live volume copy'
    fi
  fi

  if [ "$SCHEMATIC_VIEWER_WAS_RUNNING" = true ] || [ -z "$SCHEMATIC_VIEWER_CONTAINER" ] \
     || [ "$(docker inspect -f '{{.State.Running}}' "$SCHEMATIC_VIEWER_CONTAINER" 2>/dev/null)" != true ]; then
    if docker run --rm -v "$SCHEMATIC_VOLUME":/v:ro alpine:3.20 \
       tar -C /v -czf - . 2>/dev/null > "$OUT/schematic-viewer-data.tgz" \
       && [ -s "$OUT/schematic-viewer-data.tgz" ]; then
      ok "  schematic-viewer-data.tgz ($(du -h "$OUT/schematic-viewer-data.tgz" | cut -f1))"
    else
      fail '  schematic library volume'
    fi
  fi
  restart_schematic_viewer
else
  note "  no $SCHEMATIC_VOLUME -- skipping (expected before the release gate)"
fi

# --------------------------------------------------------------------- cs2
note 'CS2 config and plugins'
CS2_CFG="/var/lib/pelican/volumes/${CS2_UUID}/game/csgo"
if sudo test -d "$CS2_CFG"; then
  sudo tar -C "$CS2_CFG" -czf "$OUT/cs2-config.tgz" cfg addons 2>/dev/null \
    && sudo chown "$(id -u):$(id -g)" "$OUT/cs2-config.tgz" \
    && ok "  cs2-config.tgz ($(du -h "$OUT/cs2-config.tgz" | cut -f1))" \
    || fail '  CS2 cfg/addons'
else
  note '  no CS2 server directory -- skipping'
fi

# ----------------------------------------------------------------- stardew
note 'Stardew saves and config'
for pair in "lantern-stardew_saves:stardew-saves" "lantern-stardew_config:stardew-config"; do
  vol="${pair%%:*}"; name="${pair##*:}"
  docker volume inspect "$vol" >/dev/null 2>&1 || { note "  no $vol -- skipping"; continue; }
  if docker run --rm -v "$vol":/v:ro alpine:3.20 tar -C /v -czf - . 2>/dev/null > "$OUT/${name}.tgz" \
     && [ -s "$OUT/${name}.tgz" ]; then
    ok "  ${name}.tgz ($(du -h "$OUT/${name}.tgz" | cut -f1))"
  else
    fail "  $vol"
  fi
done

# ------------------------------------------------------------------ config
# Gitignored, so these exist nowhere else. Mode 600: they hold every password
# in the stack.
note 'secrets and config'
CONFIG_PATHS=(stack/.env stack/.weaponpaints-db ui/.env stardew/.env stardew/settings stardew/mods)
if [ -d /opt/lantern/stack/secrets ]; then
  CONFIG_PATHS+=(stack/secrets)
else
  note '  no stack/secrets yet -- expected before the Minecraft UI release gate'
fi
if tar -C /opt/lantern -czf "$OUT/config.tgz" "${CONFIG_PATHS[@]}" 2>/dev/null; then
  chmod 600 "$OUT/config.tgz"
  ok '  config.tgz (mode 600)'
else
  fail '  config files'
fi

# ------------------------------------------------------------------ finish
{
  echo "taken:    $(date -u +%FT%TZ)"
  echo "host:     $(hostname)"
  echo "commit:   $(git -C /opt/lantern rev-parse --short HEAD 2>/dev/null || echo unknown)"
  echo "failures: ${FAILED}"
  echo
  ls -lh "$OUT" | tail -n +2
} > "$OUT/MANIFEST.txt"

step 'Pruning'
mapfile -t old < <(ls -1d "${DEST}"/*/ 2>/dev/null | sort | head -n -"$KEEP")
if [ "${#old[@]}" -gt 0 ]; then
  note "removing ${#old[@]} set(s) older than the last ${KEEP}"
  sudo rm -rf "${old[@]}"
else
  note "keeping all sets (limit ${KEEP})"
fi

printf '\n'
TOTAL=$(du -sh "$OUT" | cut -f1)
if [ "$FAILED" -gt 0 ]; then
  bad "${FAILED} component(s) failed. ${OUT} is INCOMPLETE (${TOTAL})."
  exit 1
fi
ok "backup complete: ${OUT} (${TOTAL})"
