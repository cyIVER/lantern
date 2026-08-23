# Using LANtern

Panel: **http://192.168.0.115** — log in as `iveri@lantern.lan`
Connect: **`connect 192.168.0.115:27015`**

Click **LANtern CS2** in the server list. The tabs across the server view are
Console, Files, Databases, Schedules, Users, Backups, Network, Startup, Settings.

---

## Change the mode or map

**Startup tab.** Every setting is a form field:

| Field | Control | Notes |
|---|---|---|
| **Mode** | dropdown | `competitive` / `retakes` / `practice` / `deathmatch` |
| **Map** | dropdown | 21 maps, all verified present in the install |
| Max Players | text | 12 |
| Bot quota | text | 0–32, bots fill empty slots and leave as humans join |
| Bot difficulty | text | 0 easy → 3 expert |
| Server Name | text | shown in the scoreboard |
| Server Password | text | blank = open |
| Enable weapon skins | toggle | WeaponPaints; database already wired up |
| Enable RCON / VAC | toggle | |
| Auto-update server | toggle | validates against Steam on every boot |

Change a value, then hit **Restart**. Takes about 40 seconds — `boot.sh` reloads
the config and swaps the plugin set to match the mode.

### What each mode actually does

| Mode | Plugin | Behaviour |
|---|---|---|
| `competitive` | MatchZy | MR12, knife round, `.ready`, pauses, demo recording, CSTV on |
| `retakes` | CS2-Retakes | continuous retake rounds, 3s freeze |
| `practice` | CSPracc | `sv_cheats 1`, infinite ammo, $65535, no round timer, no bots |
| `deathmatch` | none | vanilla DM, admin tools only |

> MatchZy, Retakes and CSPracc each take over round flow, so exactly one is ever
> loaded. Switching mode unloads the other two — verified by
> `stack/bootstrap/test-modes.sh`.

## Change the map without restarting

**Console tab** — it is a live server console with RCON:

```
changelevel de_mirage
```

Any cvar works there too: `mp_freezetime 10`, `bot_quota 6`, `mp_restartgame 1`.
Changes made this way last until the next restart; the Startup tab is what
persists.

## In-game commands

**MatchZy** (competitive mode) — type in chat:

| Command | Effect |
|---|---|
| `.ready` / `.unready` | ready up; match starts when both teams are ready |
| `.start` | force start |
| `.pause` / `.unpause` | tactical pause |
| `.stop` | restore the round (needs both teams) |
| `.settings` | show current match settings |

Demos record automatically to `game/csgo/replays/`, reachable from the Files tab.

**CS2-SimpleAdmin** — `!` in chat or `css_` in console: `!kick`, `!ban`, `!mute`,
`!slay`, `!swap`, `!map`, `!admin`.

> Admin commands only work for Steam IDs listed in SimpleAdmin's admin config.
> See "Granting yourself admin" below.

**Weapon skins** — `!ws` opens the skin menu, `!knife` the knife menu, `!gloves`
for gloves. Choices persist per Steam ID in the `cs2_weaponpaints` database.

## Granting yourself admin

SimpleAdmin ships with no admins, so `!` commands do nothing until your SteamID64
is registered. Find yours at steamid.io, then either use the Files tab to edit

```
game/csgo/addons/counterstrikesharp/configs/plugins/CS2-SimpleAdmin/
```

or run `bash stack/bootstrap/add-admin.sh <steamid64> <name>`.

## Other panel tabs

- **Files** — full file manager. Edit any cfg, drop in maps or plugins, download demos.
- **Users** — add friends as subusers with limited permissions (restart, change map)
  without giving them full admin.
- **Schedules** — cron-style tasks, e.g. a nightly `Auto-update` restart.
- **Backups** — snapshot the server (2 allowed).
- **Network** — the port allocations: 27015 game, 27020 CSTV.

## Gotchas

- `lantern.cfg` is **regenerated on every boot**. Do not edit it; change the
  Startup variables instead, or put permanent overrides in a separate cfg.
- Changing **Mode** or **Map** in the Startup tab needs a restart to take effect.
- After a Windows reboot the panel comes back automatically via the
  "LANtern startup" scheduled task, but **game servers stay off by design** —
  start them from the panel.
