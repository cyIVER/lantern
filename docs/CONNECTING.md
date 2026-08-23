# Connecting

Everything below assumes you are **on the same network** — wired or on the house
WiFi. Nothing here is exposed to the internet and no port forwarding is involved.

---

## Send your friends this

> **CS2 → `connect 192.168.0.115:27015`**
>
> Open the console with **`~`** and paste that in. If `~` does nothing, enable the
> console first: **Settings → Game → Enable Developer Console → Yes**.

That's the whole thing. No password, no mods to install, no Steam group.

### Why it isn't in the server browser

The server runs in Docker behind WSL2's NAT, so the LAN broadcast that populates
Steam's **LAN** tab never reaches other machines. The server is perfectly
reachable — it just cannot announce itself. Typing the connect string is the
supported path here, not a workaround for something broken.

It also won't appear in the public **Community** browser, by design: it runs
`sv_lan 1` with no Game Server Login Token, so it is invisible outside your house.

### Make it one click for them

In CS2, a console alias saves retyping:

```
alias lan "connect 192.168.0.115:27015"
```

Then they just type `lan`.

---

## Spectating without taking a slot

CSTV runs on **27020**. A spectator connects with:

```
connect 192.168.0.115:27020
```

They see the match on a ~30 second delay and do **not** consume one of the 12
player slots. Useful for someone casting or just watching on a spare screen.

---

## The web interfaces

| | | |
|---|---|---|
| 🎮 **CS2 Control** | <http://192.168.0.115:8090> | **No login.** Anyone on the LAN can change maps, modes and kick people. |
| 🛠 **Pelican Panel** | <http://192.168.0.115> | Login required — `iveri@lantern.lan` |

The control UI being open is a deliberate choice for a trusted LAN party. If you
want it locked down, see [CONTROL-UI.md](CONTROL-UI.md).

Give a friend limited panel access instead of your password: **Pelican → server →
Users → invite**, and grant only what they need (restart, change map) without file
access or the ability to delete anything.

---

## If someone can't connect

Work down this list.

**1. Are they actually on your network?**
Have them run `ipconfig` (Windows) or `ifconfig` (Mac). Their address should start
`192.168.0.`. A `192.168.1.x` or `10.x` address means they are on a different
router — a guest network, a phone hotspot, or a mesh node in isolation mode.

**2. Is the server running?**
Check the state pill at <http://192.168.0.115:8090>. If it says `offline`, hit
**Start**. It takes ~40 seconds to report `running`.

**3. Guest-network isolation.**
Many routers put guest WiFi on an isolated subnet that cannot see wired devices.
Move them to the main WiFi.

**4. Windows Firewall.**
Docker Desktop installs inbound allow rules covering both the Private and Public
profiles, so this normally just works. If it does not, the LAN is currently
classified **Public**, which is the strictest profile. Reclassifying it as Private
is correct for a home network — run elevated:

```powershell
Set-NetConnectionProfile -InterfaceAlias "Ethernet 5" -NetworkCategory Private
```

If you would rather add explicit rules than change the profile:

```powershell
New-NetFirewallRule -DisplayName "LANtern CS2"   -Direction Inbound -Action Allow `
  -Protocol UDP -LocalPort 27015,27020 -RemoteAddress 192.168.0.0/24
New-NetFirewallRule -DisplayName "LANtern CS2 TCP" -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort 27015 -RemoteAddress 192.168.0.0/24
New-NetFirewallRule -DisplayName "LANtern Web"   -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort 80,8090 -RemoteAddress 192.168.0.0/24
```

**5. Version mismatch.**
CS2 clients auto-update; the server validates against Steam on every boot. If Valve
patched very recently and the server has not restarted since, restart it.

---

## Playing on the host machine

You can play on `IVERSON_PC` itself — the server is a separate container and does
not conflict with your game client. Connect to `192.168.0.115:27015` exactly like
everyone else, or use `localhost:27015`.

Budget roughly 8 GB RAM and a couple of cores for the server while you play. The
i5-13600KF has 20 threads, so it comfortably does both.

---

## Facts worth knowing

| | |
|---|---|
| Address | `192.168.0.115` — **static**, will not change across reboots |
| Slots | 12 (bots fill empty spots and leave as humans join) |
| Password | none |
| VAC | on — normal Steam accounts, no bans from playing here |
| Demos | every match auto-records to `game/csgo/replays/` |
| Skins | `!ws`, `!knife`, `!gloves` in chat — everyone has everything |
