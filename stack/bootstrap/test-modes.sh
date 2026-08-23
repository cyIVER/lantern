#!/usr/bin/env bash
# Verify MODE switching activates exactly the right plugin set.
#
# MatchZy, RetakesPlugin and CSPracc each hook round flow; two loaded at once
# produces broken rounds. This asserts that switching modes both ACTIVATES the
# expected plugin and DEACTIVATES the other two.
#
#   bash bootstrap/test-modes.sh [mode ...]     (default: all four, ends on competitive)
set -uo pipefail
cd /mnt/c/Users/iveri/Documents/code/lantern/stack

MODES=("$@")
[ ${#MODES[@]} -eq 0 ] && MODES=(retakes practice deathmatch competitive)

UUID=$(docker compose exec -T panel php artisan tinker \
  --execute='echo \App\Models\Server::where("name","LANtern CS2")->value("uuid");' 2>/dev/null \
  | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)
[ -n "$UUID" ] || { echo "could not resolve server uuid"; exit 1; }

# mode -> plugin that must be present / plugins that must be absent
expect_for() {
  case "$1" in
    competitive) echo "MatchZy|RetakesPlugin CSPracc" ;;
    retakes)     echo "RetakesPlugin|MatchZy CSPracc" ;;
    practice)    echo "CSPracc|MatchZy RetakesPlugin" ;;
    deathmatch)  echo "|MatchZy RetakesPlugin CSPracc" ;;
  esac
}

set_mode() {
  docker compose exec -T panel php artisan tinker --execute="
    \$s = \App\Models\Server::where('name','LANtern CS2')->firstOrFail();
    \$v = \$s->egg->variables()->where('env_variable','MODE')->firstOrFail();
    \App\Models\ServerVariable::updateOrCreate(
      ['server_id'=>\$s->id,'variable_id'=>\$v->id], ['variable_value'=>'$1']);
    app(\App\Repositories\Daemon\DaemonServerRepository::class)->setServer(\$s)->power('restart');
  " >/dev/null 2>&1
}

FAILURES=0
for mode in "${MODES[@]}"; do
  IFS='|' read -r want_present want_absent <<< "$(expect_for "$mode")"
  echo "=============================================================="
  echo "MODE=$mode   expect present: ${want_present:-<none>}"
  echo "             expect absent : $want_absent"
  set_mode "$mode"

  # Wait for the boot script to report the mode it actually activated.
  active=""
  for _ in $(seq 1 30); do
    sleep 4
    line=$(docker logs "$UUID" 2>&1 | grep -a "active plugins:" | tail -1)
    modeline=$(docker logs "$UUID" 2>&1 | grep -aE "MODE=$mode ->" | tail -1)
    [ -n "$modeline" ] && [ -n "$line" ] && { active="${line#*active plugins: }"; break; }
  done

  if [ -z "$active" ]; then
    echo "  RESULT: FAIL -- server did not report activation in time"
    FAILURES=$((FAILURES+1)); continue
  fi
  echo "  active: $active"

  ok=1
  if [ -n "$want_present" ] && ! echo " $active " | grep -q " $want_present "; then
    echo "  MISSING: $want_present"; ok=0
  fi
  for p in $want_absent; do
    if echo " $active " | grep -q " $p "; then echo "  LEAKED: $p"; ok=0; fi
  done

  if [ "$ok" = "1" ]; then echo "  RESULT: PASS"; else echo "  RESULT: FAIL"; FAILURES=$((FAILURES+1)); fi
done

echo "=============================================================="
echo "failures: $FAILURES"
exit $((FAILURES > 0))
