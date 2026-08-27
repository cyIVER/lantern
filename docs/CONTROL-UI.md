# The control UIs

One FastAPI service on `:8090` serves two pages, and a second application on
`:8092` serves the farm.

| | | |
|---|---|---|
| **LANtern landing page** | <http://192.168.0.115:8090/> | Which game is running, buttons to switch, and the host dashboard |
| **CS2 control** | <http://192.168.0.115:8090/cs2> | Players, maps, mode, match control, loadouts, RCON console |
| **Stardew control** | <http://192.168.0.115:8092> | Its own application in `stardew-ui/` — see [STARDEW.md](STARDEW.md) |
| **Minecraft control** | `:8093` | Reserved, being built separately. Not deployed |

No login on any of them.

**The CS2 UI moved.** It used to be served at `:8090` itself; the landing page
took the root and the CS2 UI is at `/cs2`. Nothing else moved — `/api/...`,
`/static/...` and the whole CS2 backend are on the same origin at the same paths,
so the move cost the CS2 page nothing.

Every game UI carries a **← LANtern** link back to the landing page. The landing
page links out to each game UI rather than embedding it: they are separate
applications with their own themes, and wrapping them would mean one owning the
others' chrome for no gain.

Pelican is still there for everything the panel does better — files, backups,
schedules, subusers, creating servers. None of this replaces it; it sits on top.

---

## The landing page

Two blocks.

**Game servers.** One card per game, showing state, a Start or Stop button, the
connect string when it is up, and a link to that game's own UI.

Only one game runs at a time — CS2 is allocated 8 GB and Minecraft 11 GB on a box
with about 17.6 GB usable, so two at once means the kernel OOM killer ends one of
them mid-save. That rule lives in the control service, not in the page. Starting a
game while another is running gets an **HTTP 409** whose body names what would be
stopped and who would be disconnected; the page shows that in a confirmation
dialog and only then re-sends with `confirm=true`.

Not `window.confirm()`, deliberately: the message has to name the server being
stopped and say what happens to the people on it, and a browser dialog cannot be
made to look as consequential as it is.

The switch stops the others, **waits for them to actually be down**, and only then
starts the new one — a start issued while the previous server is still releasing
its memory is how the box gets overcommitted. If something refuses to stop within
90 seconds it refuses to start anything, rather than proceeding into the state the
whole rule exists to prevent.

**Host.** CPU (a sampled rate, with a sparkline), memory, disk, load per core,
swap, uptime, and the container list.

A few of those numbers are more careful than they look:

- **CPU is a rate, not a reading.** `/proc/stat` gives cumulative jiffies since
  boot, so one read says nothing about now. The first poll after a restart has
  nothing to compare against and returns `null` rather than a made-up zero; the
  page shows a dash until the second poll.
- **Load is shown per core.** 4.0 is idle on twelve cores and on fire on two.
- **Memory uses `MemAvailable`, not `MemFree`.** Free memory excludes cache the
  kernel hands back on demand and reads alarmingly low on a perfectly healthy box.
- **Disk is measured through the `/volumes` bind**, which is `/var/lib/pelican` on
  the host — the filesystem game servers actually fill up. The container's own `/`
  is the image and would be useless.
- **Network is deliberately absent.** Network namespaces are per-container, so
  `/proc/net/dev` here describes a compose bridge rather than the LAN. A plausible
  wrong number is worse than no number.
- **Swap matters now.** The VM has 4 GB of it at `swappiness=10`, and any sustained
  use means something is over-allocated. That is what the gauge is for; see
  [DECISIONS.md](DECISIONS.md).

The container list trades Wings' UUID container names for the panel's names where
it knows one, and sorts non-running containers first — the failure the list exists
to make visible is the one restarting in a loop.

The Minecraft UI link is gated on a **server-side TCP probe** of `:8093`. It has to
be server-side: a cross-origin probe from the page cannot tell "nothing listening"
from "listening, but that is not an image", so it would report every port as up.
A TCP connect knows the difference, which is why the link can sit in the code
before the UI exists without ever offering a dead button.

---

## CS2 tabs

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

Switching games needs a third path again, because the three games are not the same
kind of thing. CS2 and Minecraft are Pelican servers, so Wings owns their
containers and they are driven through the panel's client API — their UUIDs looked
up by name at runtime rather than configured, because a UUID in a `.env` is one
more thing to get wrong after a rebuild and the panel already knows the answer.
Stardew is not a Pelican server at all; it is a separate compose project, driven
straight through the Docker socket. That is why the `ui` container has the socket
mounted, and why Stardew reports as unavailable rather than breaking the page if
it ever is not.

`sdvd-ui` — the Stardew control UI — is started with the farm and **not** stopped
with it. A management UI that disappears whenever the thing it manages is off is a
management UI you cannot use for the one thing you most need it for.

## Pages are served `no-cache`

Both UIs set `Cache-Control: no-cache` on their pages and static files, so a
deploy is visible on the next reload.

It does not mean "do not cache" — it means revalidate before use. The ETag still
works, so an unchanged file is a 304 with no body, which on a LAN is free.

This is here because it cost real time twice: a fix was deployed, verified as
being served correctly by the server, and was still absent in the browser. That
sends you looking for a bug in the deploy that is not there.

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

They are **not committed** (game assets). Regenerate after a fresh clone, on a
machine with CS2 installed:

```bash
cd ui && uv run extract-map-icons.py
```

Pass `--vpk` if CS2 is not at the default path.

The stack runs on the `lantern` VM, which has no CS2 *client* install — so run the
extractor on Windows and copy the result across:

```powershell
scp ui\static\maps\*.svg lantern:/opt/lantern/ui/static/maps/
```

Whether the extractor can read the icons out of the dedicated server's own game
files on the VM is untested.

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

Two more belong to the landing page rather than to CS2, and act on all three games:

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/servers` | every game's state, plus `running` — and `ui_up` where a game has its own UI |
| `POST` | `/api/servers/{game}/start` | `{confirm}` — **409 unless `confirm` is true** when something else is running |
| `POST` | `/api/servers/{game}/stop` | stop one game |
| `GET` | `/api/host` | cpu / memory / disk / load / swap / uptime / containers |

`{game}` is `cs2`, `minecraft` or `stardew`. The 409 body is structured, not a
string — it carries `would_stop`, `would_stop_labels` and a `message` written for a
human:

```json
{"needs_confirm": true, "game": "stardew", "label": "Stardew Valley",
 "would_stop": ["cs2"], "would_stop_labels": ["Counter-Strike 2"],
 "message": "Starting Stardew Valley will shut down Counter-Strike 2. Anyone connected will be disconnected."}
```

Anything calling this must show that message and re-send with `confirm=true`,
rather than setting `confirm` on the first request. That is the entire safety
interlock; a client that always confirms has removed it.

```bash
curl -s http://192.168.0.115:8090/api/players | python -m json.tool
curl -s http://192.168.0.115:8090/api/servers | python -m json.tool
curl -s -X POST http://192.168.0.115:8090/api/command \
     -H 'Content-Type: application/json' -d '{"command":"status"}'
```

---

## Development

No build step — plain HTML, CSS and JS.

```
ui/
  app/main.py            endpoints, Pelican client, status_json parsing, routing
  app/rcon.py            async Source RCON (CS2 quirks documented inline)
  app/servers.py         the one-server-at-a-time rule, for all three games
  app/host.py            /proc and Docker readings for the host dashboard
  app/loadout.py         WeaponPaints tables
  app/presets.py         the nine preset slots
  app/watcher.py         the Wings console stream
  static/shell.html      the LANtern landing page
  static/shell.css       its look
  static/shell.js        its polling, cards and confirmation dialog
  static/index.html      the CS2 UI
  static/style.css       the CS2 look; system fonts only, no CDN
  static/app.js          polling, rendering, actions
  static/maps/*.svg      extracted icons (gitignored)
  extract-map-icons.py   the extractor
```

Adding a game UI to the landing page is one entry in `UIS` in `shell.js` — a
label and a URL builder. The card, the link and the state row are all driven from
`/api/servers` plus that map. A game with no entry gets a panel link instead.

Adding a *game* is one entry in `GAMES` in `app/servers.py`, saying whether it is
Pelican-managed or Docker-managed and, optionally, what port its UI answers on.

On the VM:

```bash
cd /opt/lantern/stack && docker compose up -d --build ui
docker compose logs ui --tail 30
```

Credentials come from `ui/.env` (gitignored), regenerable from `/opt/lantern/stack`:

```bash
docker compose exec -T panel php artisan tinker < bootstrap/create-ui-credentials.php
docker compose cp panel:/tmp/lantern-ui.env ../ui/.env
```

## Locking it down

Open by design — see [CONNECTING.md](CONNECTING.md). Note what that now includes:
anyone on the LAN can **stop the game other people are playing** from the landing
page. It asks first and names what it will shut down, but it does not ask who you
are.

There is no firewall on the VM either, and that is also deliberate — `ufw` cannot
protect Docker-published ports, because Docker inserts its own nftables rules
ahead of ufw's. See [CONNECTING.md](CONNECTING.md).

If you want it gated, the smallest change is a reverse proxy with basic auth in
front of `:8090`, or bind the published port to `127.0.0.1` in
`stack/compose.yml`. Note that gating `:8090` also gates the Stardew UI's power
buttons, which forward to it.

Note what `127.0.0.1` means now: the VM's loopback, reachable only from a shell on
the VM. It is no longer "the machine you are sitting at" — that would want an ssh
tunnel, `ssh -L 8090:127.0.0.1:8090 lantern`.
