#!/usr/bin/env bash
# Verify MODE switching activates exactly the right plugin set.
#
# MatchZy, RetakesPlugin and CSPracc each hook round flow; two loaded at once
# produces broken rounds. This asserts that switching modes both ACTIVATES the
# expected plugin and DEACTIVATES the other two.
#
#   bash bootstrap/test-modes.sh [mode ...]     (default: all four, ends on competitive)
set -uo pipefail
cd /mnt/c/Users/iveri/Documents/code/lantern/stack || exit 1

MODES=("$@")
[ ${#MODES[@]} -eq 0 ] && MODES=(retakes practice deathmatch competitive)

UUID=$(docker compose exec -T panel php artisan tinker \
  --execute='echo \App\Models\Server::where("name","LANtern CS2")->value("uuid");' 2>/dev/null \
  | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)
[ -n "$UUID" ] || { echo "could not resolve server uuid"; exit 1; }

# mode -> plugin that must LOAD / plugins that must not load.
#
# Matched against CounterStrikeSharp's "Finished loading plugin <Name>" lines,
# not against directory contents. An earlier version of this test checked which
# directories were staged, which passed happily while the whole platform was
# broken and nothing was loading at all.
expect_for() {
  case "$1" in
    competitive) echo "MatchZy|Retakes CSPracc" ;;
    retakes)     echo "Retakes|MatchZy CSPracc" ;;
    practice)    echo "CSPracc|MatchZy Retakes" ;;
    deathmatch)  echo "|MatchZy Retakes CSPracc" ;;
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

  # Wait until CSSharp finishes its plugin pass for this boot.
  active=""
  for _ in $(seq 1 40); do
    sleep 4
    modeline=$(docker logs "$UUID" --since 5m 2>&1 | grep -aE "MODE=$mode ->" | tail -1)
    [ -n "$modeline" ] || continue
    # tr -d strips the ESC bytes of CSSharp's colour codes; the remaining
    # "[0-9;]*m" is then removed so plugin names compare cleanly.
    loaded=$(docker logs "$UUID" --since 5m 2>&1 \
             | tr -d '\033' \
             | sed -E 's/\[[0-9;]*m//g' \
             | grep -a "Finished loading plugin" \
             | sed 's/.*plugin //' | sort -u | tr '\n' ' ')
    # SimpleAdmin loads in every mode, so its presence means the pass is done.
    echo "$loaded" | grep -q "SimpleAdmin" && { active="$loaded"; break; }
  done

  if [ -z "$active" ]; then
    echo "  RESULT: FAIL -- no plugins loaded (platform broken? see repair-platform.sh)"
    FAILURES=$((FAILURES+1)); continue
  fi
  echo "  loaded: $active"

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
