#!/usr/bin/env bash
# Progress of the CS2 server install / runtime.
#   bash bootstrap/cs2-status.sh
cd /mnt/c/Users/iveri/Documents/code/lantern/stack

UUID=$(docker compose exec -T panel php artisan tinker \
  --execute='echo \App\Models\Server::where("name","LANtern CS2")->value("uuid");' 2>/dev/null \
  | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)

echo "server uuid : ${UUID:-<not found>}"
docker compose exec -T panel php artisan tinker \
  --execute='$s=\App\Models\Server::where("name","LANtern CS2")->first(); echo "status      : "; echo $s->status?->value ?? "installed (running)"; echo PHP_EOL;' 2>/dev/null | grep status

echo "containers  :"
docker ps -a --filter "name=$UUID" --format "  {{.Names}}  {{.Status}}" 2>/dev/null

echo "disk used   :"
docker run --rm -v /var/lib/pelican/volumes:/v alpine \
  sh -c "du -sh /v/$UUID 2>/dev/null || echo '  (not created yet)'"

echo "last install output:"
cid=$(docker ps -aq --filter "name=${UUID}_installer" | head -1)
[ -n "$cid" ] && docker logs "$cid" 2>&1 | tail -6 | sed 's/^/  /' || echo "  (installer finished or not started)"
