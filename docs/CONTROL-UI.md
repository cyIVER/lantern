# CS2 Control UI

**<http://192.168.0.115:8090>** — no login. A CS2-styled panel for the things you
actually touch mid-party: players, maps, mode, match control, and a console that
returns output.

Pelican is still there for everything else — files, backups, schedules, subusers,
creating new servers. This does not replace it; it sits on top of it.

---

## Tabs

| Tab | What it does |
|---|---|
| **Players** | Live roster with SteamID64s. Kick / ban / mute / slay / swap per player. Bots are flagged and only offer kick. |
| **Maps** | All 21 maps as cards with the **real in-game icons**. One click issues `changelevel`; the "also set as boot map" toggle persists it. |
| **Game Mode** | competitive / retakes / deathmatch, plus bot quota and difficulty sliders and the skins/VAC/auto-update toggles. |
| **Loadout** | Pick knives, gloves and weapon skins for any player and write them straight into WeaponPaints. |
| **Match** | MatchZy: start, knife round, pause, unpause, warmup, restart round. |
| **Console** | RCON. Unlike Pelican's console, it **shows you the reply**. |

The header always shows state, current map, player count, CPU, RAM and uptime,
with Start / Restart / Stop / Kill.

---

## How it is wired

Hybrid, because neither upstream can do the whole job:

```
                 ┌─ Pelican client API ──→ power, startup variables
   Browser ─→ UI ┤                          (RCON cannot start a stopped server
                 └─ direct RCON ─────────→   or know about egg config)
                                            roster, moderation, cvars
                                            (Pelican's command endpoint is
                                             fire-and-forget, returns no output)
```

Both paths act on the same server, so this UI and the Pelican panel never drift
out of sync — change the mode here and the panel's Startup tab reflects it.

Polling, not websockets: 4 seconds, imperceptible on a LAN, and it avoids
reconnect and token-refresh state for no real benefit.

## Things learned building it

**CS2 has `status_json`.** It returns `server.clients` as structured JSON with
`steamid64`, a `bot` flag and the name. Far more durable than scraping the
`status` text table, whose columns shift between updates and which does not
include SteamIDs for every client.

**The classic RCON sentinel trick does not work on CS2.** Following a command
with an empty `RESPONSE_VALUE` packet and reading until its echo returns is the
standard way to detect the end of multi-packet output. CS2 never echoes it back,
and sending one makes CS2 return *nothing at all*. `rcon.py` instead waits the
full timeout for the first packet then drains with a short idle window.

**Caddy matches on the `APP_URL` host.** Requesting `http://panel` (the compose
service name) returns an empty `200` — not an error, just nothing. The API base
must be the LAN IP.

**Bots carry synthetic `9007...` SteamIDs** that no admin command accepts, so the
UI kicks them by name with `bot_kick` instead.

---

## Loadout editor

Assign a knife, gloves or a weapon skin to any player from the browser, instead
of making them navigate an in-game menu.

The catalogue comes from **WeaponPaints' own bundled JSON** (`skins_en.json`,
`gloves_en.json`), read read-only from the server volume — 2067 skins and 95
gloves. Because it is the plugin's own data, the list always matches the version
actually installed; there is no second item database to drift.

Writes go directly into the plugin's tables, for **both teams** so a choice
applies whichever side the player ends up on:

| Choice | Rows written |
|---|---|
| Knife | `wp_player_knife` (entity name, e.g. `weapon_knife_karambit`) |
| Gloves | `wp_player_gloves` (model) **and** `wp_player_skins` (its paint) |
| Weapon skin | `wp_player_skins` (defindex + paint id) |

Changes apply **on respawn** — `!kill`, or wait for the next round.

Item images are hosted on GitHub, so the grid needs internet to show pictures.
Everything still works offline; the tiles just render without art.

> Only SteamID64s are accepted, and only 17-digit `7656119…` values. Bots carry
> synthetic `9007…` ids and are filtered out of the player picker.

### Arsenal — default skins per weapon

The **Arsenal** sub-tab lists every weapon, grouped (Pistols, SMGs, Rifles,
Snipers, Heavy), each showing the skin currently assigned. Click a weapon to
browse its finishes and pick one.

This is a *pre-match* loadout: assigning a skin **never gives you the weapon**.
WeaponPaints only paints a gun you buy or pick up normally, so a full arsenal
can be prepared without affecting competitive play.

> **Prerequisite, and a silent one.** CounterStrikeSharp ships with
> `FollowCS2ServerGuidelines: true`, which blocks writes to econ item
> properties. Knife *models* still apply (a different mechanism), so the symptom
> is a correct knife with no finish and no in-game error at all — the exception
> appears only in the server log:
>
> ```
> Cannot set or get 'CEconItemView::m_iEntityQuality' with
> "FollowCS2ServerGuidelines" option enabled
> ```
>
> `stack/bootstrap/configure-menus.sh` sets it to `false`.

### Presets and `!1` … `!9`

Nine slots per player. **Save** snapshots whatever that player currently has —
knife with its finish, gloves, every weapon skin — into `lantern_presets`.
**Apply** copies it back into WeaponPaints' tables.

In game, typing `!1` … `!9` in chat applies that slot. The server confirms in
chat, and the loadout takes effect on `!wp` or the next respawn.

That chat hook needs the console *stream*, because CS2 has no file logging:
`-condebug` is inert and `con_logfile` does not exist. Rather than give this
container the Docker socket, the watcher uses the same websocket the Pelican
console uses — ask Pelican for a short-lived token, connect to Wings, read.

> Wings rejects the upgrade with a bare **HTTP 403** unless the `Origin` header
> is the panel URL. And Pelican rate-limits the token endpoint, so the retry
> backoff starts at 15s — a tight loop turns one failure into a 429 that
> prolongs the outage.

The watcher does double duty: the console prints
`"cyIVER<3><[U:1:1362677841]>"` on player events, which is where the roster gets
SteamIDs that `status_json` leaves blank.

`GET /api/watcher` reports proof-of-life:

```json
{"connected": true, "lines": 20, "chat": 0, "identities": {}, "slots": {}}
```

An empty `identities` map on its own means nothing — it only fills when players
are present. `connected` and a rising `lines` are what show the stream is live.

## Map icons

Real Valve art, extracted from your own install — no external dependency, works
offline, no licensing question.

Source 2 `.vsvg_c` files are a thin binary wrapper around plain SVG text, so the
markup comes out with a byte scan; no ValveResourceFormat or .NET decompiler
needed. All 25 map icons extract cleanly.

They are **not committed** (game assets). Regenerate after a fresh clone:

```bash
cd ui && uv run extract-map-icons.py
```

Pass `--vpk` if CS2 is not at the default path.

---

## API

Useful if you want to script against it or add a tab.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/health` | liveness |
| `GET` | `/api/state` | state, cpu, memory, uptime (via Pelican) |
| `GET` | `/api/config` | egg variables, mode list, allowed maps |
| `GET` | `/api/players` | roster + map + human/bot counts (via RCON `status_json`) |
| `POST` | `/api/command` | `{command}` — runs over RCON, returns output |
| `POST` | `/api/power` | `{signal}` — start / stop / restart / kill |
| `POST` | `/api/map` | `{map, persist}` — `changelevel`, optionally sets boot map |
| `POST` | `/api/mode` | `{mode}` — sets MODE and restarts |
| `PUT` | `/api/variable` | `{key, value}` — any egg variable |
| `POST` | `/api/player` | `{steamid64, name, bot, action, duration}` |
| `POST` | `/api/match/{action}` | start / knife / pause / unpause / warmup / restart |
| `GET` | `/api/loadout/health` | catalogue counts + DB reachability |
| `GET` | `/api/loadout/catalog/knives` \| `/gloves` \| `/weapons` | item lists |
| `GET` | `/api/loadout/catalog/skins/{weapon_name}` | paints for one weapon |
| `GET` | `/api/loadout/{steamid64}` | that player's current selections |
| `POST` | `/api/loadout/knife` \| `/gloves` \| `/skin` | assign |
| `DELETE` | `/api/loadout/{steamid64}` | wipe their loadout |
| `GET` | `/api/presets/{steamid64}` | their nine slots |
| `POST` | `/api/presets` | `{steamid64, slot, name}` — snapshot current loadout |
| `POST` | `/api/presets/apply` | `{steamid64, slot}` |
| `DELETE` | `/api/presets/{steamid64}/{slot}` | delete a slot |
| `GET` | `/api/watcher` | console-stream health |

```bash
curl -s http://192.168.0.115:8090/api/players | python -m json.tool
curl -s -X POST http://192.168.0.115:8090/api/command \
     -H 'Content-Type: application/json' -d '{"command":"status"}'
```

---

## Development

No build step — plain HTML, CSS and JS.

```
ui/
  app/main.py            endpoints, Pelican client, status_json parsing
  app/rcon.py            async Source RCON (CS2 quirks documented inline)
  static/index.html      markup
  static/style.css       the CS2 look; system fonts only, no CDN
  static/app.js          polling, rendering, actions
  static/maps/*.svg      extracted icons (gitignored)
  extract-map-icons.py   the extractor
```

```bash
cd stack && docker compose up -d --build ui
docker compose logs ui --tail 30
```

Credentials come from `ui/.env` (gitignored), regenerable with:

```bash
docker compose exec -T panel php artisan tinker < bootstrap/create-ui-credentials.php
docker compose cp panel:/tmp/lantern-ui.env ../ui/.env
```

## Locking it down

Open by design — see [CONNECTING.md](CONNECTING.md). If you want it gated, the
smallest change is a reverse proxy with basic auth in front of `:8090`, or bind
the published port to `127.0.0.1` in `stack/compose.yml` so it is reachable only
from the host.
