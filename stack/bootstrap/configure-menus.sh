#!/usr/bin/env bash
# Two menu fixes:
#
#   1. MenuManagerCore defaults to "ButtonMenu", navigated with W/S and selected
#      with E. Players reasonably expect to type a number, press nothing, and see
#      it work -- so the menu appeared broken. Switch to ChatMenu.
#
#   2. PlayerSettings ships with an empty DatabaseParams block (host
#      127.0.0.1:3306, blank user). MenuManager depends on PlayerSettings, so
#      menu state has nowhere to persist. Point it at the same MariaDB the
#      skins use, with its own table prefix.
#
#   3. CounterStrikeSharp defaults FollowCS2ServerGuidelines to true, which
#      blocks writes to econ item properties:
#
#        Cannot set or get 'CEconItemView::m_iEntityQuality' with
#        "FollowCS2ServerGuidelines" option enabled
#
#      WeaponPaints needs those to apply a paint. The knife MODEL still applies
#      (different mechanism), so the symptom is a correct knife with no skin and
#      no in-game error -- the exception only appears in the server log.
set -euo pipefail
cd /mnt/c/Users/iveri/Documents/code/lantern/stack

CREDS=.weaponpaints-db
[ -f "$CREDS" ] || { echo "missing $CREDS -- run setup-weaponpaints-db.sh first"; exit 1; }
# shellcheck disable=SC1090
. "./$CREDS"

UUID=$(docker compose exec -T panel php artisan tinker \
  --execute='echo \App\Models\Server::where("name","LANtern CS2")->value("uuid");' 2>/dev/null \
  | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)
[ -n "$UUID" ] || { echo "could not resolve server uuid"; exit 1; }

VOL=/var/lib/pelican/volumes
CFGDIR="game/csgo/addons/counterstrikesharp/configs/plugins"

patch_json() {   # <relative config path> <python snippet operating on `cfg`>
  local rel="$1" snippet="$2" tmp owner
  tmp=$(mktemp)
  docker run --rm -v "$VOL":/v alpine cat "/v/$UUID/$rel" > "$tmp" 2>/dev/null || {
    echo "  skip: $rel not present"; rm -f "$tmp"; return 0; }
  owner=$(docker run --rm -v "$VOL":/v alpine stat -c '%u:%g' "/v/$UUID/$rel")

  python3 - "$tmp" "$snippet" <<'PY'
import json, sys
path, snippet = sys.argv[1], sys.argv[2]
# CounterStrikeSharp writes JSONC: a leading '//' header above the object.
with open(path, encoding='utf-8-sig') as f:
    raw = f.read()
lines = raw.splitlines(keepends=True)
header, body = [], lines
for i, line in enumerate(lines):
    if line.lstrip().startswith('//'):
        header.append(line)
    else:
        body = lines[i:]
        break
cfg = json.loads(''.join(body))
exec(snippet, {'cfg': cfg, 'os': __import__('os')})
with open(path, 'w', encoding='utf-8') as f:
    f.writelines(header)
    json.dump(cfg, f, indent=2)
    f.write('\n')
PY

  docker run --rm -i -v "$VOL":/v alpine \
    sh -c "cat > /v/$UUID/$rel && chown $owner /v/$UUID/$rel" < "$tmp"
  rm -f "$tmp"
  echo "  patched: $rel"
}

echo "=== 1. MenuManagerCore -> ChatMenu ==="
patch_json "$CFGDIR/MenuManagerCore/MenuManagerCore.json" \
  "cfg['DefaultMenu'] = 'ChatMenu'"

echo "=== 2. PlayerSettings database ==="
export PS_HOST="$DB_HOST:$DB_PORT" PS_NAME="$DB_NAME" PS_USER="$DB_USER" PS_PASS="$DB_PASS"
patch_json "$CFGDIR/PlayerSettings/PlayerSettings.json" \
  "cfg['DatabaseParams'] = {'Host': os.environ['PS_HOST'], 'Name': os.environ['PS_NAME'], 'User': os.environ['PS_USER'], 'Password': os.environ['PS_PASS'], 'Table': 'settings_'}"

echo "=== 3. CounterStrikeSharp core: allow econ item writes ==="
patch_json "game/csgo/addons/counterstrikesharp/configs/core.json"   "cfg['FollowCS2ServerGuidelines'] = False"

echo
echo "=== result (passwords masked) ==="
docker run --rm -v "$VOL":/v alpine sh -c "
  grep -i defaultmenu /v/$UUID/$CFGDIR/MenuManagerCore/MenuManagerCore.json
  sed -E 's/(\"Password\": \")[^\"]*\"/\1<set>\"/' /v/$UUID/$CFGDIR/PlayerSettings/PlayerSettings.json | grep -A6 DatabaseParams
"
echo
echo "Restart the server to apply."
