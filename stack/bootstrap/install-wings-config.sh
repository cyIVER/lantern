#!/usr/bin/env bash
set -euo pipefail
cd /mnt/c/Users/iveri/Documents/code/lantern/stack

TMP=$(mktemp)

# Regenerate from a freshly loaded model so uuid/token are populated.
docker compose exec -T panel php artisan tinker \
  --execute='echo \App\Models\Node::find(1)->fresh()->getYamlConfiguration();' 2>/dev/null \
  | sed -n '/^debug:/,$p' > "$TMP"

echo "=== generated config ($(wc -l < "$TMP") lines) ==="
sed -e 's/^\(  *token:\).*/\1 <redacted>/' -e 's/^\(token:\).*/\1 <redacted>/' \
    -e 's/^\(token_id:\).*/\1 <redacted>/' "$TMP"

if ! grep -q '^uuid: [0-9a-f]' "$TMP"; then
  echo "ABORT: uuid missing from generated config" >&2
  exit 1
fi

# /etc/pelican resolves into the Ubuntu WSL filesystem, and the daemon runs as
# root, so a throwaway container can write there without sudo.
docker run --rm -i -v /etc/pelican:/etc/pelican alpine \
  sh -c 'cat > /etc/pelican/config.yml && chmod 600 /etc/pelican/config.yml' < "$TMP"

echo "=== verify on disk ==="
docker run --rm -v /etc/pelican:/etc/pelican alpine \
  sh -c 'ls -l /etc/pelican/config.yml; echo "--- uuid line:"; grep "^uuid:" /etc/pelican/config.yml'

rm -f "$TMP"
