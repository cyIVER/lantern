#!/usr/bin/env bash
# Push eggs/src/boot.sh into the live CS2 server without reinstalling.
#
# boot.sh is written into the server volume at install time, so editing the egg
# alone does not affect an existing server -- and reinstalling would re-download
# ~66 GB. This copies the current version in and re-runs the normaliser.
#
#   bash bootstrap/push-boot-script.sh
set -euo pipefail

# Locate the repo from this script's own resolved path rather than a fixed
# one. The absolute /mnt/c path this used to carry does not exist on the VM.
REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd)"
cd "$REPO/stack" || exit 1

UUID=$(docker compose exec -T panel php artisan tinker \
  --execute='echo \App\Models\Server::where("name","LANtern CS2")->value("uuid");' 2>/dev/null \
  | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)
[ -n "$UUID" ] || { echo "could not resolve server uuid"; exit 1; }
echo "server: $UUID"

VOL=/var/lib/pelican/volumes
SRC="$REPO/eggs/src"
STAGE="game/csgo/addons/counterstrikesharp/plugins.available"

echo "--- installing boot.sh ---"
docker run --rm -v "$VOL":/v -v "$SRC":/src:ro alpine \
  sh -c "mkdir -p /v/$UUID/lantern && cp /src/boot.sh /v/$UUID/lantern/boot.sh && chmod +x /v/$UUID/lantern/boot.sh && ls -l /v/$UUID/lantern/boot.sh"

echo "--- re-running normaliser (idempotent) ---"
docker run --rm -v "$VOL":/v -v "$SRC":/src:ro bash:5 \
  bash /src/normalize-plugins.sh "/v/$UUID/$STAGE" 2>&1 | tail -12

echo "--- sanity: new overlay logic present? ---"
docker run --rm -v "$VOL":/v alpine \
  sh -c "grep -q 'lantern-plugins' /v/$UUID/lantern/boot.sh && echo '  yes' || echo '  NO -- copy failed'"
