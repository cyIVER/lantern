# Bootstrap scripts

Idempotent scripts that finish a Pelican install without clicking through the UI.
Run them from inside Ubuntu WSL, from `stack/`.

| Script | Purpose |
|---|---|
| `create-node.php` | Creates the `lantern` node (or reports the existing one) and prints its Wings config |
| `install-wings-config.sh` | Regenerates the config from a fresh model and writes `/etc/pelican/config.yml` |
| `allocations.php` | Creates the CS2 / CSTV / Minecraft port allocations |
| `create-cs2-server.php` | Creates the CS2 server (triggers the ~66 GB download) |
| `create-ui-credentials.php` | API key + RCON details for the control UI |
| `open-lan-firewall.ps1` | Hyper-V firewall rules so the LAN can reach the servers (elevated) |
| `push-boot-script.sh` | Update `boot.sh` on a live server without reinstalling |
| `repair-platform.sh` | Reinstall Metamod + CounterStrikeSharp when plugins go silent |
| `fetch-missing-plugin.sh` | Stage one plugin into an installed server |
| `add-admin.sh` | Grant a SteamID64 server admin |
| `test-modes.sh` | Assert each mode loads the right plugins |
| `cs2-status.sh` | Install / runtime progress |
| `rotate-rcon.php` | Rotate the RCON password |
| `setup-weaponpaints-db.sh` | Scoped MySQL database for skins |
| `lantern-startup.ps1` | Logon startup sequencer (WSL → Docker → stack) |

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

---

See also: [USING.md](../../docs/USING.md) for day-to-day operation,
[CONTROL-UI.md](../../docs/CONTROL-UI.md) for the CS2 panel, and
[DECISIONS.md](../../docs/DECISIONS.md) for why things are shaped this way.
