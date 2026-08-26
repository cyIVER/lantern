# Bootstrap scripts

Idempotent scripts that finish a Pelican install without clicking through the UI.
Run them on the VM (`ssh lantern`), from `/opt/lantern/stack`.

> **Most of these still `cd` to the old Windows path.** They were written when the
> repo lived at `/mnt/c/Users/iveri/Documents/code/lantern/stack` under WSL, and
> that line is hardcoded near the top of `add-admin.sh`, `configure-menus.sh`,
> `configure-weaponpaints.sh`, `cs2-status.sh`, `fetch-missing-plugin.sh`,
> `install-wings-config.sh`, `push-boot-script.sh`, `repair-platform.sh`,
> `setup-weaponpaints-db.sh` and `test-modes.sh`. On the VM that directory does
> not exist, so they abort or run against nothing. Point them at
> `/opt/lantern/stack` before relying on any of them.

| Script | Purpose |
|---|---|
| `create-node.php` | Creates the `lantern` node (or reports the existing one) and prints its Wings config |
| `install-wings-config.sh` | Regenerates the config from a fresh model and writes `/etc/pelican/config.yml` |
| `allocations.php` | Creates the CS2 / CSTV / Minecraft port allocations |
| `create-cs2-server.php` | Creates the CS2 server (triggers the ~66 GB download) |
| `create-ui-credentials.php` | API key + RCON details for the control UI |
| `push-boot-script.sh` | Update `boot.sh` on a live server without reinstalling |
| `repair-platform.sh` | Reinstall Metamod + CounterStrikeSharp when plugins go silent |
| `fetch-missing-plugin.sh` | Stage one plugin into an installed server |
| `add-admin.sh` | Grant a SteamID64 server admin |
| `test-modes.sh` | Assert each mode loads the right plugins |
| `cs2-status.sh` | Install / runtime progress |
| `rotate-rcon.php` | Rotate the RCON password |
| `setup-weaponpaints-db.sh` | Scoped MySQL database for skins |
| `backup.sh` | Snapshot a game's volumes (see the note under **Backups** below) |

```bash
cd /opt/lantern/stack
docker compose exec -T panel php artisan tinker < bootstrap/create-node.php
bash bootstrap/install-wings-config.sh
docker compose restart wings
docker compose exec -T panel php artisan tinker < bootstrap/allocations.php
```

## The Windows-side script

One PowerShell script is still live, and it is a **host** concern rather than a
stack concern:

| Script | Purpose |
|---|---|
| `fix-ethernet.ps1` | Recover the physical NIC when it wedges (elevated, on Windows) |

The VM is bridged, which means its adapter rides the Windows host's physical NIC.
A wedged NIC on Windows therefore still takes LANtern off the LAN even though
nothing about the stack has changed. Nothing on Windows routes, forwards or
filters LANtern traffic any more — but the cable still belongs to Windows.

## Superseded by the move to the VM

These exist in the tree and no longer have a purpose. They all solved problems
created by running Docker inside WSL2, and a bridged VM does not have those
problems. **Do not run them.**

| Script | What it used to do | Why it is dead |
|---|---|---|
| `../lantern.cmd` | Forward `lantern` from PowerShell into WSL | `lantern` is on the VM's `PATH`; you reach it over ssh |
| `publish-to-lan.ps1` | `netsh` portproxy from Windows into WSL's NAT | The VM has its own LAN address; nothing needs forwarding |
| `open-lan-firewall.ps1` | Hyper-V firewall rules for the WSL vNIC | There is no WSL vNIC in the path any more |
| `register-startup-task.ps1` | Register the logon sequencer | Nothing sequences at logon now |
| `lantern-startup.ps1` | Logon sequencer: WSL → Docker Desktop → compose | The VM boots, systemd starts Docker, compose restarts itself |
| `set-service-ip.ps1` | Pin `192.168.0.115` onto a Windows NIC | The VM holds `.115` statically via netplan; Windows is on DHCP |

## Gotchas these encode

**`uuid` is null right after `save()`.** It is generated in a model hook, so the
in-memory instance does not have it yet. `getYamlConfiguration()` on that stale
instance emits `uuid: null` and Wings refuses to start. Always `->fresh()` first;
`install-wings-config.sh` also hard-aborts if the uuid is missing.

**`/etc/pelican` needs root, but not `sudo`.** The Docker daemon runs as root, so
a throwaway container with that path bind-mounted can write the config without a
password prompt. On the VM you could equally `sudo tee` it; the container route
just keeps the script working for anyone in the `docker` group.

**Allocations bind `0.0.0.0`, aliased to `192.168.0.115`.** Docker publishes on
every interface the VM has; the alias is what the panel displays to users, and it
is now genuinely the address players reach.

**`systemInformation()` caches for 360 seconds -- including failures.** If you query a
node while Wings is still restarting, Docker's port proxy accepts the TCP connection
before Wings is listening and closes it, giving `cURL error 52: Empty reply from
server`. That exception is then cached for six minutes and every retry replays it,
which looks exactly like a broken node. Clear it rather than debugging the network:

```bash
docker compose exec -T panel php artisan tinker \
  --execute='cache()->forget("nodes.1.system_information");'
```

## Backups

`backup.sh` still defaults its destination to `/mnt/e/lantern-backups`, which was
the Windows E: drive seen through WSL. That path does not exist on the VM, so set
the destination explicitly until the default is changed:

```bash
LANTERN_BACKUP_DIR=/var/backups/lantern bash bootstrap/backup.sh stardew
```

Where backups should ultimately live is unsettled. The old arrangement wrote them
to E: precisely so that one corrupted disk image could not take the farm and every
backup of it at the same moment, and the same reasoning applies to the VM's virtual
disk. A destination off that disk is worth arranging; nothing does it automatically
today.

---

See also: [USING.md](../../docs/USING.md) for day-to-day operation,
[CONTROL-UI.md](../../docs/CONTROL-UI.md) for the CS2 panel, and
[DECISIONS.md](../../docs/DECISIONS.md) for why things are shaped this way.
