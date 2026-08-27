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
FAILURE_CODES=()
fail_code() {
  local code=$1
  shift
  FAILURE_CODES+=("$code")
  fail "$*"
}

MC_SAVES_DISABLED=false
MC_RESUME_FAILURE_RECORDED=false
MC_RCON_PASSWORD=
MC_RCON_PORT=
MC_BACKUP_STATUS=not_configured

minecraft_rcon() {
  printf '%s' "$MC_RCON_PASSWORD" \
    | python3 "${STACK}/bootstrap/mc-rcon.py" --password-stdin \
        127.0.0.1 "$MC_RCON_PORT" "$1" >/dev/null 2>&1
}

resume_minecraft_saves() {
  [ "$MC_SAVES_DISABLED" = true ] || return 0
  # Consume the recovery intent before attempting it: one authoritative
  # save-on result is reflected in status metadata, never a hidden EXIT retry.
  MC_SAVES_DISABLED=false
  if minecraft_rcon 'save-on'; then
    MC_RCON_PASSWORD=
    MC_RCON_PORT=
    note '  Minecraft saves re-enabled'
    return 0
  fi
  if [ "$MC_RESUME_FAILURE_RECORDED" = false ]; then
    fail_code minecraft.rcon_resume_failed \
      '  Minecraft saves could not be re-enabled; restart Minecraft before play resumes'
    MC_RESUME_FAILURE_RECORDED=true
  fi
  MC_BACKUP_STATUS=resume_failed
  return 1
}

# If the process is interrupted after the schematic sidecar is stopped, bring
# that sidecar back. This deliberately names only schematic-viewer: the
# Minecraft game, Wings, and the public Minecraft UI are never stopped here.
SCHEMATIC_VIEWER_WAS_RUNNING=false
SCHEMATIC_BACKUP_TMP=
SCHEMATIC_ARCHIVE_TMP=
SCHEMATIC_CHECKSUM_TMP=
SCHEMATIC_VIEWER_IMAGE='ghcr.io/scotsgamez/create-schematic-viewer:v1.0.1@sha256:d5501af9de95f9b89484ae4e4dbea098b0cdd3e86af3b19e50976855b533444c'
SCHEMATIC_VIEWER_UID=
SCHEMATIC_VIEWER_GID=
restart_schematic_viewer() {
  if [ "$SCHEMATIC_VIEWER_WAS_RUNNING" = true ]; then
    if docker compose start schematic-viewer >/dev/null 2>&1; then
      note '  schematic-viewer restarted'
    else
      fail '  schematic-viewer did not restart'
    fi
    SCHEMATIC_VIEWER_WAS_RUNNING=false
  fi
  if [ -n "$SCHEMATIC_BACKUP_TMP" ]; then
    # The typed backup runs as the viewer UID, which may differ from the host
    # operator, so cleanup retains the same sudo boundary used for staging.
    sudo rm -rf -- "$SCHEMATIC_BACKUP_TMP"
    SCHEMATIC_BACKUP_TMP=
  fi
  [ -n "$SCHEMATIC_ARCHIVE_TMP" ] && rm -f -- "$SCHEMATIC_ARCHIVE_TMP"
  [ -n "$SCHEMATIC_CHECKSUM_TMP" ] && rm -f -- "$SCHEMATIC_CHECKSUM_TMP"
  SCHEMATIC_ARCHIVE_TMP=
  SCHEMATIC_CHECKSUM_TMP=
}
finish_backup() {
  resume_minecraft_saves || true
  restart_schematic_viewer
}
trap finish_backup EXIT

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
  MC_BACKUP_ALLOWED=true
  if ! MC_UP=$(docker inspect -f '{{.State.Running}}' "$MC_UUID" 2>/dev/null) \
     || { [ "$MC_UP" != true ] && [ "$MC_UP" != false ]; }; then
    MC_BACKUP_ALLOWED=false
    MC_BACKUP_STATUS=state_unavailable
    fail_code minecraft.state_unavailable \
      '  Minecraft state could not be determined; refusing a potentially live archive'
  elif [ "$MC_UP" = true ]; then
    RCON_RECORD=$(docker compose exec -T panel php artisan tinker --execute="
      \$s = \\App\\Models\\Server::where('uuid','${MC_UUID}')->firstOrFail();
      \$e = [];
      foreach (\$s->variables as \$v) { \$e[\$v->env_variable] = \$v->server_value ?? \$v->default_value; }
      echo 'LANTERN_RCON_V1:'.base64_encode(\$e['RCON_PASSWORD'] ?? '').':'.(\$e['RCON_PORT'] ?? '');
    " 2>/dev/null | grep '^LANTERN_RCON_V1:' | tail -1)
    IFS=: read -r RCON_PREFIX RCON_ENCODED MC_RCON_PORT RCON_EXTRA <<< "$RCON_RECORD"
    if [ ! -x "${STACK}/bootstrap/mc-rcon.py" ] \
       || [ "$RCON_PREFIX" != LANTERN_RCON_V1 ] \
       || [ -n "$RCON_EXTRA" ] \
       || ! [[ "$RCON_ENCODED" =~ ^[A-Za-z0-9+/]+={0,2}$ ]] \
       || ! [[ "$MC_RCON_PORT" =~ ^[0-9]+$ ]] \
       || [ "$MC_RCON_PORT" -lt 1 ] \
       || [ "$MC_RCON_PORT" -gt 65535 ] \
       || ! MC_RCON_PASSWORD=$(printf '%s' "$RCON_ENCODED" | base64 --decode 2>/dev/null) \
       || [ -z "$MC_RCON_PASSWORD" ]; then
      MC_BACKUP_ALLOWED=false
      MC_BACKUP_STATUS=credentials_unavailable
      fail_code minecraft.rcon_credentials_unavailable \
        '  Minecraft RCON credentials are unavailable; refusing a live world archive'
    else
      # The request can reach Minecraft even when the response is lost. Record
      # recovery intent before transmission so an ambiguous error still gets
      # one authoritative save-on attempt.
      MC_SAVES_DISABLED=true
      if ! minecraft_rcon 'save-off'; then
        MC_BACKUP_ALLOWED=false
        MC_BACKUP_STATUS=quiesce_failed
        fail_code minecraft.rcon_quiesce_failed \
          '  Minecraft save-off failed; refusing a live world archive'
      elif ! minecraft_rcon 'save-all flush'; then
        MC_BACKUP_ALLOWED=false
        MC_BACKUP_STATUS=quiesce_failed
        fail_code minecraft.rcon_quiesce_failed \
          '  Minecraft save-all flush failed; refusing a live world archive'
        resume_minecraft_saves || true
      else
        sleep 3
        note '  quiesced via RCON (nobody kicked)'
      fi
    fi
    RCON_RECORD=
    RCON_ENCODED=
  fi

  if [ "$MC_BACKUP_ALLOWED" = true ] && [ "$MC_UP" = false ]; then
    if ! MC_RECHECK=$(docker inspect -f '{{.State.Running}}' "$MC_UUID" 2>/dev/null) \
       || [ "$MC_RECHECK" != false ]; then
      MC_BACKUP_ALLOWED=false
      MC_BACKUP_STATUS=state_changed
      fail_code minecraft.state_changed \
        '  Minecraft started before the offline archive; refusing the world backup'
    fi
  fi

  if [ "$MC_BACKUP_ALLOWED" = true ]; then
    if sudo tar -C "$MC_DIR" -czf "$OUT/minecraft-world.tgz" \
         --exclude='./logs' --exclude='./crash-reports' world 2>/dev/null \
       && sudo chown "$(id -u):$(id -g)" "$OUT/minecraft-world.tgz"; then
      if [ "$MC_UP" = false ] \
         && { ! MC_RECHECK=$(docker inspect -f '{{.State.Running}}' "$MC_UUID" 2>/dev/null) \
              || [ "$MC_RECHECK" != false ]; }; then
        rm -f -- "$OUT/minecraft-world.tgz"
        MC_BACKUP_STATUS=state_changed
        fail_code minecraft.state_changed \
          '  Minecraft state changed during the offline archive; discarding it'
      elif [ "$MC_UP" = true ]; then
        MC_BACKUP_STATUS=quiesced_consistent
        ok "  minecraft-world.tgz ($(du -h "$OUT/minecraft-world.tgz" | cut -f1))"
      else
        MC_BACKUP_STATUS=offline_consistent
        ok "  minecraft-world.tgz ($(du -h "$OUT/minecraft-world.tgz" | cut -f1))"
      fi
    else
      rm -f -- "$OUT/minecraft-world.tgz"
      MC_BACKUP_STATUS=archive_failed
      fail_code minecraft.archive_failed '  Minecraft world archive failed'
    fi
  else
    rm -f -- "$OUT/minecraft-world.tgz"
  fi
  resume_minecraft_saves || true
else
  MC_BACKUP_STATUS=missing
  fail_code minecraft.world_missing \
    '  Minecraft server directory is missing; refusing a worldless restore set'
fi

# ------------------------------------------------------- schematic library
# The viewer is the only process that writes this volume. Stop just that
# private sidecar for a consistent archive; the :8093 UI stays up and reports
# the library unavailable for the few seconds this takes. Minecraft and Wings
# are unrelated and remain untouched.
note 'schematic library'
SCHEMATIC_VOLUME='lantern-schematic-viewer-data'
if docker volume inspect "$SCHEMATIC_VOLUME" >/dev/null 2>&1; then
  SCHEMATIC_VIEWER_CONTAINER=
  SCHEMATIC_VIEWER_RUNNING=
  SCHEMATIC_VIEWER_STATE_KNOWN=false
  if SCHEMATIC_VIEWER_CONTAINER=$(docker compose ps -q schematic-viewer 2>/dev/null); then
    if [ -z "$SCHEMATIC_VIEWER_CONTAINER" ]; then
      SCHEMATIC_VIEWER_STATE_KNOWN=true
      SCHEMATIC_VIEWER_RUNNING=false
    elif SCHEMATIC_VIEWER_RUNNING=$(docker inspect -f '{{.State.Running}}' "$SCHEMATIC_VIEWER_CONTAINER" 2>/dev/null) \
         && { [ "$SCHEMATIC_VIEWER_RUNNING" = true ] || [ "$SCHEMATIC_VIEWER_RUNNING" = false ]; }; then
      SCHEMATIC_VIEWER_STATE_KNOWN=true
    else
      fail '  schematic-viewer state could not be determined; refusing a live volume copy'
    fi
  else
    fail '  schematic-viewer container lookup failed; refusing a live volume copy'
  fi

  if [ "$SCHEMATIC_VIEWER_STATE_KNOWN" = true ]; then
    if viewer_identity=$(docker run --rm --network none --read-only --cap-drop ALL \
       --security-opt no-new-privileges --entrypoint sh "$SCHEMATIC_VIEWER_IMAGE" \
       -ec 'printf "%s:%s\n" "$(id -u)" "$(id -g)"'); then
      SCHEMATIC_VIEWER_UID=${viewer_identity%%:*}
      SCHEMATIC_VIEWER_GID=${viewer_identity##*:}
      case "$SCHEMATIC_VIEWER_UID:$SCHEMATIC_VIEWER_GID" in
        *[!0-9:]* | :* | *:)
          SCHEMATIC_VIEWER_STATE_KNOWN=false
          fail '  released viewer image has an invalid runtime identity'
          ;;
      esac
    else
      SCHEMATIC_VIEWER_STATE_KNOWN=false
      fail '  released viewer image runtime identity could not be determined'
    fi
  fi

  if [ "$SCHEMATIC_VIEWER_STATE_KNOWN" = true ] && [ "$SCHEMATIC_VIEWER_RUNNING" = true ]; then
      # Record restart intent before stop: EXIT recovery must also cover an
      # interrupt while Docker is waiting for the container's grace period.
      SCHEMATIC_VIEWER_WAS_RUNNING=true
      if docker compose stop schematic-viewer >/dev/null 2>&1; then
        SCHEMATIC_VIEWER_RUNNING=false
        note '  schematic-viewer stopped; Minecraft, Wings, and :8093 remain up'
      else
        # A failed stop may still have stopped the container. Prevent backup
        # and make the EXIT path perform an idempotent start either way.
        SCHEMATIC_VIEWER_STATE_KNOWN=false
        fail '  schematic-viewer could not be stopped; refusing a live volume copy'
      fi
  fi

  if [ "$SCHEMATIC_VIEWER_STATE_KNOWN" = true ] && [ "$SCHEMATIC_VIEWER_RUNNING" = false ]; then
    if ! SCHEMATIC_BACKUP_TMP=$(mktemp -d "$OUT/.schematic-viewer-backup.XXXXXX") \
       || ! SCHEMATIC_ARCHIVE_TMP=$(mktemp "$OUT/.schematic-viewer-data.XXXXXX.tgz") \
       || ! SCHEMATIC_CHECKSUM_TMP=$(mktemp "$OUT/.schematic-viewer-data.XXXXXX.sha256"); then
      fail '  could not allocate schematic-library backup staging files'
      restart_schematic_viewer
    elif ! sudo chown "$SCHEMATIC_VIEWER_UID:$SCHEMATIC_VIEWER_GID" "$SCHEMATIC_BACKUP_TMP"; then
      fail '  could not grant the released viewer access to backup staging'
      restart_schematic_viewer
    elif docker run --rm --network none --read-only --cap-drop ALL \
       --security-opt no-new-privileges --user "$SCHEMATIC_VIEWER_UID:$SCHEMATIC_VIEWER_GID" \
       --env DATA_DIR=/data \
       --volume "$SCHEMATIC_VOLUME":/data:ro \
       --volume "$SCHEMATIC_BACKUP_TMP":/backup \
       "$SCHEMATIC_VIEWER_IMAGE" \
       node tools/library_data.js backup /backup/library >/dev/null \
       && sudo tar -C "$SCHEMATIC_BACKUP_TMP/library" -czf "$SCHEMATIC_ARCHIVE_TMP" . \
       && sudo chown "$(id -u):$(id -g)" "$SCHEMATIC_ARCHIVE_TMP" \
       && [ -s "$SCHEMATIC_ARCHIVE_TMP" ]; then
      if archive_digest=$(sha256sum "$SCHEMATIC_ARCHIVE_TMP" | awk '{print $1}') \
         && [ -n "$archive_digest" ] \
         && printf '%s  schematic-viewer-data.tgz\n' "$archive_digest" > "$SCHEMATIC_CHECKSUM_TMP" \
         && chmod 600 "$SCHEMATIC_ARCHIVE_TMP" "$SCHEMATIC_CHECKSUM_TMP" \
         && mv -- "$SCHEMATIC_ARCHIVE_TMP" "$OUT/schematic-viewer-data.tgz" \
         && { SCHEMATIC_ARCHIVE_TMP=; mv -- "$SCHEMATIC_CHECKSUM_TMP" "$OUT/schematic-viewer-data.tgz.sha256"; }; then
        SCHEMATIC_CHECKSUM_TMP=
        ok "  schematic-viewer-data.tgz ($(du -h "$OUT/schematic-viewer-data.tgz" | cut -f1))"
      else
        rm -f -- "$OUT/schematic-viewer-data.tgz" "$OUT/schematic-viewer-data.tgz.sha256"
        fail '  schematic library archive publication'
      fi
    else
      rm -f -- "$OUT/schematic-viewer-data.tgz" "$OUT/schematic-viewer-data.tgz.sha256"
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
if [ "$FAILED" -gt 0 ]; then
  BACKUP_STATUS=incomplete
else
  BACKUP_STATUS=complete
fi
if python3 - "$OUT/BACKUP_STATUS.json" "$STAMP" "$BACKUP_STATUS" "$FAILED" \
  "$MC_BACKUP_STATUS" "${FAILURE_CODES[@]}" <<'PY'
import json
from pathlib import Path
import sys

path, backup_id, status, failure_count, minecraft_world, *failure_codes = sys.argv[1:]
payload = {
    "schema": 1,
    "event": "backup.completed",
    "backup_id": backup_id,
    "status": status,
    "failure_count": int(failure_count),
    "failure_codes": failure_codes,
    "components": {"minecraft_world": minecraft_world},
}
Path(path).write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
PY
then
  if ! chmod 600 "$OUT/BACKUP_STATUS.json"; then
    rm -f -- "$OUT/BACKUP_STATUS.json"
    fail_code backup.status_metadata_failed '  backup status permissions could not be secured'
  fi
else
  rm -f -- "$OUT/BACKUP_STATUS.json"
  fail_code backup.status_metadata_failed '  backup status metadata could not be written'
fi

{
  echo "taken:    $(date -u +%FT%TZ)"
  echo "host:     $(hostname)"
  echo "commit:   $(git -C /opt/lantern rev-parse --short HEAD 2>/dev/null || echo unknown)"
  echo "failures: ${FAILED}"
  echo
  ls -lh "$OUT" | tail -n +2
} > "$OUT/MANIFEST.txt"

step 'Pruning'
if [ "$FAILED" -gt 0 ]; then
  note 'current set is incomplete; retaining every prior set'
else
  complete_sets=()
  while IFS= read -r directory; do
    if python3 - "$directory/BACKUP_STATUS.json" <<'PY'
import json
from pathlib import Path
import sys

try:
    status = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
backup_id = Path(sys.argv[1]).parent.name
valid_minecraft = status.get("components", {}).get("minecraft_world") in {
    "offline_consistent",
    "quiesced_consistent",
}
raise SystemExit(
    0
    if status.get("schema") == 1
    and status.get("event") == "backup.completed"
    and status.get("backup_id") == backup_id
    and status.get("status") == "complete"
    and status.get("failure_count") == 0
    and status.get("failure_codes") == []
    and valid_minecraft
    else 1
)
PY
    then
      complete_sets+=("$directory")
    fi
  done < <(ls -1d "${DEST}"/*/ 2>/dev/null | sort)
  old=()
  if [ "${#complete_sets[@]}" -gt "$KEEP" ]; then
    old=("${complete_sets[@]:0:${#complete_sets[@]}-KEEP}")
  fi
  if [ "${#old[@]}" -gt 0 ]; then
    note "removing ${#old[@]} verified complete set(s) beyond the last ${KEEP}"
    sudo rm -rf "${old[@]}"
  else
    note "keeping all verified complete sets (limit ${KEEP}); incomplete and legacy sets are untouched"
  fi
fi

printf '\n'
TOTAL=$(du -sh "$OUT" | cut -f1)
if [ "$FAILED" -gt 0 ]; then
  bad "${FAILED} component(s) failed. ${OUT} is INCOMPLETE (${TOTAL})."
  exit 1
fi
ok "backup complete: ${OUT} (${TOTAL})"
