# Pelican stack

Panel + Wings + MariaDB + Redis on Docker Desktop (WSL2 backend).

## Bring up

**Run from inside Ubuntu WSL, not Git Bash.** MSYS rewrites Linux paths and will
mangle the docker socket mount.

```bash
cd /mnt/c/Users/iveri/Documents/code/lantern/stack

# One-time: the panel mounts a 'plugins' subpath of a volume that starts empty,
# so the directory has to exist before the panel will start.
docker run --rm -v stack_pelican-data:/d alpine mkdir -p /d/plugins

docker compose up -d
docker compose ps
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
   Write it into Ubuntu WSL and restart Wings:

   ```bash
   sudo mkdir -p /etc/pelican
   sudo nano /etc/pelican/config.yml     # paste the generated contents
   docker compose restart wings
   docker compose logs wings --tail 20   # should report it is listening
   ```

   Until this exists Wings logs *"Configuration File Not Found"* — that is the
   expected state on a fresh stack, not a fault.

4. **Autostart** — Docker Desktop → Settings → General → *Start Docker Desktop when
   you sign in*. The compose services are `restart: unless-stopped`, so the panel
   returns after a reboot while game servers stay off until you start them.

## Deltas from upstream

Taken from `pelican/panel` `compose-full-stack.yml` and `pelican/wings`
`docker-compose.example.yml`, changed as follows:

| Change | Reason |
|---|---|
| Dropped `build: .` on panel | We consume the published image, not a source build |
| Port 80 only, no 443 | LAN-only, plain HTTP by decision |
| `restart: unless-stopped` | Was `always`; matches the panel-only autostart decision |
| Default subnet `172.22.0.0/16` | Upstream's `172.20.0.0/16` collides with `memory-system_default`; `172.26.x` is the WSL vSwitch |
| DB healthcheck + `depends_on: healthy` | Panel raced MariaDB on first boot |
| Secrets from a gitignored `.env` | Upstream ships literal `NEEDSTOCHANGE` placeholders |

## Ports

| Port | Service |
|---|---|
| 80 | Panel web UI |
| 8080 | Wings API (panel ↔ wings) |
| 2022 | SFTP for the panel file manager |
| 27015/udp + tcp | CS2 (once the server exists) |
| 27020/udp | CSTV |
| 25565 | Minecraft |
