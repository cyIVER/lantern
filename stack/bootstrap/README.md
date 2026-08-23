# Bootstrap scripts

Idempotent scripts that finish a Pelican install without clicking through the UI.
Run them from inside Ubuntu WSL, from `stack/`.

| Script | Purpose |
|---|---|
| `create-node.php` | Creates the `lantern` node (or reports the existing one) and prints its Wings config |
| `install-wings-config.sh` | Regenerates the config from a fresh model and writes `/etc/pelican/config.yml` |
| `allocations.php` | Creates the CS2 / CSTV / Minecraft port allocations |

```bash
cd /mnt/c/Users/iveri/Documents/code/lantern/stack
docker compose exec -T panel php artisan tinker < bootstrap/create-node.php
bash bootstrap/install-wings-config.sh
docker compose restart wings
docker compose exec -T panel php artisan tinker < bootstrap/allocations.php
```

## Gotchas these encode

**`uuid` is null right after `save()`.** It is generated in a model hook, so the
in-memory instance does not have it yet. `getYamlConfiguration()` on that stale
instance emits `uuid: null` and Wings refuses to start. Always `->fresh()` first;
`install-wings-config.sh` also hard-aborts if the uuid is missing.

**`/etc/pelican` needs root, but not `sudo`.** The path resolves into the Ubuntu WSL
filesystem, and the Docker daemon runs as root, so a throwaway container with that
path bind-mounted can write the config without a password prompt.

**Allocations bind `0.0.0.0`, aliased to `192.168.0.115`.** Docker Desktop publishes
to every interface; the alias is what the panel displays to users.

**`systemInformation()` caches for 360 seconds -- including failures.** If you query a
node while Wings is still restarting, Docker's port proxy accepts the TCP connection
before Wings is listening and closes it, giving `cURL error 52: Empty reply from
server`. That exception is then cached for six minutes and every retry replays it,
which looks exactly like a broken node. Clear it rather than debugging the network:

```bash
docker compose exec -T panel php artisan tinker \
  --execute='cache()->forget("nodes.1.system_information");'
```
