#!/usr/bin/env bash
# Reinstall the plugin PLATFORM -- Metamod:Source and CounterStrikeSharp -- into
# the live CS2 server. Plugins themselves are left untouched.
#
# Needed when Metamod fails with:
#   MMS: Fatal error: Detected engine NN but could not load:
#     metamod.2.cs2.so: undefined symbol: ...
#
# or CounterStrikeSharp with:
#   [META] Failed to load counterstrikesharp.so: undefined symbol: ...
#
# Two causes, both fixed by this:
#   1. A plugin archive overwrote them with stale bundled copies.
#      CS2-Practice-Plugin ships BOTH its own addons/metamod and a complete
#      CounterStrikeSharp (bin/, dotnet/, api/, source/). normalize-plugins.sh
#      now strips those, but an already-damaged install still needs repairing.
#   2. CS2 updated past the installed builds and newer ones have since shipped.
#
# Also re-applies the gameinfo.gi patch, which steamcmd 'validate' reverts.
set -euo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." || exit 1

UUID=$(docker compose exec -T panel php artisan tinker \
  --execute='echo \App\Models\Server::where("name","LANtern CS2")->value("uuid");' 2>/dev/null \
  | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)
[ -n "$UUID" ] || { echo "could not resolve server uuid"; exit 1; }
VOL=/var/lib/pelican/volumes
CSGO="/v/$UUID/game/csgo"

MM_FILE=$(curl -sSL https://mms.alliedmods.net/mmsdrop/2.0/mmsource-latest-linux)
[ -n "$MM_FILE" ] || { echo "could not resolve the latest Metamod build"; exit 1; }
echo "latest Metamod: $MM_FILE"

echo "--- current binary ---"
docker run --rm -v "$VOL":/v alpine \
  sh -c "ls -l --full-time $CSGO/addons/metamod/bin/linuxsteamrt64/metamod.2.cs2.so 2>/dev/null || echo '  (none)'"

TMP=$(mktemp)
curl -sSL -o "$TMP" "https://mms.alliedmods.net/mmsdrop/2.0/${MM_FILE}"
echo "downloaded $(wc -c < "$TMP") bytes"

OWNER=$(docker run --rm -v "$VOL":/v alpine stat -c '%u:%g' "$CSGO/addons")

# addons/metamod also holds counterstrikesharp.vdf -- the hook that makes Metamod
# load CSSharp. It is NOT in the Metamod tarball, so it must survive the replace
# or CSSharp silently never loads. Preserve every .vdf across the swap.
docker run --rm -i -v "$VOL":/v alpine sh -c "
  set -e
  mkdir -p /tmp/vdf
  cp $CSGO/addons/metamod/*.vdf /tmp/vdf/ 2>/dev/null || true
  rm -rf $CSGO/addons/metamod
  cat > /tmp/mm.tar.gz
  tar -xzf /tmp/mm.tar.gz -C $CSGO
  cp /tmp/vdf/*.vdf $CSGO/addons/metamod/ 2>/dev/null || true
  chown -R $OWNER $CSGO/addons/metamod
  echo '--- installed ---'
  ls -l --full-time $CSGO/addons/metamod/bin/linuxsteamrt64/metamod.2.cs2.so
  echo '--- vdf files kept ---'
  ls $CSGO/addons/metamod/*.vdf 2>/dev/null || echo '  (none)'
" < "$TMP"
rm -f "$TMP"

# If the hook is absent (first repair, or it was already lost), pull it from the
# CounterStrikeSharp release rather than hand-writing a VDF.
if ! docker run --rm -v "$VOL":/v alpine test -f "$CSGO/addons/metamod/counterstrikesharp.vdf"; then
  echo "--- counterstrikesharp.vdf missing; restoring from the CSSharp release ---"
  CSS_URL=$(curl -sSL https://api.github.com/repos/roflmuffin/CounterStrikeSharp/releases/latest \
    | grep -oE '"browser_download_url"[^"]*"[^"]+"' | sed -E 's/.*"(https[^"]+)"/\1/' \
    | grep 'with-runtime-linux' | head -1)
  echo "    $CSS_URL"
  CTMP=$(mktemp)
  curl -sSL -o "$CTMP" "$CSS_URL"
  docker run --rm -i -v "$VOL":/v alpine sh -c "
    set -e
    apk add --no-cache unzip >/dev/null 2>&1
    cat > /tmp/css.zip
    unzip -oq /tmp/css.zip 'addons/metamod/*' -d /tmp/cssx
    cp /tmp/cssx/addons/metamod/*.vdf $CSGO/addons/metamod/
    chown $OWNER $CSGO/addons/metamod/*.vdf
    ls $CSGO/addons/metamod/*.vdf
  " < "$CTMP"
  rm -f "$CTMP"
fi

echo "--- CounterStrikeSharp's metamod hook still present? ---"
docker run --rm -v "$VOL":/v alpine sh -c "
  ls $CSGO/addons/metamod/counterstrikesharp.vdf 2>/dev/null && echo '  yes' \
    || echo '  MISSING -- CSSharp will not load; reinstall the server or re-extract CSSharp'
"

# ---------------------------------------------------------- CounterStrikeSharp
echo "--- reinstalling CounterStrikeSharp ---"
docker run --rm -v "$VOL":/v alpine   sh -c "ls -l --full-time $CSGO/addons/counterstrikesharp/bin/linuxsteamrt64/counterstrikesharp.so 2>/dev/null || echo '  (none)'"

CSS_URL=$(curl -sSL https://api.github.com/repos/roflmuffin/CounterStrikeSharp/releases/latest   | grep -oE '"browser_download_url"[^"]*"[^"]+"' | sed -E 's/.*"(https[^"]+)"//'   | grep 'with-runtime-linux' | head -1)
[ -n "$CSS_URL" ] || { echo "could not resolve the CounterStrikeSharp release"; exit 1; }
echo "  $CSS_URL"

STMP=$(mktemp)
curl -sSL -o "$STMP" "$CSS_URL"

# Unzipping over the top refreshes bin/, dotnet/, api/ and the metamod vdf while
# leaving plugins/, configs/ and plugins.available/ alone.
docker run --rm -i -v "$VOL":/v alpine sh -c "
  set -e
  apk add --no-cache unzip >/dev/null 2>&1
  cat > /tmp/css.zip
  unzip -oq /tmp/css.zip -d $CSGO
  chown -R $OWNER $CSGO/addons/counterstrikesharp $CSGO/addons/metamod
  echo '--- installed ---'
  ls -l --full-time $CSGO/addons/counterstrikesharp/bin/linuxsteamrt64/counterstrikesharp.so
" < "$STMP"
rm -f "$STMP"

echo "--- gameinfo.gi patched for Metamod? ---"
docker run --rm -v "$VOL":/v alpine sh -c "
  grep -q 'csgo/addons/metamod' $CSGO/gameinfo.gi && echo '  yes' \
    || echo '  no -- boot.sh re-applies this on next start'
"

echo
echo "Restart the server, then confirm with:"
echo "  docker logs <uuid> 2>&1 | grep -ai 'MMS:\\|Finished loading plugin'"
