#!/usr/bin/env bash
# One-shot, re-runnable setup for the LANtern Minecraft server.
#
#   bash bootstrap/setup-minecraft.sh            # full setup, then validate
#   bash bootstrap/setup-minecraft.sh --validate # validate only, change nothing
#
# Every step is idempotent, so re-running after a failure resumes rather than
# duplicating. Run from stack/, inside Ubuntu WSL -- not Git Bash, which mangles
# the Linux paths Docker Desktop needs (see docs/DECISIONS.md).
#
# ---------------------------------------------------------------------------
# What this script is willing to believe
#
# It asserts on artifacts and behaviour, never on the panel's status field.
# That is not paranoia. During development the install script died instantly
# with "Permission denied", Wings logged "completed installation process"
# anyway, and the panel reported the server ready with a completely empty data
# directory. A status field said yes; nothing had happened.
#
# So the install is done when the manifest it writes exists, the loader is
# installed when its arg file exists, and the server is up when it answers
# RCON. An earlier CS2 test script asserted only that directories existed, and
# passed while the platform was entirely dead.
# ---------------------------------------------------------------------------
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
REPO=".."
SERVER_NAME="LANtern Minecraft"
VALIDATE_ONLY=0
[ "${1:-}" = "--validate" ] && VALIDATE_ONLY=1

PASS=0; FAIL=0
ok()    { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASS=$((PASS+1)); }
bad()   { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); }
info()  { printf '  ....  %s\n' "$*"; }
head_() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

# Inline PHP. Argument order matters: --execute belongs to artisan, so it must
# come AFTER the service name. Placed before `panel` it is swallowed by docker
# compose as one of its own flags, the command silently emits nothing, and every
# check reading that output quietly "succeeds" against an empty string.
tinker() {
  docker compose exec -T panel php artisan tinker --execute="$1" 2>/dev/null \
    | grep -vE '^\s*$'
}

# Script files. Piping into tinker's stdin drives it as a REPL, which echoes
# each input line back behind a "> " prompt -- including the lines containing
# the success strings, so a failure reads as a pass. `require` skips the REPL.
run_php() {
  local script="$1"; shift
  docker compose cp "bootstrap/${script}" panel:/tmp/lantern-run.php >/dev/null 2>&1
  docker compose exec -T "$@" panel \
    php artisan tinker --execute='require "/tmp/lantern-run.php";' 2>/dev/null \
    | grep -vE '^\s*$'
}

inv()      { docker run --rm -v /var/lib/pelican/volumes:/v alpine "$@" 2>/dev/null; }
instlog()  { docker run --rm -v /var/log/pelican:/l alpine "$@" 2>/dev/null; }

# --------------------------------------------------------------- 0. preflight
head_ "Preflight"

if ! docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -q '^panel$'; then
  bad "the stack is not running -- 'docker compose up -d' first"; exit 1
fi
ok "panel and wings are up"

if [ ! -f .env ] || ! grep -q '^CURSEFORGE_API_KEY=' .env; then
  bad "CURSEFORGE_API_KEY missing from stack/.env -- see docs/SECRETS.md"; exit 1
fi
CF_KEY=$(grep '^CURSEFORGE_API_KEY=' .env | cut -d= -f2- | tr -d '"'"'"'' | tr -d '\r')

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
  -H "x-api-key: ${CF_KEY}" -H 'Accept: application/json' \
  'https://api.curseforge.com/v1/mods/925200')
if [ "$code" = "200" ]; then
  ok "CurseForge API key authenticates"
else
  bad "CurseForge API returned HTTP ${code} -- the key is wrong or revoked"; exit 1
fi

# --------------------------------------------------- 1. build and import egg
if [ "$VALIDATE_ONLY" = "0" ]; then
  head_ "Egg"

  if python3 "${REPO}/eggs/build-mc-egg.py" >/tmp/eggbuild.log 2>&1; then
    ok "egg rebuilt from src/mc-*.sh"
  else
    bad "egg build failed"; cat /tmp/eggbuild.log; exit 1
  fi

  docker compose cp "${REPO}/eggs/lantern-minecraft.json" panel:/tmp/egg.json >/dev/null 2>&1
  out=$(run_php import-egg.php -e EGG_FILE=/tmp/egg.json)
  echo "$out" | sed 's/^/        /'
  if echo "$out" | grep -qE '^(IMPORTED|UPDATED)  id=[0-9]+'; then
    ok "egg imported into the panel"
  else
    bad "egg import failed"; exit 1
  fi

  # ------------------------------------------------------------ 2. the server
  head_ "Server"
  out=$(run_php create-mc-server.php -e CURSEFORGE_API_KEY="${CF_KEY}")
  echo "$out" | sed 's/^/        /'
  if echo "$out" | grep -qE '^(CREATED|ALREADY EXISTS)  id=[0-9]+'; then
    ok "server record exists"
  else
    bad "server creation failed"; exit 1
  fi
fi

# ------------------------------------------------------- 3. read identifiers
UUID=$(tinker "echo \App\Models\Server::where('name','${SERVER_NAME}')->firstOrFail()->uuid;" \
       | tail -1 | tr -d '[:space:]')
if [ -z "${UUID}" ]; then
  head_ "Validation"; bad "could not read the server record from the panel"; exit 1
fi

read -r RCON_PW RCON_PORT MEM <<EOF
$(tinker "
  \$s = \App\Models\Server::where('name','${SERVER_NAME}')->firstOrFail();
  \$e = [];
  foreach (\$s->variables as \$v) { \$e[\$v->env_variable] = \$v->server_value ?? \$v->default_value; }
  echo (\$e['RCON_PASSWORD'] ?? '-').' '.(\$e['RCON_PORT'] ?? '25575').' '.\$s->memory;
" | tail -1)
EOF

# ------------------------------------------------------- 4. wait for install
if [ "$VALIDATE_ONLY" = "0" ]; then
  head_ "Install"
  info "downloading a 1.1 GB server pack and running the loader installer"
  info "several minutes on a first run; log at /var/log/pelican/install/${UUID}.log"

  for i in $(seq 1 120); do
    if inv test -f "/v/${UUID}/lantern/installed.json"; then
      ok "install finished (manifest written)"
      break
    fi

    # Wings reports success even when the install script exits non-zero, so the
    # log is the only honest signal that it died.
    if [ "$i" -gt 2 ] && instlog grep -qE 'Permission denied|command not found|FATAL:|No such file|curl: \([0-9]+\)|unzip:.*cannot find' "/l/install/${UUID}.log"; then
      bad "the install script failed (Wings reported success anyway)"
      instlog tail -20 "/l/install/${UUID}.log" | sed 's/^/        /'
      exit 1
    fi

    if [ $((i % 6)) -eq 0 ]; then
      info "still installing ($((i * 10))s, $(inv du -sh "/v/${UUID}" 2>/dev/null | cut -f1) on disk)"
    fi
    [ "$i" = "120" ] && { bad "install did not finish within 20 minutes"; exit 1; }
    sleep 10
  done
fi

# ------------------------------------------------------------- 5. validation
head_ "Validation"
info "uuid ${UUID}  memory ${MEM:-?} MiB"

if inv test -f "/v/${UUID}/lantern/installed.json"; then
  desc=$(inv cat "/v/${UUID}/lantern/installed.json" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["pack"], "| file", d["file_id"], "|", d["mods"], "mods")' 2>/dev/null)
  ok "install manifest: ${desc}"
else
  bad "no lantern/installed.json -- the install script never completed"
fi

args=$(inv sh -c "find /v/${UUID}/libraries -name unix_args.txt 2>/dev/null | head -1")
if [ -n "$args" ]; then
  ok "loader present: $(echo "$args" | cut -d/ -f7-8 | tr / ' ')"
else
  bad "no libraries/**/unix_args.txt -- the loader installer did not run"
fi

mods=$(inv sh -c "ls /v/${UUID}/mods/*.jar 2>/dev/null | wc -l")
if [ "${mods:-0}" -gt 400 ]; then
  ok "${mods} mod jars staged"
else
  bad "only ${mods:-0} mod jars -- expected 400+"
fi

# Nothing below can pass without the pack on disk, and trying would bury the
# real error under connection failures.
if [ "$FAIL" -gt 0 ]; then
  head_ "Result"
  printf '  %d passed, %d failed\n\n' "$PASS" "$FAIL"
  printf '  The install produced no usable server. Read the install log:\n'
  printf '    docker run --rm -v /var/log/pelican:/l alpine tail -40 /l/install/%s.log\n\n' "$UUID"
  exit 1
fi

state=$(docker inspect -f '{{.State.Status}}' "$UUID" 2>/dev/null || echo absent)
if [ "$state" != "running" ]; then
  info "starting the server"
  tinker "
    \$s = \App\Models\Server::where('name','${SERVER_NAME}')->firstOrFail();
    app(\App\Repositories\Daemon\DaemonServerRepository::class)->setServer(\$s)->power('start');
  " >/dev/null
fi

info "waiting for the world to load (a 485-mod pack takes 2-4 minutes)"
booted=0
for i in $(seq 1 72); do
  if docker logs "$UUID" --since 30m 2>&1 | grep -qa ')! For help, type'; then booted=1; break; fi
  if docker logs "$UUID" --since 30m 2>&1 | grep -qaE 'Exception in server tick loop|Failed to start the minecraft server|A fatal error has occurred'; then
    bad "the server crashed during startup"
    docker logs "$UUID" --since 30m 2>&1 | grep -aE 'Caused by|Exception|Error' | tail -6 | sed 's/^/        /'
    break
  fi
  [ $((i % 6)) -eq 0 ] && info "still loading ($((i * 5))s)"
  sleep 5
done

if [ "$booted" = "1" ]; then
  line=$(docker logs "$UUID" --since 30m 2>&1 | grep -a ')! For help, type' | tail -1 | tr -d '\r')
  ok "server finished loading -- ${line##*]: }"
elif [ "$FAIL" = "0" ]; then
  bad "server never printed the ready line within 6 minutes"
fi

if [ -n "${RCON_PW:-}" ] && [ "$RCON_PW" != "-" ]; then
  out=$(printf '%s' "$RCON_PW" \
    | python3 bootstrap/mc-rcon.py --password-stdin 127.0.0.1 "${RCON_PORT:-25575}" list 2>&1)
  if echo "$out" | grep -qi 'players online'; then
    ok "rcon answers: ${out}"
  else
    bad "rcon did not answer on ${RCON_PORT:-25575}: ${out}"
  fi
else
  bad "no RCON password set -- the control UI cannot reach this server"
fi

if timeout 5 bash -c 'cat < /dev/null > /dev/tcp/127.0.0.1/25565' 2>/dev/null; then
  ok "port 25565 is accepting connections"
else
  bad "nothing is listening on 25565"
fi

# ------------------------------------------------------------------- verdict
head_ "Result"
printf '  %d passed, %d failed\n' "$PASS" "$FAIL"
if [ "$FAIL" = "0" ]; then
  ip=$(grep -oE '^APP_URL=https?://[0-9.]+' .env | grep -oE '[0-9.]+$' || echo '<host-ip>')
  printf '\n  Friends connect to  \033[1m%s:25565\033[0m\n' "$ip"
  printf '  They need All the Mods 10 at the pinned version -- see docs/MINECRAFT.md\n\n'
  exit 0
fi
printf '\n  Useful next steps:\n'
printf '    docker logs %s --tail 100\n' "$UUID"
printf '    docker run --rm -v /var/log/pelican:/l alpine tail -40 /l/install/%s.log\n\n' "$UUID"
exit 1
