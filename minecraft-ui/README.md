# LANtern Minecraft UI

The always-on Minecraft home and administration portal for LANtern. It serves
the themed shell on port `8093`, operates Minecraft through LANtern and
Pelican's existing APIs, and mounts Create Schematic Viewer at `/schematics/`
through a private HTTP adapter. The portal remains available when the Minecraft
game is stopped.

The currently deployed `main` baseline already serves the home and schematic
workspace. The named-admin portal described below is implemented on
`feature/minecraft-admin-portal` and remains pending review, merge, release and
selective VM deployment.

## Access model

- Any LAN user can inspect Minecraft state and start, stop or restart it. The
  existing one-game-at-a-time control service still owns conflict detection and
  returns a confirmation before another running game is stopped.
- Any LAN user can browse, inspect, convert and download schematics, upload a
  submission, and request promotion to the shared catalog. Title, tags and the
  unrestricted `CC0-1.0` license are autofilled. A submission is published only
  when every automated requirement passes; otherwise it enters the admin review
  queue and raises the pending badge.
- Named administrators can publish or reject queued submissions, edit allowlisted
  text configuration, stage/enable/disable/delete mod JARs, create verified
  Pelican backups, restore a verified backup after confirmation, and inspect the
  append-only audit activity.
- The initial portal accounts are `iveri` and `scotlandf`. LANtern attributes
  their actions separately in its immutable local audit log.
- Pelican remains available at `http://192.168.0.115` for its complete management
  surface. The portal links to it in a new tab; it is not framed or proxied.

## Boundaries

- `app/portal.py` is the browser-facing workspace and intent seam.
- `app/identity.py` owns named Argon2 identities and signed, expiring sessions;
  `app/admin_cli.py` creates the secret-mounted identity directory interactively.
- `app/minecraft_control.py` delegates power operations to LANtern's existing
  control service, preserving its one-server-at-a-time policy.
- `app/catalog_workflow.py` and `app/viewer_catalog.py` own submission analysis,
  review state and publication into the viewer.
- `app/pelican_operations.py` exposes Minecraft-shaped file, mod, backup and
  restore operations. One Pelican service token is read from a Docker secret
  and is never returned to browser code. Pelican may record that service identity;
  named attribution lives in LANtern's local audit log.
- `app/audit_log.py` records named admin actions and LAN power actions.
- `app/schematic_workspace.py` owns prefix routing, streaming, contract checks,
  header isolation and the private viewer HTTP adapter.
- `static/` is the accessible Overview, Schematics and Admin shell. The viewer
  stays mounted while tabs change so an in-progress view is not discarded.

Only `8093` is published. The viewer listens on `4173` inside the Compose-only
`schematic-backplane` network and has no host binding, Docker socket, game-volume
mount or Wings-network access. The portal reaches the main control UI and Pelican
through `minecraft-admin-backplane`; credentials remain server-side.

Persistent portal queue and audit state lives in `lantern-minecraft-ui-data`.
Viewer library state lives separately in `lantern-schematic-viewer-data`. Both
volumes must be preserved across selective deploys and rollbacks.

## Development

From the repository root:

```powershell
Set-Location minecraft-ui
uv sync --locked --extra test
uv run pytest tests -q
uv run uvicorn app.main:app --port 8093
```

The UI expects the viewer at `http://schematic-viewer:4173` by default. For a
local viewer, set `SCHEMATIC_VIEWER_URL=http://127.0.0.1:4173` first.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `SCHEMATIC_VIEWER_URL` | `http://schematic-viewer:4173` | Fixed private viewer upstream |
| `MINECRAFT_ADMIN_USERS_FILE` | unset | Named-admin JSON directory containing Argon2 hashes and roles |
| `MINECRAFT_SESSION_SECRET_FILE` | unset | Cookie-signing secret, at least 32 bytes |
| `SCHEMATIC_VIEWER_ADMIN_TOKEN_FILE` | unset | Private viewer mutation token, at least 32 bytes |
| `MINECRAFT_SESSION_TTL_SECONDS` | `28800` | Administrator session lifetime |
| `MINECRAFT_SECURE_COOKIE` | `false` | Mark the admin cookie `Secure` when HTTPS is used |
| `MINECRAFT_ALLOW_INSECURE_ADMIN` | `false` in the application | Permit named administration over trusted-LAN HTTP; Compose intentionally defaults this to `true` |
| `MINECRAFT_TRUSTED_BROWSER_ORIGINS` | LAN deployment origins | Exact scheme/host/port origins permitted to use the portal |
| `MINECRAFT_PORTAL_DATA_DIR` | `/data` | Durable SQLite review and audit state |
| `SCHEMATIC_QUEUE_MAX_COUNT` | `500` | Maximum retained pending submissions |
| `SCHEMATIC_QUEUE_MAX_BYTES` | `2147483648` | Maximum retained pending payload bytes (2 GiB) |
| `SCHEMATIC_UPLOADS_PER_IP` | `20` | Uploads permitted per source during the rate window |
| `SCHEMATIC_RATE_WINDOW_SECONDS` | `3600` | Per-source rate-limit window |
| `SCHEMATIC_RETENTION_SECONDS` | `2592000` | Pending payload retention (30 days) |
| `SCHEMATIC_MAX_CONCURRENT_UPLOADS` | `4` | Global body-buffering admission limit |
| `SCHEMATIC_ADMISSION_TTL_SECONDS` | `600` | Expiry for abandoned pre-body reservations |
| `LANTERN_CONTROL_URL` | `http://ui:8090` | Existing LANtern power-policy service |
| `PELICAN_URL` | `http://panel` | Private Pelican application URL |
| `PELICAN_VIRTUAL_HOST` | `192.168.0.115` | Caddy `Host` authority sent while using the private `panel` service address |
| `PELICAN_API_KEY_FILE` | unset | Server-side Pelican client token file |
| `PELICAN_MINECRAFT_SERVER_NAME` | `LANtern Minecraft` | Pelican server lookup name |
| `PELICAN_UPLOAD_ORIGINS` | `http://192.168.0.115:8080` | Exact scheme/host/port accepted for Pelican's signed `/upload/file` target |

LANtern intentionally uses HTTP on a trusted private LAN. Its deployment sets
`MINECRAFT_ALLOW_INSECURE_ADMIN=true` and `MINECRAFT_SECURE_COOKIE=false`; the
session cookie remains `HttpOnly`, `SameSite=Strict`, signed and time-limited,
but it is not transport-encrypted. Do not publish port `8093` to the internet.
A partial or invalid secret configuration fails closed at startup.

Generate the initial identity file interactively after building the image:

```bash
python -m app.admin_cli /secrets/minecraft-admins.json \
  iveri:iveri scotlandf:scotlandf
```

The CLI's second value is a compatibility alias in the identity record; Pelican
operations still use one service token and do not impersonate either admin. The
command prompts twice for each password and writes only Argon2 hashes.
Production commands and the complete four-secret setup are in
[`../docs/SECRETS.md`](../docs/SECRETS.md).

All unsafe browser requests must match the configured exact origin; a forged
`Host` header cannot define a new trusted origin. Admin mod uploads are
authorized before their bodies are buffered. Mutations claim durable idempotency
keys before changing state, and destructive confirmation challenges are bound to
the exact request so they cannot execute twice. Logout persistently revokes the
captured session until its original expiry.

Expired pending schematic payload bytes are purged after 30 days and stop
counting against quota. Their durable metadata and workflow events remain for
accountability; published and rejected payloads are discarded immediately after
their terminal transition. Interrupted nonterminal workflows are recovered into
review after the admission TTL. Backup creation stops Minecraft, proves Pelican
reports it offline, marks the backup as consistency-proven, and leaves the server
stopped. Restore repeats the offline proof before its safety backup and again
before replacement; the receipt reports `server_state=stopped`. Externally created
backups without LANtern's offline marker are visible but cannot be restored here.

## HTTP interface

- `GET /` — Minecraft shell
- `GET /healthz` — UI process liveness
- `GET /readyz` — viewer readiness and contract compatibility
- `GET /api/workspace` — role-filtered state for the three tabs
- `POST /api/intents` — same-origin login, power, review and admin operations
- `POST /api/submissions` — LAN schematic submission and optional promotion request
- `GET /api/admin/submissions/{id}/download` — authenticated review payload download
- `POST /api/admin/mods` — authenticated staged mod upload
- `/schematics/...` — streaming viewer workspace

Deployment and rollback are in [`../stack/README.md`](../stack/README.md).
