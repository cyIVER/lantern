# Bootstrap scripts

Idempotent scripts that finish a Pelican install without clicking through the UI.
Run them on the VM (`ssh lantern`), from `/opt/lantern/stack`.

> **One of these still points at the old Windows path.** They were written when
> the repo lived at `/mnt/c/Users/iveri/Documents/code/lantern/stack` under WSL.
> All but one now derive their own location
> (`cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.."`) and work wherever the
> tree is checked out.
>
> The exception is **`push-boot-script.sh`**, which still hardcodes
> `REPO=/mnt/c/Users/iveri/Documents/code/lantern`. That directory does not exist
> on the VM, so it runs against nothing. Override `REPO` or fix the line before
> using it.

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
| `setup-minecraft.sh` | Build and import the Minecraft egg, create the server, validate it |
| `setup-stardew.sh` | Bring the Stardew compose project up and validate it |
| `mc-rcon.py` | One-shot RCON to the Minecraft server |
| `backup.sh` | Snapshot **one** game to `/var/backups/lantern` (see **Backups** below) |

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

The other live Windows scripts are in `vm/`, not here: `Start-LANtern.cmd`,
`windows-setup.ps1`, `backup-pull.ps1`, `export-vm-image.ps1`, `reclaim-space.ps1`.
See [../../vm/README.md](../../vm/README.md).

## Superseded by the move to the VM

These exist in the tree and no longer have a purpose. They all solved problems
created by running Docker inside WSL2, and a bridged VM does not have those
problems. **Do not run them.**

There is also nothing left for them to act on: both WSL distros were unregistered
and their disks deleted, and WSL2 cannot start at all while the Windows hypervisor
is off. They are kept as a record of what the WSL arrangement needed, not as a
fallback.

| Script | What it used to do | Why it is dead |
|---|---|---|
| `../lantern.cmd` | Forward `lantern` from PowerShell into WSL | `lantern` is on the VM's `PATH`; you reach it over ssh |
| `publish-to-lan.ps1` | `netsh` portproxy from Windows into WSL's NAT | The VM has its own LAN address; nothing needs forwarding |
| `open-lan-firewall.ps1` | Hyper-V firewall rules for the WSL vNIC | There is no WSL vNIC in the path any more |
| `register-startup-task.ps1` | Register the logon sequencer | The task it registered, "LANtern startup", was unregistered by `vm/windows-setup.ps1`. Nothing sequences at logon now, and the VM is started by hand |
| `lantern-startup.ps1` | Logon sequencer: WSL → Docker Desktop → compose | The VM boots, systemd starts Docker, compose restarts itself. Also unrunnable: WSL2 and Docker Desktop do not work on this machine with the hypervisor off |
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

**Settled, and automatic.** `vm/backup-all.sh` takes the complete set nightly and
`vm/backup-pull.ps1` copies it to `D:\LANtern-Backups\data` on the Windows host —
a different physical disk from the one the VM lives on. A Windows scheduled task
named "LANtern backup" runs it at 03:00 and exits quietly when the VM is off. Full
detail in [../../vm/README.md](../../vm/README.md).

`backup.sh` here is the **single-game** version, kept because it is the quick
thing to reach for before a risky change to one server:

```bash
bash bootstrap/backup.sh minecraft
bash bootstrap/backup.sh stardew --keep 10
```

It now defaults to `/var/backups/lantern`. It used to default to
`/mnt/e/lantern-backups` — the Windows E: drive seen through WSL — which does not
exist on the VM, so on the VM it had never once worked. `LANTERN_BACKUP_DIR`
overrides it.

What it writes stays **inside** the VM, which is deliberate for what it is for:
undoing the change you are about to make. It is not a substitute for the nightly
pull, because a backup that lives only on the machine it is backing up does not
survive losing that machine.

Minecraft is quiesced through RCON rather than stopped — `save-off` plus
`save-all flush` — so nobody is kicked and the world is consistent. A tar taken
mid-chunk-write restores a world with holes in it, which is worse than no backup
because you find out weeks later.

---

See also: [USING.md](../../docs/USING.md) for day-to-day operation,
[CONTROL-UI.md](../../docs/CONTROL-UI.md) for the CS2 panel, and
[DECISIONS.md](../../docs/DECISIONS.md) for why things are shaped this way.
