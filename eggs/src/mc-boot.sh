#!/bin/bash
# LANtern Minecraft boot script -- runs on every start.
#
# Three jobs, in order:
#   1. size the JVM heap against what Pelican actually allocated
#   2. rewrite the server.properties keys LANtern manages
#   3. exec the loader
#
# Credentials are written into server.properties, never passed on argv: the
# process command line is world-readable inside the container and Pelican echoes
# the startup line into the console on every boot.
set -euo pipefail
cd /home/container

say() { echo "[lantern] $*"; }

# ------------------------------------------------------------------ heap sizing
# SERVER_MEMORY is the container's hard limit in MiB. The heap must sit well
# under it: metaspace, code cache, GC structures, direct buffers and the JVM
# itself all live outside -Xmx, and a modded server with 485 mods carries a
# large class-metadata footprint. Exceeding the limit gets the container OOM
# killed by the kernel with no Java stack trace, which is a miserable thing to
# debug -- so reserve a fixed slice up front.
HEADROOM="${JVM_HEADROOM_MB:-1024}"
HEAP=$(( ${SERVER_MEMORY:-8192} - HEADROOM ))
if [ "${HEAP}" -lt 2048 ]; then
  say "FATAL: ${SERVER_MEMORY:-0} MiB allocated leaves only ${HEAP} MiB of heap."
  say "Give the server at least $(( 2048 + HEADROOM )) MiB in Pelican."
  exit 1
fi
[ "${HEAP}" -lt 6144 ] && say "WARNING: ${HEAP} MiB heap is under the 6 GB this pack wants."

# Aikar's flags. G1 defaults are tuned for throughput on short-lived heaps;
# Minecraft wants consistent short pauses on a large one, which is what the
# smaller region size and aggressive concurrent start deliver.
cat > user_jvm_args.txt <<ARGS
-Xms${HEAP}M
-Xmx${HEAP}M
-XX:+UseG1GC
-XX:+ParallelRefProcEnabled
-XX:MaxGCPauseMillis=200
-XX:+UnlockExperimentalVMOptions
-XX:+DisableExplicitGC
-XX:+AlwaysPreTouch
-XX:G1NewSizePercent=30
-XX:G1MaxNewSizePercent=40
-XX:G1HeapRegionSize=8M
-XX:G1ReservePercent=20
-XX:G1HeapWastePercent=5
-XX:G1MixedGCCountTarget=4
-XX:InitiatingHeapOccupancyPercent=15
-XX:G1MixedGCLiveThresholdPercent=90
-XX:G1RSetUpdatingPauseTimePercent=5
-XX:SurvivorRatio=32
-XX:+PerfDisableSharedMem
-XX:MaxTenuringThreshold=1
-Dusing.aikars.flags=https://mcflags.emc.gs
-Daikars.new.flags=true
-Dfml.queryResult=confirm
ARGS
say "heap ${HEAP} MiB of ${SERVER_MEMORY:-?} MiB allocated (${HEADROOM} MiB reserved)"

# ------------------------------------------------------------ server.properties
# Rewrite only the keys LANtern owns; leave everything else the operator or the
# pack set. Absent keys are appended.
prop() {
  local key="$1" val="$2"
  if grep -q "^${key}=" server.properties 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" server.properties
  else
    echo "${key}=${val}" >> server.properties
  fi
}

touch server.properties
prop server-port      "${SERVER_PORT:-25565}"
prop max-players      "${MAX_PLAYERS:-8}"
prop motd             "${MOTD:-LANtern}"
prop difficulty       "${DIFFICULTY:-normal}"
prop online-mode      "$([ "${ONLINE_MODE:-1}" = "1" ] && echo true || echo false)"
prop pvp              "$([ "${PVP:-1}" = "1" ] && echo true || echo false)"
prop white-list       "$([ "${WHITELIST:-0}" = "1" ] && echo true || echo false)"

# The two cvars that actually move server memory and CPU on a modded pack.
# Nobody notices 8 chunks; everybody notices the stutter at 12 with eight
# players loading chunks at once.
prop view-distance       "${VIEW_DISTANCE:-8}"
prop simulation-distance "${SIMULATION_DISTANCE:-6}"

if [ -n "${RCON_PASSWORD:-}" ]; then
  prop enable-rcon   "true"
  prop "rcon.port"   "${RCON_PORT:-25575}"
  prop "rcon.password" "${RCON_PASSWORD}"
  say "rcon enabled on ${RCON_PORT:-25575}"
else
  prop enable-rcon "false"
  say "rcon disabled (no password set) -- the control UI cannot reach this server"
fi

echo "eula=true" > eula.txt

# ------------------------------------------------------------------------ launch
# The loader writes an arg file naming its exact version, so glob for it rather
# than hardcoding a version that a pack update would silently invalidate.
ARGS_FILE=$(find libraries -name unix_args.txt 2>/dev/null | head -1)
if [ -z "${ARGS_FILE}" ]; then
  say "FATAL: no libraries/**/unix_args.txt -- the loader is not installed."
  say "Reinstall the server from the panel."
  exit 1
fi
say "loader: $(echo "${ARGS_FILE}" | cut -d/ -f4-5 | tr / ' ')"
say "mods:   $(find mods -name '*.jar' 2>/dev/null | wc -l)"

exec java @user_jvm_args.txt @"${ARGS_FILE}" nogui
