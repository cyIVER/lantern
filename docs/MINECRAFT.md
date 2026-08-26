# Minecraft — All the Mods 10

A modded Minecraft server on the LANtern stack. NeoForge, Minecraft 1.21.1,
Java 21, roughly 485 mods, pinned to one exact CurseForge release.

| | |
|---|---|
| Pack | [All the Mods 10](https://www.curseforge.com/minecraft/modpacks/all-the-mods-10) |
| Pinned version | `8.0` — CurseForge file `8649077` |
| Loader | NeoForge `21.1.247` |
| Address | `192.168.0.115:25565` |
| Slots | 8 |
| Server heap | 10 GB (11 GB container) |

---

## Joining — read this before you try

**This is not CS2.** You cannot just type an address. Every player runs the same
485 mods as the server, at the same version, or the connection is refused.

### 1. You need Minecraft Java Edition

Not Bedrock. Not the Microsoft Store version, not console, not mobile. Those
cannot join a NeoForge server at all — not "with difficulty", not at all.

### 2. Install Prism Launcher

[prismlauncher.org](https://prismlauncher.org/) — free and open source. Sign in
with your Microsoft account under **Accounts → Add Microsoft account**.

### 3. Add the pack

**Add Instance → CurseForge → search "All the Mods 10" → Version 8.0**

Pick version **8.0** explicitly. The newest is not always what the server runs;
the table above is authoritative, and the `#minecraft` Discord channel announces
every change.

### 4. Expect seven manual downloads

Seven of the pack's 485 mods have third-party downloads disabled by their
authors, so Prism cannot fetch them for you. It opens a browser tab for each
one and waits. Download each jar and Prism picks it up.

This happens **once**, on first install. It is annoying and it is not something
going wrong.

### 5. Give it memory

**Instance → Edit → Settings → Memory**, set maximum to **8192 MB**. The default
is far too small for a 485-mod pack and the game will crash or run terribly.

### 6. Connect

**Multiplayer → Add Server →** `192.168.0.115:25565`

First join takes a minute or two while the client generates its mod registry.

---

## Running it

Everything is scripted. On the VM (`ssh lantern`), from `/opt/lantern/stack`:

```bash
bash bootstrap/setup-minecraft.sh
```

Idempotent — builds and imports the egg, creates the server if absent, waits for
the 1.1 GB install, starts it and validates. Safe to re-run after any failure.

To check an existing server without changing anything:

```bash
bash bootstrap/setup-minecraft.sh --validate
```

### What the validation actually checks

It deliberately never trusts the panel's status field. During development the
install script died in under a second, Wings logged *"completed installation
process"*, and the panel showed the server as ready with an empty data
directory. Status fields lie; artifacts do not.

| Check | Why this one |
|---|---|
| `lantern/installed.json` exists | The install script only writes it as its last act |
| `libraries/**/unix_args.txt` exists | The launch command reads this file; no file, no boot |
| 400+ mod jars staged | Catches a truncated or partial extract |
| Log contains `)! For help, type` | The server genuinely finished loading the world |
| RCON answers `list` | It is serving, not merely running |
| Port 25565 accepts a connection | The thing players actually type works |

### Ad-hoc commands

```bash
python3 bootstrap/mc-rcon.py 127.0.0.1 25575 "$RCON_PASSWORD" "list"
python3 bootstrap/mc-rcon.py 127.0.0.1 25575 "$RCON_PASSWORD" "op YourName"
```

Read the password from the panel, or:

```bash
docker compose exec -T panel php artisan tinker --execute="
  \$s = \App\Models\Server::where('name','LANtern Minecraft')->firstOrFail();
  foreach (\$s->variables as \$v) if (\$v->env_variable === 'RCON_PASSWORD') echo \$v->server_value;
"
```

---

## Memory, and why one server at a time

Two budgets now, because the server lives in a VM.

**Inside the VM** — 18 GB assigned, about 17 GB usable:

```
 1.5 GB   Pelican, Wings, MariaDB, Redis, the control UI
11.0 GB   Minecraft server (10 GB heap + 1 GB JVM overhead)
───────
12.5 GB   of ~17, before Ubuntu's own footprint
```

CS2 wants 8 GB of that same pool, so the two do not fit together. `lantern use
minecraft` stops CS2 first for exactly this reason.

**On the Windows host** — 32 GB, of which the VM takes its full 18 GB for as long
as it is running, whether or not a game server is up inside it:

```
18.0 GB   the lantern VM
 6.0 GB   Windows and background apps
10.0 GB   your Minecraft client
───────
34.0 GB   of 31.8
```

That does not fit, and it is worth being blunt about: playing a heavily modded
client on the same box that hosts the server is tighter than it was, because the
VM's 18 GB belongs to the VM the whole time it is running rather than shrinking
when the server inside it is idle. Windows starts swapping, which feels like the
whole machine breaking rather than one game being slow. Close what you can, or
lower `VM_RAM_MB` and the server heap together.

The 1 GB of JVM headroom is not padding. Metaspace, code cache, GC structures
and direct buffers all live outside `-Xmx`, and 485 mods carry a large class
footprint. Exceed the container limit and the VM's kernel OOM-kills the process with
no Java stack trace at all — a genuinely miserable thing to debug. Raise
`JVM_HEADROOM_MB` if that ever happens.

`VIEW_DISTANCE` (8) and `SIMULATION_DISTANCE` (6) are the two settings that
actually move server memory. Nobody notices 8 chunks; everybody notices the
stutter at 12 with eight players loading chunks at once.

---

## Updating the pack

**Not automatic, on purpose.**

The server and every client must run the identical version. Nothing on this
machine can update your friends' launchers, so an unattended server upgrade does
not remove coordination — it forces it, at whatever hour the job fires, with
everyone locked out until they work out why. Pack updates also *remove* mods,
and Minecraft resolves a missing mod by deleting the affected blocks and items
from the world on load.

So the pipeline is: detect automatically, announce automatically, merge
deliberately.

1. `.github/workflows/pack-update.yml` checks CurseForge weekly
2. A newer stable release opens a PR moving the pin, and posts to Discord
3. You read the changelog — **specifically for removed mods**
4. Back up the world
5. Merge, then reinstall on the host and re-validate
6. Announce the new version so everyone updates Prism to match

To check by hand:

```bash
CURSEFORGE_API_KEY=... python tools/check-pack-update.py
```

Exit `0` means current, `10` means a newer release exists.

---

## How the egg differs from the stock modpack eggs

Pelican's CurseForge eggs install from the **client** manifest: a list of 485
project/file ids that the installer resolves one at a time. That cannot work
here — seven of those mods block third-party downloads, so an unattended install
stalls forever on jars it is not permitted to fetch.

This egg installs the **server pack** instead: 1.1 GB with every jar already
inside it, published by the pack authors for exactly this purpose.

Two other things worth knowing if you ever edit it:

- The install container is `eclipse-temurin:21-jdk`, **not** a `yolks` image.
  Yolks are runtime images that drop to an unprivileged user, which cannot read
  the install script Wings writes as root-only. The container dies with
  "Permission denied" in under a second and Wings reports success anyway.
- The CurseForge API hands out `edge.forgecdn.net` URLs, which 404 on ranged
  requests. The install script rewrites them to `mediafilez.forgecdn.net`.

Rebuild after editing `eggs/src/mc-*.sh`:

```bash
python eggs/build-mc-egg.py
```

CI fails if the generated JSON drifts from its source.

---

## Related

- [SECRETS.md](SECRETS.md) — the CurseForge API key and the Discord webhook
- [CONNECTING.md](CONNECTING.md) — how friends reach the CS2 server
- [DECISIONS.md](DECISIONS.md) — why the stack is shaped this way
