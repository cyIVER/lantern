#!/usr/bin/env bash
# Stage a single CounterStrikeSharp plugin into an already-installed server,
# without triggering a full reinstall (which would re-download ~66 GB).
#
#   bash bootstrap/fetch-missing-plugin.sh <repo> <asset-regex> <staging-dir>
#
# e.g. bash bootstrap/fetch-missing-plugin.sh \
#         CHR15cs/CS2-Practice-Plugin '^Linux\.Release.*\.zip$' practice
set -euo pipefail

REPO="${1:?repo required}"
# Plain substring, not a regex: this string passes through PowerShell -> wsl ->
# bash -lc -> grep, and backslash escapes do not survive that intact.
PATTERN="${2:?asset filename substring required}"
DEST="${3:?staging dir name required}"

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." || exit 1
UUID=$(docker compose exec -T panel php artisan tinker \
  --execute='echo \App\Models\Server::where("name","LANtern CS2")->value("uuid");' 2>/dev/null \
  | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)
[ -n "$UUID" ] || { echo "could not resolve server uuid"; exit 1; }

# grep -F (fixed string) rather than jq or a regex: jq may not be present, and
# an escaped regex does not survive the PowerShell -> wsl -> bash quoting chain.
URL=$(curl -sSL "https://api.github.com/repos/${REPO}/releases/latest" \
      | grep -oE '"browser_download_url"[[:space:]]*:[[:space:]]*"[^"]+"' \
      | sed -E 's/.*"(https[^"]+)"/\1/' \
      | grep -F "$PATTERN" \
      | head -1)
[ -n "$URL" ] || { echo "no asset matching /$PATTERN/ in $REPO"; exit 1; }
echo "asset: $URL"

STAGE="game/csgo/addons/counterstrikesharp/plugins.available"

# Match ownership of the sibling plugin dirs so Wings/CSSharp can read them.
OWNER=$(docker run --rm -v /var/lib/pelican/volumes:/v alpine \
        sh -c "stat -c '%u:%g' /v/$UUID/$STAGE 2>/dev/null || echo 988:988")
echo "owner: $OWNER"

docker run --rm -v /var/lib/pelican/volumes:/v alpine sh -c "
  set -e
  apk add --no-cache curl unzip >/dev/null 2>&1
  mkdir -p /v/$UUID/$STAGE/$DEST
  curl -sSL -o /tmp/p.zip '$URL'
  unzip -oq /tmp/p.zip -d /v/$UUID/$STAGE/$DEST
  chown -R $OWNER /v/$UUID/$STAGE/$DEST
  echo 'staged:'
  ls -1 /v/$UUID/$STAGE/$DEST | sed 's/^/  /'
"

echo "--- all staged plugin sets now ---"
docker run --rm -v /var/lib/pelican/volumes:/v alpine \
  ls -1 "/v/$UUID/$STAGE" | sed 's/^/  /'
