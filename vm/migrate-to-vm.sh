#!/usr/bin/env bash
# Move LANtern from Docker-inside-WSL onto the VM built by build-lantern-vm.sh.
#
# HISTORICAL. This ran once, on 2026-08-26, and cannot run again: it reads the
# source stack through WSL's Docker, and WSL was unregistered when the
# hypervisor was disabled. The hardcoded /mnt/c path below is correct for what
# this did -- it is the Windows checkout as WSL saw it -- and is not a bug to
# fix. Kept because it documents exactly what was moved and what was left.
#
# WHAT ACTUALLY HAS TO MOVE
#
# Most of the 69 GB on disk is CS2 game content that SteamCMD would happily
# download again. What cannot be downloaded again is small and scattered:
#
#   stack_pelican-db          228 MB  panel database AND cs2_weaponpaints,
#                                     which is where every loadout lives
#   stack_pelican-data         20 kB  panel data + plugins
#   lantern-stardew_saves     3.2 MB  the farm
#   lantern-stardew_config    1.4 MB  SMAPI/game config (see note below)
#   lantern-stardew_steam-session      Steam token cache
#   /etc/pelican               28 kB  Wings node token -- without it the panel
#                                     cannot re-adopt its own node
#   /var/lib/pelican/wings.db  16 kB  which servers exist, and their UUIDs
#   .../volumes/<mc-uuid>     1.6 GB  the Minecraft world
#   .../volumes/<cs2>/csgo/cfg+addons 524 MB  CounterStrikeSharp, WeaponPaints
#   the repo working tree, including six gitignored .env files
#
# THE STARDEW /config VOLUME
#
# It used to be anonymous -- the image declares /config as a VOLUME and compose
# never named it. This script reads it by container ID on the way out and
# writes it to the now-named `lantern-stardew_config` on the way in. Run this
# BEFORE any `compose down -v` on the old host or that config is simply gone.
#
# COPY EVERYTHING BY DEFAULT
#
# --minimal skips CS2's ~67 GB of game content and Stardew's game install and
# expects you to reinstall them on the VM. That sounds thrifty and usually is
# not: reinstalling CS2 means a 40 GB pull from Steam, and a Pelican reinstall
# wipes the server directory first -- so the addons and cfg have to be restored
# on top afterwards, in the right order, or the loadout plugin comes back
# unconfigured. Copying across the LAN is faster and has no such ordering.
#
# Idempotent per phase. Run one phase at a time while debugging:
#   ./migrate-to-vm.sh repo
#   ./migrate-to-vm.sh volumes
#
# Run this from WSL.

set -uo pipefail

VM_IP="${VM_IP:-192.168.0.116}"
VM_USER="${VM_USER:-iverson}"
KEY="${KEY:-$HOME/.ssh/lantern_vm}"
SRC="${SRC:-/mnt/c/Users/iveri/Documents/code/lantern}"
DST="${DST:-/opt/lantern}"
HELPER="${HELPER:-alpine:3.20}"

CS2_UUID=a530efc8-3095-4030-8ddb-c82c1d5c56fb
MC_UUID=2be9425c-1141-4181-b0a0-34f38d84fb7f

MINIMAL=0
PHASES=(stop repo volumes pelican verify)
ARGS=()
for a in "$@"; do
  case "$a" in
    --minimal) MINIMAL=1 ;;
    -*)        echo "unknown option: $a" >&2; exit 1 ;;
    *)         ARGS+=("$a") ;;
  esac
done
[ "${#ARGS[@]}" -gt 0 ] && PHASES=("${ARGS[@]}")

ok()   { printf '  \033[32m%s\033[0m\n' "$*"; }
bad()  { printf '  \033[31m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
step() { printf '\n\033[36m%s\033[0m\n' "$*"; }
die()  { bad "$*"; exit 1; }

SSH_OPTS=(-i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
          -o LogLevel=ERROR -o ConnectTimeout=10 -o Compression=no)
vm() { ssh "${SSH_OPTS[@]}" "$VM_USER@$VM_IP" "$@"; }

# Reads a path that root owns without needing sudo on this side: the container
# runs as root and can see whatever is bind-mounted into it. WSL has no
# passwordless sudo, so this is the only way to get at /var/lib/pelican.
pull_path() {  # pull_path <host-dir> <relative-path-inside>
  docker run --rm -v "$1":/src:ro "$HELPER" \
    tar -C /src --numeric-owner -cf - "$2" 2>/dev/null
}

pull_volume() {  # pull_volume <volume-name-or-id>
  docker run --rm -v "$1":/v:ro "$HELPER" \
    tar -C /v --numeric-owner -cf - . 2>/dev/null
}

push_volume() {  # push_volume <target-volume-name>
  vm "docker volume create '$1' >/dev/null && \
      docker run --rm -i -v '$1':/v $HELPER tar -C /v --numeric-owner -xf -"
}

human() { numfmt --to=iec --suffix=B "${1:-0}" 2>/dev/null || echo "$1 bytes"; }

want() { printf '%s\n' "${PHASES[@]}" | grep -qx "$1"; }

# --------------------------------------------------------------- preflight
step 'Preflight'

grep -qi microsoft /proc/version || die 'Run this from WSL (the source stack lives here).'
[ -d "$SRC" ] || die "source repo not found: $SRC"
docker info >/dev/null 2>&1 || die 'the local Docker daemon is not answering'
ok 'local Docker answers'

[ -f "$KEY" ] || die "SSH key not found: $KEY"
vm true 2>/dev/null || die "cannot SSH to $VM_USER@$VM_IP -- is the VM up?"
ok "SSH to $VM_IP works"

vm 'docker info >/dev/null 2>&1' || die 'Docker on the VM is not answering'
ok "Docker on the VM: $(vm 'docker --version' 2>/dev/null)"

vm "docker image inspect $HELPER >/dev/null 2>&1 || docker pull -q $HELPER >/dev/null" \
  || die "could not get $HELPER onto the VM"
docker image inspect "$HELPER" >/dev/null 2>&1 || docker pull -q "$HELPER" >/dev/null
ok "helper image $HELPER present on both sides"

FREE_KB=$(vm "df -Pk $DST 2>/dev/null || df -Pk /" 2>/dev/null | awk 'NR==2{print $4}')
note "VM free space: $(human $((FREE_KB * 1024)))"

# ------------------------------------------------------------------- stop
if want stop; then
  step 'Stopping the local stack'
  # A MariaDB volume copied while the server is running is a copy of a database
  # mid-write. Stop first; this is a migration, not a backup.
  for d in stack stardew; do
    if [ -f "$SRC/$d/compose.yml" ]; then
      ( cd "$SRC/$d" && docker compose stop >/dev/null 2>&1 )
      ok "$d stopped"
    fi
  done
  RUNNING=$(docker ps --format '{{.Names}}' | grep -cE '^(stack|sdvd)-' || true)
  [ "$RUNNING" = 0 ] && ok 'no LANtern containers running' \
                     || bad "$RUNNING LANtern containers still running"
fi

# ------------------------------------------------------------------- repo
if want repo; then
  step 'Repo and secrets'
  vm "mkdir -p $DST"
  # .scotland-login is deliberately excluded: that password was exposed and has
  # to be reissued on the VM rather than carried over.
  tar -C "$SRC" --numeric-owner -cf - \
      --exclude='**/.venv' --exclude='**/__pycache__' --exclude='**/node_modules' \
      --exclude='stack/startup.log' --exclude='stack/.scotland-login' \
      . 2>/dev/null \
    | vm "tar -C $DST --numeric-owner -xf -" \
    || die 'repo copy failed'

  # The Windows working tree is CRLF, and on Linux a CR is part of the token:
  # shebangs become "bad interpreter" and DB_PASSWORD gains a trailing \r that
  # makes authentication fail with a correct-looking password.
  note 'normalising line endings'
  vm "bash $DST/vm/normalize-line-endings.sh $DST 2>&1 | tail -4" || die 'normalisation failed'

  MISSING=0
  for f in stack/.env stack/.weaponpaints-db ui/.env stardew/.env \
           stack/compose.yml stardew/compose.yml; do
    if vm "test -s $DST/$f" 2>/dev/null; then
      ok "$f"
    else
      bad "$f MISSING on the VM"; MISSING=1
    fi
  done
  [ "$MISSING" = 0 ] || die 'secrets did not arrive -- check .gitignore did not shadow them'
fi

# ---------------------------------------------------------------- volumes
if want volumes; then
  step 'Docker volumes'

  # The Stardew /config volume was anonymous. Find it by the mount it provides
  # rather than by name, because it has no name.
  ANON_CONFIG=$(docker inspect sdvd-server 2>/dev/null \
    | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit()
for m in d[0].get("Mounts",[]):
    if m.get("Destination")=="/config" and m.get("Type")=="volume":
        print(m.get("Name",""))
' 2>/dev/null | head -1)

  MAP=(
    "stack_pelican-db:stack_pelican-db"
    "stack_pelican-data:stack_pelican-data"
    "lantern-stardew_saves:lantern-stardew_saves"
    "lantern-stardew_steam-session:lantern-stardew_steam-session"
  )
  [ "$MINIMAL" = 1 ] || MAP+=("lantern-stardew_game-data:lantern-stardew_game-data")
  if [ -n "$ANON_CONFIG" ]; then
    MAP+=("$ANON_CONFIG:lantern-stardew_config")
    note "Stardew /config was anonymous (${ANON_CONFIG:0:12}...) -> lantern-stardew_config"
  else
    bad 'could not find the anonymous /config volume -- sdvd-server may be gone.'
    bad 'SMAPI config will come back at defaults on the VM.'
  fi

  for pair in "${MAP[@]}"; do
    from="${pair%%:*}"; to="${pair##*:}"
    if ! docker volume inspect "$from" >/dev/null 2>&1; then
      bad "source volume $from does not exist -- skipping"; continue
    fi
    note "$from -> $to"
    pull_volume "$from" | push_volume "$to" || { bad "  copy failed"; continue; }
    n=$(vm "docker run --rm -v '$to':/v:ro $HELPER sh -c 'find /v | wc -l'" 2>/dev/null)
    ok "  $to now holds $n entries"
  done
fi

# ---------------------------------------------------------------- pelican
if want pelican; then
  step 'Wings state and server directories'

  vm 'sudo mkdir -p /etc/pelican /var/lib/pelican/volumes /var/log/pelican /tmp/pelican'

  note '/etc/pelican  (node token -- the panel cannot re-adopt the node without it)'
  pull_path /etc/pelican . | vm 'sudo tar -C /etc/pelican --numeric-owner -xf -' \
    || die '/etc/pelican copy failed'
  vm 'sudo test -s /etc/pelican/config.yml' \
    && ok '  config.yml restored' || die '  config.yml did not arrive'

  note 'wings.db and states.json'
  pull_path /var/lib/pelican './wings.db' \
    | vm 'sudo tar -C /var/lib/pelican --numeric-owner -xf -' 2>/dev/null
  pull_path /var/lib/pelican './states.json' \
    | vm 'sudo tar -C /var/lib/pelican --numeric-owner -xf -' 2>/dev/null
  vm 'sudo test -s /var/lib/pelican/wings.db' && ok '  wings.db restored' \
                                              || bad '  wings.db did not arrive'

  note "Minecraft server $MC_UUID"
  pull_path /var/lib/pelican/volumes "./$MC_UUID" \
    | vm 'sudo tar -C /var/lib/pelican/volumes --numeric-owner -xf -' \
    || bad '  Minecraft copy failed'

  if [ "$MINIMAL" = 1 ]; then
    note "CS2 config and addons only (game content will be reinstalled)"
    vm "sudo mkdir -p /var/lib/pelican/volumes/$CS2_UUID/game/csgo"
    for sub in cfg addons; do
      pull_path "/var/lib/pelican/volumes/$CS2_UUID/game/csgo" "./$sub" \
        | vm "sudo tar -C /var/lib/pelican/volumes/$CS2_UUID/game/csgo --numeric-owner -xf -" \
        || bad "  csgo/$sub copy failed"
    done
    bad '  CS2 game content NOT copied. After reinstalling from the panel, re-run:'
    bad "    ./migrate-to-vm.sh pelican      (restores cfg and addons on top)"
  else
    note "CS2 server $CS2_UUID (~67 GB, this is the slow one)"
    pull_path /var/lib/pelican/volumes "./$CS2_UUID" \
      | vm 'sudo tar -C /var/lib/pelican/volumes --numeric-owner -xf -' \
      || bad '  CS2 copy failed'
  fi

  vm 'sudo chown -R root:root /etc/pelican /var/lib/pelican' 2>/dev/null
  ok 'ownership reset'
fi

# ----------------------------------------------------------------- verify
if want verify; then
  step 'Verify'
  FAIL=0

  # Assert that nothing was LOST, not that the two sides are identical. Once the
  # target database has been started it legitimately has files the source does
  # not -- ibtmp1 and ddl_recovery.log are created at startup -- and a strict
  # equality check reports that healthy state as a failed migration.
  for v in stack_pelican-db stack_pelican-data lantern-stardew_saves \
           lantern-stardew_config lantern-stardew_steam-session; do
    docker volume inspect "$v" >/dev/null 2>&1 || continue
    a=$(mktemp); b=$(mktemp)
    docker run --rm -v "$v":/v:ro "$HELPER" sh -c 'find /v -type f | sort' >"$a" 2>/dev/null
    vm "docker run --rm -v '$v':/v:ro $HELPER sh -c 'find /v -type f | sort'" >"$b" 2>/dev/null
    missing=$(comm -23 "$a" "$b" | wc -l)
    extra=$(comm -13 "$a" "$b" | wc -l)
    if [ "$missing" -eq 0 ] && [ -s "$a" ]; then
      if [ "$extra" -gt 0 ]; then
        ok "$v: all $(wc -l <"$a") files arrived (+$extra created by the running service)"
      else
        ok "$v: all $(wc -l <"$a") files arrived"
      fi
    else
      bad "$v: $missing files did not arrive"
      comm -23 "$a" "$b" | head -5 | sed 's/^/      /'
      FAIL=1
    fi
    rm -f "$a" "$b"
  done

  # The server directories are bind mounts, not volumes, and root owns them.
  for pair in "CS2:$CS2_UUID" "Minecraft:$MC_UUID"; do
    label="${pair%%:*}"; uuid="${pair##*:}"
    s=$(docker run --rm -v /var/lib/pelican/volumes:/v:ro "$HELPER" \
          sh -c "find /v/$uuid -type f 2>/dev/null | wc -l")
    t=$(vm "sudo find /var/lib/pelican/volumes/$uuid -type f 2>/dev/null | wc -l")
    if [ -n "$s" ] && [ "$s" -gt 0 ] && [ "$t" -ge "$s" ]; then
      ok "$label: $t files on the VM (source has $s)"
    else
      bad "$label: $s files here, $t there"; FAIL=1
    fi
  done

  vm 'sudo test -s /etc/pelican/config.yml' && ok 'wings config present' \
    || { bad 'wings config missing'; FAIL=1; }

  MC=$(vm "sudo du -sm /var/lib/pelican/volumes/$MC_UUID 2>/dev/null | cut -f1")
  CS=$(vm "sudo du -sm /var/lib/pelican/volumes/$CS2_UUID 2>/dev/null | cut -f1")
  note "on disk: Minecraft ${MC:-0} MB, CS2 ${CS:-0} MB"

  printf '\n'
  if [ "$FAIL" != 0 ]; then
    bad 'Migration incomplete. Nothing on the old host has been deleted --'
    bad 'fix the failures above and re-run the affected phase.'
    exit 1
  fi

  printf '  Data is on the VM. Next, and only then:\n\n'
  printf '    1. Cut the address over  ./cutover.sh\n'
  printf '    2. On the VM:            cd %s/stack && docker compose up -d\n' "$DST"
  printf '    3. Reissue the panel account whose password was exposed\n'
  printf '    4. Leave the old stack stopped but intact until a real LAN test passes\n\n'
fi
