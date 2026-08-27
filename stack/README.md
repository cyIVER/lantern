# Pelican stack

Panel + Wings + MariaDB + Redis on Docker CE, on the `lantern` VM.

## Bring up

**Run this on the VM**, not on Windows:

```bash
ssh lantern
cd /opt/lantern/stack

# One-time: the panel mounts a 'plugins' subpath of a volume that starts empty,
# so the directory has to exist before the panel will start.
docker run --rm -v stack_pelican-data:/d alpine mkdir -p /d/plugins

docker compose up -d
docker compose ps
```

Docker here is **Docker CE from Docker's own apt repository** — not Docker
Desktop. `/var/run/docker.sock`, the bind paths Wings hands the daemon, and the
filesystem the compose file references are all the one Linux namespace, so there
is no path-translation layer to get wrong.

If the `ui` container comes up healthy but the Loadout grids render blank, its
`/var/lib/pelican/volumes` bind is pointing at nothing. Check it directly rather
than trusting the health state:

```bash
docker exec stack-ui-1 ls /volumes   # must list the server UUID, not nothing
```

## First run

1. **Installer** — open <http://192.168.0.115/installer>. Database and Redis
   settings are already supplied via environment, so it should validate on its own.
   Create your admin account here; the password is yours and never goes through an agent.

   Admin login identity (no secret here -- the password is not recorded anywhere):

   | Field | Value |
   |---|---|
   | App name | `LANtern` |
   | App URL | `http://192.168.0.115` |
   | Admin email / login handle | `iveri@lantern.lan` |

   `MAIL_DRIVER` is `log`, so **password reset email will never arrive**. If you are
   locked out, create a new admin from the CLI instead:

   ```bash
   docker compose exec panel php artisan p:user:make
   ```

   ### Installer gotcha: "Page expired" on Finish

   The wizard uses `persistStepInQueryString()`. **If the page reloads mid-wizard,
   you return to the same step with empty form data** -- and Filament only validates
   the *current* step, so blank earlier-step values pass straight through to Finish.

   `submit()` then runs in this order, and swallows failures via `catch (Halt)`:

   ```
   writeToEnvironment(APP_INSTALLED=true)   <- installer disables itself FIRST
   runMigrations()
   createAdminUser()                        <- dies here if user data is empty
   writeToEnv('env_session')                <- never reached
   installEggs()                            <- never reached
   ```

   Result: `APP_INSTALLED=true` with **no admin user and no eggs**, and the installer
   refuses to run again. Symptom in the log:

   ```
   User::email(): Argument #1 ($value) must be of type string, null given
   ```

   Recover from the CLI rather than resetting the install:

   ```bash
   docker compose exec panel php artisan p:user:make        --email iveri@lantern.lan --username iveri --admin=1
   ```

   Then log in and import eggs from *Admin -> Eggs -> Import*.

   Note `writeToEnv('env_session')` never running is harmless here: `SESSION_DRIVER`
   is supplied by compose as a container env var, and `SESSION_SECURE_COOKIE` unset
   defaults to false, which is what plain-HTTP needs anyway.

2. **Create the Node** — in the panel, *Admin → Nodes → Create Node*:

   | Field | Value |
   |---|---|
   | Name | `lantern` |
   | FQDN / IP | `192.168.0.115` |
   | Communicate over SSL | **off** (the panel is plain HTTP on the LAN) |
   | Daemon port | `8080` |
   | SFTP port | `2022` |
   | Daemon directory | `/var/lib/pelican/volumes` |

3. **Install the Wings config** — the node page shows a generated `config.yml`.
   Write it onto the VM and restart Wings:

   ```bash
   sudo mkdir -p /etc/pelican
   sudo nano /etc/pelican/config.yml     # paste the generated contents
   docker compose restart wings
   docker compose logs wings --tail 20   # should report it is listening
   ```

   Until this exists Wings logs *"Configuration File Not Found"* — that is the
   expected state on a fresh stack, not a fault.

4. **Autostart** — nothing to configure inside the VM. The compose services are
   `restart: unless-stopped`, so the panel returns whenever the VM boots. Wings
   also restores whichever game server was running when the VM went down, so the
   box does not necessarily come up idle.

   Starting the **VM** is a manual act on Windows: the Desktop shortcut
   **"Start LANtern"**, or `VBoxManage startvm lantern --type headless`. Nothing
   does it automatically — the old logon task was unregistered when the stack left
   WSL and was deliberately not replaced.

5. **Host services** — once Wings has run at least once:

   ```bash
   bash ../vm/install-vm-services.sh
   ```

   This installs `lantern-dbnet.timer`, creates a 4 GB swap file at
   `vm.swappiness=10`, and symlinks `lantern` onto `PATH`. The timer re-attaches
   MariaDB to Wings' `pelican_nw` bridge every 60 seconds.
   It cannot be a compose dependency: Wings creates that network itself, and only
   once it starts, so an `external: true` reference fails on a machine where no
   game server has ever run. Without it, WeaponPaints inside the CS2 container
   cannot resolve `database` and every loadout silently reads as empty.

   The swap is a shock absorber, not headroom — 11 GB of the VM's ~17.6 is
   committed to Minecraft when it runs, and without swap a spike is the OOM killer
   ending something mid-save rather than a stutter. See
   [../vm/README.md](../vm/README.md).

6. **On Windows**, in an elevated PowerShell — writes the Desktop shortcut and
   registers the nightly backup to `D:`:

   ```powershell
   vm\windows-setup.ps1
   ```

## Minecraft UI release (pending approval)

The repository contains the `minecraft-ui` service and its private
`schematic-viewer` sidecar, but they are **not deployed yet**. Do not run this
section until the release gate has separately approved the viewer release,
exact image digest, secret creation and VM cutover.

The viewer image is hard-pinned in `compose.yml` to the independently verified
v1.0.1 release index:

```text
ghcr.io/scotsgamez/create-schematic-viewer:v1.0.1@sha256:d5501af9de95f9b89484ae4e4dbea098b0cdd3e86af3b19e50976855b533444c
```

Changing the viewer is a reviewed source change; `latest`, a bare mutable tag,
or a runtime `.env` override cannot select a different image. Configure the
LAN-only bind, secret-reader group, and secure-cookie policy in `stack/.env`:

```dotenv
LANTERN_MINECRAFT_UI_BIND_IP=192.168.0.115
LANTERN_SECRET_GID=1000 # replace with the output of `id -g` on the VM
MINECRAFT_SECURE_COOKIE=false # keep false until an HTTPS proxy is in place
```

Create the three files as described in
[../docs/SECRETS.md](../docs/SECRETS.md#5-minecraft-ui-and-schematic-library-secrets),
then deploy only the two new services:

```bash
cd /opt/lantern/stack
docker compose pull schematic-viewer
docker compose build minecraft-ui
docker compose up -d --no-deps schematic-viewer minecraft-ui

curl --fail http://192.168.0.115:8093/healthz
curl --fail http://192.168.0.115:8093/readyz
docker compose ps schematic-viewer minecraft-ui
```

This command does not recreate or restart Wings, the panel, the existing UI or
the running Minecraft server. The Minecraft UI and schematic library use
`restart: unless-stopped` and stay available independently of game power.

For a viewer rollback, make a reviewed source change that restores the previous
approved digest in `compose.yml`, pull it, and repeat the selective `up` command
with `--force-recreate`. For an initial-release rollback, stop only the new
services:

```bash
docker compose stop minecraft-ui schematic-viewer
```

Never use `docker compose down` or `down -v` for this rollback. The named
`lantern-schematic-viewer-data` volume is deliberately retained. A selective
rollback has no effect on Wings or Minecraft; port 8093 merely disappears until
the UI is released again.

To restore a backed-up schematic library, use the guarded restore script. It
requires the archive and SHA-256 companion under the approved backup root,
validates the viewer's typed backup manifest before touching live data, creates
an atomic pre-restore safety copy, and replaces only the named viewer volume:

```bash
sudo /opt/lantern/vm/restore-schematic-library.sh \
  /var/backups/lantern/<timestamp>/schematic-viewer-data.tgz \
  --confirm-replace
```

## Deltas from upstream

Taken from `pelican/panel` `compose-full-stack.yml` and `pelican/wings`
`docker-compose.example.yml`, changed as follows:

| Change | Reason |
|---|---|
| Dropped `build: .` on panel | We consume the published image, not a source build |
| Port 80 only, no 443 | LAN-only, plain HTTP by decision |
| `restart: unless-stopped` | Was `always`; matches the panel-only autostart decision |
| Default subnet `172.22.0.0/16` | Upstream's `172.20.0.0/16` collided with `memory-system_default` on the old host; kept because Wings' own `pelican_nw` sits at `172.23.x` |
| DB healthcheck + `depends_on: healthy` | Panel raced MariaDB on first boot |
| Secrets from a gitignored `.env` | Upstream ships literal `NEEDSTOCHANGE` placeholders |
| Always-on Minecraft UI and private viewer sidecar | Port 8093 serves the themed shell; viewer port 4173 is internal-only and its library has a named volume |

## Ports

Every published port below is reachable directly from the LAN once its service
is deployed — the VM is bridged, so
nothing on Windows forwards, proxies or filters them. Nothing on the VM does
either: **there is no firewall on it, deliberately.** `ufw` cannot protect
Docker-published ports, because Docker inserts its own nftables rules ahead of
ufw's — a `ufw deny` on a published port would be the appearance of protection
without the fact of it, which is worse than none. This is a home LAN behind NAT
and the panel has real authentication.

| Port | Service |
|---|---|
| 80 | Panel web UI |
| 2022 | SFTP for the panel file manager |
| 5800 | Stardew web VNC (password-protected) |
| 8080 | Wings API (panel ↔ wings) |
| 8090 | LANtern landing page (`/`) and CS2 control UI (`/cs2`) |
| 8091 | Stardew HTTP API |
| 8092 | Stardew control UI |
| 8093 | Minecraft UI and `/schematics/` workspace — implementation complete, deployment pending the release gate |
| 24642/udp | Stardew game |
| 25565 · 25575 | Minecraft · its RCON |
| 27015/udp + tcp | CS2 |
| 27020/udp | CSTV |
| 27030/udp | Stardew query (moved off 27015, which CS2 owns) |

The Stardew ports belong to a separate compose project in `../stardew`, not to
this one. They are listed here because "what is on this box" is the question
people actually have.

---

## Related

- [../docs/USING.md](../docs/USING.md) — day-to-day operation
- [../docs/CONTROL-UI.md](../docs/CONTROL-UI.md) — the landing, CS2 and release-gated Minecraft UIs
- [../docs/CONNECTING.md](../docs/CONNECTING.md) — how players join
- [../vm/README.md](../vm/README.md) — the VM under all of this, and the backups
- [bootstrap/README.md](bootstrap/README.md) — the setup and repair scripts
