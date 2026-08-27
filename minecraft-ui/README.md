# LANtern Minecraft UI

The always-on Minecraft home for LANtern. It serves the themed shell on port
`8093` and mounts Create Schematic Viewer at `/schematics/` through a fixed,
private HTTP adapter. It does not control or depend on the Minecraft game
container.

## Boundaries

- `app/main.py` owns the shell, health/readiness, and administrator session routes.
- `app/schematic_workspace.py` owns prefix routing, streaming, contract checks,
  header isolation, error mapping, and the viewer HTTP adapter.
- `app/admin_session.py` owns Argon2 verification, signed expiring cookies,
  login throttling, exact-Origin checks, and trusted-token decisions.
- `static/` is the accessible two-tab shell. The viewer remains mounted in an
  iframe when tabs change so an in-progress view is not discarded.

Only `8093` is published. The viewer listens on `4173` inside the Compose-only
`schematic-backplane` network and has no host binding.

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
| `SCHEMATIC_VIEWER_URL` | `http://schematic-viewer:4173` | Fixed private upstream |
| `MINECRAFT_ADMIN_PASSWORD_HASH_FILE` | unset | Argon2 password-hash file |
| `MINECRAFT_SESSION_SECRET_FILE` | unset | Cookie-signing secret, at least 32 bytes |
| `SCHEMATIC_VIEWER_ADMIN_TOKEN_FILE` | unset | Shared private-hop token, at least 32 bytes |
| `MINECRAFT_SESSION_TTL_SECONDS` | `28800` | Administrator session lifetime |
| `MINECRAFT_SECURE_COOKIE` | `false` | Enable only when the public UI uses HTTPS |

If all three secret paths are unset, browsing works and administration is
disabled. A partial or undersized secret configuration fails at startup.

## HTTP interface

- `GET /` — Minecraft shell
- `GET /healthz` — UI process liveness
- `GET /readyz` — viewer readiness and contract compatibility
- `GET /api/session` — anonymous/admin state
- `POST /api/session/login` and `/api/session/logout` — same-origin admin session
- `/schematics/...` — streaming viewer workspace

The deployment and secret-generation runbook is in
[`../stack/README.md`](../stack/README.md); security details are in
[`../docs/SECRETS.md`](../docs/SECRETS.md).
