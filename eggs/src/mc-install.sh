#!/bin/bash
# LANtern Minecraft installer -- CurseForge modpack server packs.
#
# Runs once, as root, in a throwaway container with the server's data directory
# mounted at /mnt/server. Everything here must be idempotent: Pelican reinstalls
# on demand and a half-finished install is worse than no install.
#
# Why the *server* pack and not the client one: CurseForge publishes two files
# per release. The client zip is a 192 MB manifest of 485 project/file IDs that
# a launcher then resolves one by one -- and seven of those mods block
# third-party downloads, so a manifest-driven install cannot complete
# unattended. The server pack is 1.1 GB with every jar already inside it.
set -euo pipefail

: "${CURSEFORGE_API_KEY:?CURSEFORGE_API_KEY is unset -- see docs/SECRETS.md}"
: "${CF_PROJECT_ID:?CF_PROJECT_ID is unset}"

API="https://api.curseforge.com/v1"
CURL=(curl -fsS --retry 3 --retry-delay 5
      -H "x-api-key: ${CURSEFORGE_API_KEY}" -H "Accept: application/json")

echo "[lantern] installing dependencies"
apt-get update -qq
apt-get install -y -qq --no-install-recommends curl unzip jq ca-certificates >/dev/null

# ---------------------------------------------------------------- resolve file
# CF_FILE_ID pins an exact pack version. Empty or "latest" tracks the newest
# stable release -- convenient for a first install, but pin it afterwards:
# an unpinned server that reinstalls onto a newer pack than your friends have
# locks every one of them out until they update too.
if [ -z "${CF_FILE_ID:-}" ] || [ "${CF_FILE_ID}" = "latest" ]; then
  echo "[lantern] resolving latest release for project ${CF_PROJECT_ID}"
  CF_FILE_ID=$("${CURL[@]}" "${API}/mods/${CF_PROJECT_ID}" \
    | jq -r '[.data.latestFiles[] | select(.releaseType == 1)] | last | .id')
  [ -n "${CF_FILE_ID}" ] && [ "${CF_FILE_ID}" != "null" ] \
    || { echo "[lantern] FATAL: could not resolve a release file"; exit 1; }
fi
echo "[lantern] client file id: ${CF_FILE_ID}"

meta=$("${CURL[@]}" "${API}/mods/${CF_PROJECT_ID}/files/${CF_FILE_ID}")
pack_name=$(echo "${meta}" | jq -r '.data.displayName')
server_id=$(echo "${meta}" | jq -r '.data.serverPackFileId // empty')

if [ -z "${server_id}" ] || [ "${server_id}" = "null" ]; then
  echo "[lantern] FATAL: '${pack_name}' publishes no server pack."
  echo "[lantern] This egg cannot install client-only packs unattended."
  exit 1
fi

server_meta=$("${CURL[@]}" "${API}/mods/${CF_PROJECT_ID}/files/${server_id}")
url=$(echo "${server_meta}" | jq -r '.data.downloadUrl // empty')
name=$(echo "${server_meta}" | jq -r '.data.fileName')
bytes=$(echo "${server_meta}" | jq -r '.data.fileLength')

[ -n "${url}" ] || { echo "[lantern] FATAL: server pack has no download URL"; exit 1; }

# The API hands out edge.forgecdn.net, which 404s on ranged requests and has
# been flaky on plain ones. mediafilez is what the 302 resolves to anyway.
url="${url/edge.forgecdn.net/mediafilez.forgecdn.net}"

echo "[lantern] pack   : ${pack_name}"
echo "[lantern] server : ${name} ($((bytes / 1048576)) MB)"

# ------------------------------------------------------------------- download
cd /mnt/server

# Download into /mnt/server, NOT /tmp. Wings mounts /tmp in its containers as a
# tmpfs capped at 100 MB by default, so a 1.1 GB download there dies at roughly
# 8% with the deeply unhelpful "curl: (23) Failure writing output to
# destination". /mnt/server is the server's real disk allocation.
ZIP=/mnt/server/.serverpack.zip
echo "[lantern] downloading to ${ZIP}"
curl -fL --retry 3 --retry-delay 5 --no-progress-meter -o "${ZIP}" "${url}"

got=$(stat -c%s "${ZIP}")
if [ "${got}" -ne "${bytes}" ]; then
  echo "[lantern] FATAL: size mismatch -- expected ${bytes}, got ${got}"
  echo "[lantern] (a short file usually means the disk allocation is too small)"
  rm -f "${ZIP}"
  exit 1
fi
echo "[lantern] size verified: ${got} bytes"

echo "[lantern] extracting"
unzip -q -o "${ZIP}" -d /mnt/server
rm -f "${ZIP}"

# Some packs nest everything one level down. Flatten it so the layout below is
# predictable regardless of how the author zipped it.
if [ ! -d /mnt/server/mods ]; then
  inner=$(find /mnt/server -maxdepth 2 -type d -name mods | head -1)
  if [ -n "${inner}" ]; then
    top=$(dirname "${inner}")
    echo "[lantern] flattening ${top}"
    shopt -s dotglob
    mv "${top}"/* /mnt/server/ 2>/dev/null || true
    shopt -u dotglob
    rmdir "${top}" 2>/dev/null || true
  fi
fi
[ -d /mnt/server/mods ] || { echo "[lantern] FATAL: no mods/ after extract"; exit 1; }

# ------------------------------------------------------- run the loader installer
# The server pack ships mods and configs but no libraries/ -- the loader has to
# be installed on this machine to generate them, along with the arg files the
# launch command reads.
installer=$(find /mnt/server -maxdepth 1 -name '*installer*.jar' | head -1)
if [ -n "${installer}" ]; then
  echo "[lantern] running $(basename "${installer}")"
  ( cd /mnt/server && java -jar "${installer}" --installServer >/mnt/server/.loader-install.log 2>&1 ) || {
    echo "[lantern] FATAL: loader install failed"; tail -30 /mnt/server/.loader-install.log; exit 1; }
  rm -f "${installer}" "${installer}.log" /mnt/server/.loader-install.log
  echo "[lantern] loader installed"
else
  echo "[lantern] no installer jar -- assuming libraries/ ships in the pack"
fi

args=$(find /mnt/server/libraries -name unix_args.txt 2>/dev/null | head -1)
[ -n "${args}" ] || { echo "[lantern] FATAL: no unix_args.txt -- loader did not install"; exit 1; }
echo "[lantern] launch args: ${args#/mnt/server/}"

# ---------------------------------------------------------------- server files
# Accepting the EULA on the operator's behalf is what every Minecraft egg does;
# creating the server at all is the act of agreement.
echo "eula=true" > /mnt/server/eula.txt

# Only seed server.properties if absent -- boot.sh owns the managed keys from
# here on, and clobbering this file would discard hand-edits between restarts.
if [ ! -f /mnt/server/server.properties ]; then
  cat > /mnt/server/server.properties <<'PROPS'
# Seeded at install. Keys LANtern manages are rewritten by boot.sh on start;
# anything else you set here is preserved.
enable-jmx-monitoring=false
level-name=world
allow-nether=true
enable-command-block=true
spawn-protection=0
allow-flight=true
sync-chunk-writes=false
PROPS
fi

# The pack's own wrappers assume an interactive terminal and re-run the
# installer on every launch. boot.sh replaces both.
rm -f /mnt/server/startserver.sh /mnt/server/startserver.bat /mnt/server/run.bat

mkdir -p /mnt/server/lantern
cat > /mnt/server/lantern/boot.sh <<'BOOTEOF'
__MC_BOOT_SCRIPT__
BOOTEOF
chmod +x /mnt/server/lantern/boot.sh

# Record exactly what was installed. The validator and the CI version check both
# read this, so "what is actually deployed" is never a guess.
cat > /mnt/server/lantern/installed.json <<JSON
{
  "project_id": ${CF_PROJECT_ID},
  "file_id": ${CF_FILE_ID},
  "server_file_id": ${server_id},
  "pack": $(echo "${meta}" | jq '.data.displayName'),
  "game_versions": $(echo "${meta}" | jq -c '.data.gameVersions'),
  "mods": $(find /mnt/server/mods -name '*.jar' | wc -l),
  "installed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON

echo "[lantern] ---------------------------------------------"
cat /mnt/server/lantern/installed.json
echo "[lantern] install complete"
