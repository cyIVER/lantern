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

### The server browser

The server does not advertise itself in the public **Community** browser, by
design: it runs `sv_lan 1` with no Game Server Login Token, so it is invisible
outside your house.

Whether it shows up in Steam's **LAN** tab is **untested**. It used to be
impossible — the server sat behind WSL2's NAT and the LAN broadcast never left
the machine — and that obstacle is gone now that the stack runs on a bridged VM
with its own address. Nobody has checked since. Typing the connect string always
works, so that is what to send people.

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

**2. Is the VM up?**
`192.168.0.115` belongs to the `lantern` VM, not to Windows. If nothing at all
answers — not the panel, not the control UI, not ping — start it from Windows:

```powershell
VBoxManage startvm lantern --type headless
```

Nothing starts it automatically, so this is the normal state after a Windows
reboot.

**3. Is the server running?**
Check the state pill at <http://192.168.0.115:8090>. If it says `offline`, hit
**Start**. It takes ~40 seconds to report `running`.

**4. Guest-network isolation.**
Many routers put guest WiFi on an isolated subnet that cannot see wired devices.
Move them to the main WiFi.

**5. Version mismatch.**
CS2 clients auto-update; the server validates against Steam on every boot. If Valve
patched very recently and the server has not restarted since, restart it.

Note what is **not** on this list any more: Windows Firewall, the Hyper-V firewall,
port proxies. The VM is bridged onto the LAN and holds its own address, so its
traffic never traverses the Windows network stack and nothing on Windows can block
it. The VM's own firewall is Ubuntu's `ufw`, left inactive by the cloud image —
check with `sudo ufw status` if you suspect otherwise. If a port is not answering,
the service behind it is down.

---

## Playing on the Windows box

You can play on the Windows host itself — the server is in a VM and does not
conflict with your game client. Connect to `192.168.0.115:27015` exactly like
everyone else. `localhost:27015` does **not** work any more: localhost on Windows
is no longer where the server lives.

The VM holds 18 GB of the host's 32 GB and 12 of its 20 logical cores whenever it
is running, whether or not a game server is up inside it. That is the budget you
are playing around; power the VM off if you want the whole machine back.

---

## Facts worth knowing

| | |
|---|---|
| Address | `192.168.0.115` — **static on the VM**, will not change across reboots |
| Slots | 12 (bots fill empty spots and leave as humans join) |
| Password | none |
| VAC | on — normal Steam accounts, no bans from playing here |
| Demos | every match auto-records to `game/csgo/replays/` |
| Skins | `!ws`, `!knife`, `!gloves` in chat — everyone has everything |

## UDP works now

Worth stating plainly, because for most of this project's life it did not. CS2
gameplay is UDP on 27015, and under WSL2 it could not be published to the LAN at
all: the NAT relay bound IPv6 loopback only, mirrored mode handed inbound traffic
to a Hyper-V firewall that defaults to Block, and none of the workarounds covered
UDP.

On the bridged VM there is nothing in the path. UDP 27015 has been reached from
two separate machines on the LAN and gameplay works. If someone cannot connect,
it is not this.
