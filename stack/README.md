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
   `restart: unless-stopped`, so the panel returns whenever the VM boots while
   game servers stay off until you start them. Starting the VM itself is a
   `VBoxManage startvm lantern --type headless` on Windows.

5. **Host services** — once Wings has run at least once:

   ```bash
   bash ../vm/install-vm-services.sh
   ```

   This installs `lantern-dbnet.timer` and symlinks `lantern` onto `PATH`. The
   timer re-attaches MariaDB to Wings' `pelican_nw` bridge every 60 seconds.
   It cannot be a compose dependency: Wings creates that network itself, and only
   once it starts, so an `external: true` reference fails on a machine where no
   game server has ever run. Without it, WeaponPaints inside the CS2 container
   cannot resolve `database` and every loadout silently reads as empty.

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

## Ports

Every one of these is reachable directly from the LAN — the VM is bridged, so
nothing on Windows forwards, proxies or filters them.

| Port | Service |
|---|---|
| 80 | Panel web UI |
| 8080 | Wings API (panel ↔ wings) |
| 2022 | SFTP for the panel file manager |
| 27015/udp + tcp | CS2 (once the server exists) |
| 27020/udp | CSTV |
| 25565 | Minecraft |

---

## Related

- [../docs/USING.md](../docs/USING.md) — day-to-day operation
- [../docs/CONTROL-UI.md](../docs/CONTROL-UI.md) — the CS2 control UI on :8090
- [../docs/CONNECTING.md](../docs/CONNECTING.md) — how players join
- [bootstrap/README.md](bootstrap/README.md) — the setup and repair scripts
