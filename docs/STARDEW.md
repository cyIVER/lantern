# Stardew Valley — always-on farm

A Stardew Valley farm that stays open whether or not anyone is hosting, running
[JunimoServer](https://github.com/stardew-valley-dedicated-server/server) beside
the Pelican stack.

| | |
|---|---|
| Game | Stardew Valley 1.6.15 |
| Players | 8 (PC vanilla limit) |
| VNC console | `http://192.168.0.115:5800` |
| HTTP API | `http://192.168.0.115:8091` |
| Game port | `24642/udp` |
| Mods | SMAPI, from `stardew/mods/` |

---

## What this actually is

**Stardew has no dedicated server.** It never has. Multiplayer is host-based:
one person runs the real game and everyone else joins their session. Close the
host's game and the farm is gone for the evening.

JunimoServer works around that rather than solving it. It runs the **actual
Windows-less game** inside Docker against a virtual display, with a SMAPI mod
that turns the host farmer into an idle bot so the farm stays open. That is why
it needs a Steam login, a VNC console, and 3 GB of memory to host a game about
turnips.

It also means this is **not a Pelican egg**. There is no server binary for
Pelican to supervise, so it lives in its own compose project at `stardew/` and
`stack/lantern` knows about it as a special case.

---

## Setting it up

### 1. Fill in the environment

```bash
cp stardew/.env.example stardew/.env
```

Three values are required: `STEAM_USERNAME`, `STEAM_PASSWORD`, `VNC_PASSWORD`.
Generate the VNC one — it guards a console that can drive the running game, and
anyone on the LAN who reaches port 5800 without it has full control of the farm:

```bash
openssl rand -base64 24
```

### 2. Log in to Steam — this step is yours

Valve does not allow anonymous downloads of paid titles, so the server
downloads Stardew with your account. The first login is interactive and prompts
for a Steam Guard code:

```bash
cd stardew && docker compose run --rm -it steam-auth setup
```

**You type this.** Not a script, not an agent working on this repository. No
automation here will ever handle your Steam password.

### 3. Harden it immediately afterwards

Trade the password for a download-only token:

```bash
docker compose run --rm steam-auth export-token
```

Paste the result into `STEAM_REFRESH_TOKEN` in `stardew/.env` and **blank
`STEAM_PASSWORD`**. The token only authorises Steam downloads, so a leak is far
less damaging than a password that also owns your library and your friends list.

### 4. Start and validate

```bash
cd ../stack && bash bootstrap/setup-stardew.sh
```

Idempotent. It refuses to run until the Steam login exists, and validates by
behaviour: containers up, game files actually downloaded (not an empty volume),
HTTP API answering, VNC listening.

---

## Joining

Unlike Minecraft, nobody types an address. The farm uses Steam's relay network,
so friends join by **invite code**, shown in the VNC console.

Open `http://192.168.0.115:5800`, log in with your `VNC_PASSWORD`, and read the
code off the running game. Then in Stardew: **Co-op → Join → Enter invite code**.

Everyone needs their own copy of Stardew Valley. There is no LAN exception.

---

## Mods

Drop mod folders into `stardew/mods/` and restart. SMAPI loads anything there.

```
stardew/mods/
├── StardewValleyExpanded/
├── Automate/
└── ...
```

### The rule that ruins modded co-op

**Content mods must be installed by every player, at identical versions.**

A farm running Stardew Valley Expanded will break for anyone who does not have
it — the base game cannot render NPCs it has never heard of. Interface and
convenience mods are yours alone and nobody else needs to care.

| Mod | Who installs it |
|---|---|
| SMAPI, Content Patcher | Everyone |
| Stardew Valley Expanded | Everyone |
| Ridgeside Village, East Scarp | Everyone |
| Automate, Tractor Mod | Everyone |
| Generic Mod Config Menu | Client only |
| Lookup Anything, UI Info Suite 2 | Client only |
| NPC Map Locations, Chests Anywhere | Client only |
| Unlimited Players | Host only |

Start with **SVE alone**. Every content mod you add is one more thing each
friend must install at a matching version, and you personally carry that support
burden every time somebody new joins.

Vanilla Stardew 1.6 on PC already supports 8 players. *Unlimited Players* only
matters beyond that.

### Why mod updates are not automated

Nexus Mods requires a **Premium account** for programmatic downloads. The
community tools that work around it violate Nexus's terms of service, so nothing
in this repository scrapes them.

Options, in order of preference: buy Nexus Premium and use the sanctioned API;
prefer CurseForge-hosted Stardew mods, which have a proper free API; or let
SMAPI notify you of updates and apply them by hand. Given every content mod
needs version-matching across all players anyway, manual is less painful than it
sounds.

---

## Running it

```bash
cd stack
./lantern use stardew      # stops CS2 and Minecraft, starts the farm
./lantern status
```

One game server at a time is enforced, not just suggested. Stardew is cheap
compared to Minecraft — 3 GB against 11.5 GB — but the rule is uniform.

```bash
cd stardew
docker compose logs -f server        # follow
docker compose --profile discord up -d   # optional Discord bot
```

### Ports, and why two differ from upstream

| | Upstream | Here | Why |
|---|---|---|---|
| Query | 27015 | **27030** | CS2 owns 27015 in Pelican |
| HTTP API | 8080 | **8091** | Wings owns 8080 |
| VNC | 5800 | 5800 | |
| Game | 24642 | 24642 | |

`setup-stardew.sh` fails the preflight if either reverts to the upstream value.

### Performance

`SERVER_TPS` defaults to **30** here rather than 60. It roughly halves CPU on a
farm that is idle most of the time. `SERVER_FPS` is **0** — rendering is off
entirely; raise it only while you are actually watching the VNC console, since
drawing frames nobody sees is pure waste.

---

## Image versions

The stable `latest` tag was last built **2026-01-07**. The `preview` channel
ships daily and is what upstream suggests when stable misbehaves — the project
describes itself as "under heavy development".

`IMAGE_VERSION` in `stardew/.env` selects. Pin a specific build rather than
tracking a floating tag:

```bash
IMAGE_VERSION=1.5.0-preview.127
```

One risk worth knowing: `steam-auth` downloads whatever Stardew version Steam
currently serves. If the game updates past what a months-old server image
supports, the fix is to move to `preview`, not to wait.

---

## Backups

```bash
bash bootstrap/backup.sh stardew
```

Saves live in the `saves` Docker volume, which is inside the WSL `.vhdx`.
Backups are written to **E:** on purpose — a corrupted `.vhdx` would otherwise
take the farm and every backup of it at the same time.

---

## Related

- [SECRETS.md](SECRETS.md) — Steam credentials and why they never become GitHub secrets
- [MINECRAFT.md](MINECRAFT.md) — the other modded server
- [DECISIONS.md](DECISIONS.md) — why the stack is shaped this way
