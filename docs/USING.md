# Using LANtern

| | |
|---|---|
| 🎮 **CS2 Control** | **<http://192.168.0.115:8090>** — no login. Day-to-day driving. |
| 🛠 **Pelican Panel** | **<http://192.168.0.115>** — `iveri@lantern.lan`. Files, backups, schedules, subusers. |
| 🔫 **Play** | `connect 192.168.0.115:27015` |

**Use the control UI for anything mid-party.** Pelican is for the deeper
plumbing. Both drive the same server and stay in sync — see
[CONTROL-UI.md](CONTROL-UI.md) for how that works.

Friends joining? → [CONNECTING.md](CONNECTING.md)

---

## Change the mode or map

### The fast way — control UI

- **Maps tab** → click a map card. Switches immediately via `changelevel`; the
  "also set as boot map" toggle makes it persist.
- **Game Mode tab** → click a mode. Restarts the server (~40s), because the mode
  decides which plugins load.
- Bot quota and difficulty are sliders; skins / VAC / auto-update are toggles.

### The other way — Pelican Startup tab

Same settings as form fields, then hit **Restart**:

| Field | Control | Notes |
|---|---|---|
| **Mode** | dropdown | `competitive` / `retakes` / `deathmatch` |
| **Map** | dropdown | 21 maps, all verified present in the install |
| Max Players | text | 12 |
| Bot quota | text | 0–32, bots fill empty slots and leave as humans join |
| Bot difficulty | text | 0 easy → 3 expert |
| Server Name | text | shown in the scoreboard |
| Server Password | text | blank = open |
| Enable weapon skins | toggle | WeaponPaints; database already wired up |
| Enable RCON / VAC | toggle | |
| Auto-update server | toggle | validates against Steam on every boot |

### What each mode actually does

| Mode | Round-flow plugin | Behaviour |
|---|---|---|
| `competitive` | MatchZy | MR12, knife round, `.ready`, pauses, demo recording, CSTV on |
| `retakes` | CS2-Retakes 3.1.0 | continuous retake rounds, 3s freeze |
| `deathmatch` | none | vanilla DM, admin tools only |

**Admin commands and skins work in every mode** — only the round-flow plugin
swaps. Every mode loads SimpleAdmin (plus its MenuManager / PlayerSettings /
AnyBaseLib chain) and WeaponPaints, then adds exactly one of MatchZy or Retakes,
never both, because they each take over round flow.

> **`practice` was removed.** CS2-Practice-Plugin v1.0.0.3 is built against a
> 2024 CounterStrikeSharp and **segfaults the server** on current CS2 — which is
> why its archive shipped an entire 2024 runtime. `boot.sh` falls back to
> competitive if the value is somehow still set. It can return if a maintained
> practice plugin appears.

## Change the map without restarting

The control UI's **Maps** tab, or a console:

```
changelevel de_mirage
```

Any cvar works too: `mp_freezetime 10`, `bot_quota 6`, `mp_restartgame 1`.
Console changes last until the next restart; the Startup values persist.

The control UI's **Console** tab uses RCON and **shows you the reply**. Pelican's
console is fire-and-forget and shows nothing back — that difference is why the
control UI has its own.

## In-game commands

**MatchZy** (competitive only) — type in chat:

| Command | Effect |
|---|---|
| `.ready` / `.unready` | ready up; match starts when both teams are ready |
| `.start` | force start |
| `.pause` / `.unpause` | tactical pause |
| `.stop` | restore the round (needs both teams) |
| `.settings` | show current match settings |

Demos record automatically to `game/csgo/replays/`, reachable from Pelican's
Files tab. The control UI's **Match** tab has buttons for all of the above.

**CS2-SimpleAdmin** — `!` in chat or `css_` in console: `!kick`, `!ban`, `!mute`,
`!slay`, `!swap`, `!map`, `!admin`. The control UI's **Players** tab does the
same things with buttons, and shows SteamID64s.

**Weapon skins** — every player has the full catalogue regardless of inventory:

| Command | Opens |
|---|---|
| `!knife` | knife menu |
| `!gloves` | gloves |
| `!ws` | skin for the gun you are holding |
| `!skins` | full skin selection |
| `!agents` | player models |
| `!pin` / `!coin` | profile coins |
| `!music` | music kits |
| `!st` | toggle StatTrak |
| `!wp` | reapply everything without respawning |
| `!kill` | suicide, to respawn into a new loadout |

Knives and gloves only appear on respawn — pick, then `!kill` or wait a round.
Choices persist per SteamID in the `cs2_weaponpaints` database.

**Menus are chat menus**: type the number of the option you want. (They shipped
as *button* menus — navigated with W/S and selected with E — which is why they
looked unresponsive if you tried typing a number. Changed via
`stack/bootstrap/configure-menus.sh`.)

**Or skip the in-game menu entirely**: the control UI's **Loadout** tab assigns
knives, gloves and weapon skins to any player from the browser. See
[CONTROL-UI.md](CONTROL-UI.md).

## Admins

`cyIVER` (`76561199322943569`) is registered with `@css/root` and immunity 100,
so every `!` command works.

Add someone else:

```bash
bash stack/bootstrap/add-admin.sh <steamid64> "<name>" [immunity]
```

It merges into `addons/counterstrikesharp/configs/admins.json` rather than
overwriting, validates the SteamID64 shape, and preserves file ownership. Apply
with a restart or `css_reladmin` in the console.

Give friends a lower immunity than yours so they cannot target you.

SimpleAdmin keeps bans and mutes in SQLite (`cs2-simpleadmin.sqlite`), so it
needs no database of its own.

## Other Pelican tabs

- **Files** — full file manager. Edit any cfg, drop in maps or plugins, download demos.
- **Users** — add friends as subusers with limited permissions (restart, change map)
  without giving them full admin.
- **Schedules** — cron-style tasks, e.g. a nightly `Auto-update` restart.
- **Backups** — snapshot the server (2 allowed).
- **Network** — the port allocations: 27015 game, 27020 CSTV.

---

## Gotchas

- **`lantern.cfg` is regenerated on every boot.** Do not edit it; change the
  Startup variables, or put permanent overrides in a separate cfg.
- **Changing Mode needs a restart.** Map changes do not, if you use
  `changelevel` or the Maps tab.
- **After a Windows reboot** the panel returns automatically via the "LANtern
  startup" scheduled task, but **game servers stay off by design** — start them
  from either UI.
- **Never drop a plugin's full archive into `addons/`.** Some ship their own
  Metamod, CounterStrikeSharp runtime, or the core `gamedata.json`, and will
  overwrite the platform with a stale copy. The staging normaliser strips these,
  but only for plugins installed through the egg. See
  [the writeup](DECISIONS.md) if plugins ever go silent.

## When plugins silently stop working

The symptom is that `!` commands do nothing and no error appears in game.
Metamod or CounterStrikeSharp has failed to load, usually after a CS2 update.

```bash
# what happened
docker logs <server-uuid> 2>&1 | grep -aE "MMS:|\[META\]|Failed to find signature"

# fix: reinstall both, preserving the counterstrikesharp.vdf hook
bash stack/bootstrap/repair-platform.sh
```

Then restart and confirm you get seven `Finished loading plugin` lines.
