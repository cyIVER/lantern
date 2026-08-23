#!/bin/bash
# LANtern CS2 install script.
#
# Extends the stock Counter-Strike 2 egg (steamcmd base) with Metamod:Source,
# CounterStrikeSharp and the LANtern plugin set. Plugins are staged into
# plugins.available/ and the boot script activates only the ones the selected
# MODE needs -- MatchZy, Retakes and PracticeMode each take over round flow and
# break each other if loaded together.
#
# Server files: /mnt/server   (this becomes /home/container at runtime)
set -o pipefail

SRV=/mnt/server
CSGO="$SRV/game/csgo"
ADDONS="$CSGO/addons"
STAGE="$ADDONS/counterstrikesharp/plugins.available"

say() { echo -e "\n\033[1m[LANtern] $*\033[0m"; }

# ---------------------------------------------------------------- prerequisites
say "Installing prerequisites"
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq curl tar unzip jq ca-certificates >/dev/null 2>&1
command -v unzip >/dev/null || { echo "FATAL: unzip unavailable"; exit 1; }

# ------------------------------------------------------------------- base game
if [[ "${STEAM_USER}" == "" ]] || [[ "${STEAM_PASS}" == "" ]]; then
    say "Using anonymous Steam login"
    STEAM_USER=anonymous; STEAM_PASS=""; STEAM_AUTH=""
fi

say "Installing steamcmd"
cd /tmp
mkdir -p "$SRV/steamcmd" "$SRV/steamapps"
curl -sSL -o steamcmd.tar.gz https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz
tar -xzf steamcmd.tar.gz -C "$SRV/steamcmd"
cd "$SRV/steamcmd"
chown -R root:root /mnt
export HOME=$SRV

say "Downloading Counter-Strike 2 (app ${SRCDS_APPID:-730}) -- this is ~35 GB, be patient"
./steamcmd.sh +force_install_dir "$SRV" +login "$STEAM_USER" $STEAM_PASS $STEAM_AUTH \
    +app_update "${SRCDS_APPID:-730}" $EXTRA_FLAGS validate +quit

if [ ! -f "$CSGO/gameinfo.gi" ]; then
    echo "FATAL: game files missing after steamcmd (no $CSGO/gameinfo.gi)"; exit 1
fi

say "Setting up Steam client libraries"
mkdir -p "$SRV/.steam/sdk32" "$SRV/.steam/sdk64"
cp -f linux32/steamclient.so "$SRV/.steam/sdk32/steamclient.so"
cp -f linux64/steamclient.so "$SRV/.steam/sdk64/steamclient.so"

# -------------------------------------------------------------- Metamod:Source
say "Installing Metamod:Source"
MM_FILE=$(curl -sSL https://mms.alliedmods.net/mmsdrop/2.0/mmsource-latest-linux)
if [ -z "$MM_FILE" ]; then echo "FATAL: could not resolve Metamod latest build"; exit 1; fi
echo "  build: $MM_FILE"
curl -sSL -o /tmp/mm.tar.gz "https://mms.alliedmods.net/mmsdrop/2.0/${MM_FILE}"
mkdir -p "$ADDONS"
tar -xzf /tmp/mm.tar.gz -C "$CSGO"      # tarball already contains addons/metamod
[ -d "$ADDONS/metamod" ] || { echo "FATAL: metamod did not extract"; exit 1; }

# --------------------------------------------------------- CounterStrikeSharp
# 'with-runtime' bundles .NET -- the steamcmd runtime image has none, and the
# plain build fails to load with no useful error.
say "Installing CounterStrikeSharp (with .NET runtime)"
CSS_URL=$(curl -sSL https://api.github.com/repos/roflmuffin/CounterStrikeSharp/releases/latest \
  | jq -r '.assets[] | select(.name | test("with-runtime-linux")) | .browser_download_url' | head -1)
if [ -z "$CSS_URL" ]; then echo "FATAL: could not resolve CounterStrikeSharp release"; exit 1; fi
echo "  $CSS_URL"
curl -sSL -o /tmp/css.zip "$CSS_URL"
unzip -oq /tmp/css.zip -d "$CSGO"        # contains addons/counterstrikesharp + metamod vdf
[ -d "$ADDONS/counterstrikesharp" ] || { echo "FATAL: CounterStrikeSharp did not extract"; exit 1; }

# ------------------------------------------------------------------- plugins
mkdir -p "$STAGE"

# Fetch a GitHub release asset matching a regex, unzip into a staging subdir.
fetch_plugin() {
    local repo="$1" pattern="$2" dest="$3"
    say "Installing $repo"
    local url
    url=$(curl -sSL "https://api.github.com/repos/${repo}/releases/latest" \
          | jq -r --arg p "$pattern" '.assets[] | select(.name | test($p)) | .browser_download_url' | head -1)
    if [ -z "$url" ]; then
        echo "  WARNING: no asset matching /$pattern/ for $repo -- skipping"
        return 0
    fi
    echo "  $url"
    curl -sSL -o /tmp/p.zip "$url" || { echo "  WARNING: download failed"; return 0; }
    mkdir -p "$STAGE/$dest"
    unzip -oq /tmp/p.zip -d "$STAGE/$dest" || { echo "  WARNING: unzip failed"; return 0; }
    rm -f /tmp/p.zip
}

#            repo                          asset regex                    staging dir
fetch_plugin "shobhit-pathak/MatchZy"      '^MatchZy-[0-9.]+\.zip$'        matchzy

# CS2-SimpleAdmin's core plugin will not load without this chain:
#   CS2-SimpleAdmin -> MenuManagerCS2 -> PlayerSettingsCS2 -> AnyBaseLibCS2
# Omit any of them and you get a FileNotFoundException for MenuManagerApi while
# the submodules load fine, which makes it look like SimpleAdmin half-works.
fetch_plugin "NickFox007/AnyBaseLibCS2"    '^AnyBaseLib\.zip$'             anybaselib
fetch_plugin "NickFox007/PlayerSettingsCS2" '^PlayerSettings\.zip$'        playersettings
fetch_plugin "NickFox007/MenuManagerCS2"   '^MenuManager\.zip$'            menumanager
fetch_plugin "daffyyyy/CS2-SimpleAdmin"    '^CS2-SimpleAdmin.*\.zip$'      simpleadmin
fetch_plugin "Nereziel/cs2-WeaponPaints"   '^WeaponPaints\.zip$'           weaponpaints
fetch_plugin "CHR15cs/CS2-Practice-Plugin" '^Linux\.Release.*\.zip$'       practice
fetch_plugin "B3none/cs2-retakes"          '^RetakesPlugin-[0-9.]+\.zip$'  retakes

say "Staged plugin sets"
ls -1 "$STAGE" 2>/dev/null | sed 's/^/  /'

# Each project ships a differently-shaped archive. Normalise them all into
# csgo/-rooted overlays so boot.sh can activate a set with a plain copy.
say "Normalising plugin layouts"
cat > /tmp/normalize-plugins.sh <<'NORMEOF'
__NORMALIZE_SCRIPT__
NORMEOF
bash /tmp/normalize-plugins.sh "$STAGE"

# ----------------------------------------------------------------- boot script
# Written at install time, executed on every start. Lives outside addons/ so a
# steamcmd 'validate' cannot remove it.
say "Installing LANtern boot script"
mkdir -p "$SRV/lantern"
cat > "$SRV/lantern/boot.sh" <<'BOOTEOF'
__BOOT_SCRIPT__
BOOTEOF
chmod +x "$SRV/lantern/boot.sh"

say "Installation complete"
