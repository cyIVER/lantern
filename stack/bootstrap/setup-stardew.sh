#!/usr/bin/env bash
# Setup and validation for the Stardew Valley server.
#
#   bash bootstrap/setup-stardew.sh            # pull, start, validate
#   bash bootstrap/setup-stardew.sh --validate # validate only
#
# Run from stack/, inside Ubuntu WSL.
#
# ---------------------------------------------------------------------------
# One step this script will never do for you
#
# JunimoServer downloads Stardew Valley from Steam using your account, because
# Valve does not allow anonymous downloads of paid titles. That first login is
# interactive and prompts for a Steam Guard code:
#
#     cd ../stardew && docker compose run --rm -it steam-auth setup
#
# You type it. Not this script, and not any agent working on this repository.
# The script detects whether it has happened and stops with instructions if not.
# ---------------------------------------------------------------------------
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
SDV="../stardew"
VALIDATE_ONLY=0
[ "${1:-}" = "--validate" ] && VALIDATE_ONLY=1

PASS=0; FAIL=0
ok()    { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASS=$((PASS+1)); }
bad()   { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); }
warn()  { printf '  \033[33mWARN\033[0m  %s\n' "$*"; }
info()  { printf '  ....  %s\n' "$*"; }
head_() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

dc() { (cd "$SDV" && docker compose "$@"); }

# --------------------------------------------------------------- 0. preflight
head_ "Preflight"

if [ ! -f "$SDV/.env" ]; then
  bad "stardew/.env does not exist"
  printf '\n  cp stardew/.env.example stardew/.env\n'
  printf '  then fill in STEAM_USERNAME, STEAM_PASSWORD and VNC_PASSWORD.\n'
  printf '  See docs/SECRETS.md.\n\n'
  exit 1
fi
ok "stardew/.env exists"

# Read without sourcing: .env values are quoted and may contain anything.
envval() { grep -E "^${1}=" "$SDV/.env" | tail -1 | cut -d= -f2- | tr -d '"'"'"'' | tr -d '\r'; }

missing=()
[ -n "$(envval VNC_PASSWORD)" ] || missing+=(VNC_PASSWORD)
if [ -z "$(envval STEAM_REFRESH_TOKEN)" ]; then
  [ -n "$(envval STEAM_USERNAME)" ] || missing+=(STEAM_USERNAME)
  [ -n "$(envval STEAM_PASSWORD)" ] || missing+=(STEAM_PASSWORD)
fi
if [ "${#missing[@]}" -gt 0 ]; then
  bad "unset in stardew/.env: ${missing[*]}"
  exit 1
fi
ok "required values are set"

[ -n "$(envval API_KEY)" ] || warn "API_KEY is empty -- the HTTP API's write endpoints are unprotected"

if [ -n "$(envval STEAM_REFRESH_TOKEN)" ] && [ -n "$(envval STEAM_PASSWORD)" ]; then
  warn "both STEAM_PASSWORD and STEAM_REFRESH_TOKEN are set -- blank the password, the token is enough"
fi

# The port remaps that stop this colliding with the Pelican stack.
for pair in "QUERY_PORT:27015:CS2" "API_PORT:8080:Wings"; do
  IFS=: read -r var forbidden owner <<< "$pair"
  if [ "$(envval "$var")" = "$forbidden" ]; then
    bad "$var is $forbidden, which $owner already owns. Change it in stardew/.env."
  fi
done
[ "$FAIL" -gt 0 ] && exit 1
ok "no port collisions with the Pelican stack"

# ------------------------------------------------------------------ 1. images
if [ "$VALIDATE_ONLY" = "0" ]; then
  head_ "Images"
  info "pulling (about 650 MB on a first run)"
  if dc pull --quiet server steam-auth >/tmp/sdvpull.log 2>&1; then
    ok "images pulled: $(dc config --images 2>/dev/null | tr '\n' ' ')"
  else
    bad "pull failed"; tail -10 /tmp/sdvpull.log | sed 's/^/        /'; exit 1
  fi
fi

# -------------------------------------------------------- 2. has Steam logged in?
head_ "Steam"

# The session volume is only populated by a successful interactive login. Its
# absence is the difference between "not set up yet" and "broken".
have_session=0
if docker volume inspect lantern-stardew_steam-session >/dev/null 2>&1; then
  n=$(docker run --rm -v lantern-stardew_steam-session:/s alpine sh -c 'ls -A /s 2>/dev/null | wc -l' 2>/dev/null)
  [ "${n:-0}" -gt 0 ] && have_session=1
fi

if [ "$have_session" = "0" ]; then
  bad "Steam has never authenticated -- the game cannot be downloaded yet"
  cat <<'STEP'

  ─────────────────────────────────────────────────────────────────────
  This one step is yours. It is interactive and asks for a Steam Guard
  code, and no script or agent should be typing your Steam credentials.

      cd stardew
      docker compose run --rm -it steam-auth setup

  Then harden it -- swap the password for a download-only token:

      docker compose run --rm steam-auth export-token

  Paste the token into STEAM_REFRESH_TOKEN in stardew/.env and blank
  STEAM_PASSWORD. Re-run this script afterwards.
  ─────────────────────────────────────────────────────────────────────

STEP
  exit 1
fi
ok "Steam session exists (login has been completed)"

# ------------------------------------------------------------------- 3. start
if [ "$VALIDATE_ONLY" = "0" ]; then
  head_ "Start"
  if dc up -d >/tmp/sdvup.log 2>&1; then
    ok "containers started"
  else
    bad "compose up failed"; tail -15 /tmp/sdvup.log | sed 's/^/        /'; exit 1
  fi
  info "first boot downloads the game from Steam -- several minutes"
fi

# -------------------------------------------------------------- 4. validation
head_ "Validation"

for c in sdvd-steam-auth sdvd-server; do
  st=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo absent)
  if [ "$st" = "running" ]; then ok "$c is running"; else bad "$c is $st"; fi
done
[ "$FAIL" -gt 0 ] && { head_ "Result"; printf '  %d passed, %d failed\n\n  dc logs: cd stardew && docker compose logs --tail 50\n\n' "$PASS" "$FAIL"; exit 1; }

# Did the game itself actually download? An empty game-data volume means the
# Steam step reported success but fetched nothing.
# Assert on the artifact, not on a size threshold. A complete English-only
# Stardew is about 257 MB -- localized content is stripped unless
# STEAM_KEEP_LANGUAGES is set -- so any megabyte floor generous enough to be
# meaningful would also fail a perfectly good install.
gsize=$(docker run --rm -v lantern-stardew_game-data:/g alpine du -sm /g 2>/dev/null | cut -f1)
if docker run --rm -v lantern-stardew_game-data:/g alpine test -f "/g/Stardew Valley.dll" 2>/dev/null; then
  nfiles=$(docker run --rm -v lantern-stardew_game-data:/g alpine sh -c 'find /g/Content -type f 2>/dev/null | wc -l')
  ok "game downloaded (${gsize} MB, ${nfiles} content files)"
else
  bad "no 'Stardew Valley.dll' in game-data (${gsize:-0} MB) -- the download is incomplete"
fi

# Wait for the server to actually come up rather than assuming.
API_PORT=$(envval API_PORT); API_PORT="${API_PORT:-8091}"
VNC_PORT=$(envval VNC_PORT); VNC_PORT="${VNC_PORT:-5800}"

API_KEY_V=$(envval API_KEY)
# /health is deliberately unauthenticated. Every other route returns 401
# without the bearer token, which reads as "not ready yet" if you forget it.
AUTH=()
[ -n "$API_KEY_V" ] && AUTH=(-H "Authorization: Bearer ${API_KEY_V}")

info "waiting for the HTTP API on ${API_PORT}"
api_up=0
for _ in $(seq 1 60); do
  if curl -sf --max-time 4 "http://127.0.0.1:${API_PORT}/health" -o /tmp/sdvhealth.json 2>/dev/null; then
    api_up=1; break
  fi
  sleep 5
done

if [ "$api_up" = "1" ]; then
  # /health reports whether the game loop is actually ticking, which is a far
  # better liveness signal than "a socket accepted a connection".
  detail=$(python3 -c "import json;d=json.load(open('/tmp/sdvhealth.json'));print('game=%s ticks=%s frozen=%s' % (d.get('gameAvailable'),d.get('tickCount'),d.get('isFrozen')))" 2>/dev/null || cat /tmp/sdvhealth.json)
  ok "HTTP API healthy: ${detail}"
else
  bad "no HTTP API response on ${API_PORT} after 5 minutes"
fi

if timeout 5 bash -c "cat < /dev/null > /dev/tcp/127.0.0.1/${VNC_PORT}" 2>/dev/null; then
  ok "VNC console listening on ${VNC_PORT}"
else
  bad "nothing listening on ${VNC_PORT}"
fi

# An invite code exists only once the save has loaded and the Galaxy lobby is
# open, so asking for one is the real "joinable" test -- much better than
# grepping the log for a word that might never be printed.
code=""
for _ in $(seq 1 24); do
  code=$(curl -sf --max-time 5 "${AUTH[@]}" "http://127.0.0.1:${API_PORT}/status" 2>/dev/null \
         | python3 -c "import json,sys;print(json.load(sys.stdin).get('steamInviteCode',''))" 2>/dev/null)
  [ -n "$code" ] && break
  sleep 5
done

if [ -n "$code" ]; then
  counts=$(curl -sf --max-time 5 "${AUTH[@]}" "http://127.0.0.1:${API_PORT}/status" \
    | python3 -c "import json,sys;d=json.load(sys.stdin);print('%s/%s' % (d.get('playerCount',0),d.get('maxPlayers',0)))" 2>/dev/null)
  ok "farm is open -- invite code ${code}, ${counts} players"
else
  bad "no invite code after 2 minutes -- the farm has not finished loading"
  docker logs sdvd-server --tail 5 2>&1 | tr -d '\r' | cut -c1-120 | sed 's/^/        /'
fi

mods=$(find "$SDV/mods" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l)
info "${mods} mod folder(s) staged in stardew/mods"

# ------------------------------------------------------------------- verdict
head_ "Result"
printf '  %d passed, %d failed\n' "$PASS" "$FAIL"
if [ "$FAIL" = "0" ]; then
  ip=$(grep -oE '^APP_URL=https?://[0-9.]+' .env 2>/dev/null | grep -oE '[0-9.]+$' || echo '<host-ip>')
  printf '\n  VNC console   http://%s:%s\n' "$ip" "$VNC_PORT"
  printf '  HTTP API      http://%s:%s\n' "$ip" "$API_PORT"
  printf '  Invite code   \033[1m%s\033[0m   (changes on restart)\n' "${code:-see the VNC console}"
  printf '  See docs/STARDEW.md\n\n'
  exit 0
fi
printf '\n  cd stardew && docker compose logs --tail 60\n\n'
exit 1
