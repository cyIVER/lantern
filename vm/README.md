# vm/

Everything about the machine LANtern runs *on*, rather than the stack that runs
inside it. Three groups: the scripts that built the VM and moved the stack onto
it, the ones that service it day to day, and the ones that back it up.

The VM is `lantern` — VirtualBox, Ubuntu 26.04 LTS, 18 GB RAM (about 17.6 GB
usable), 12 vCPUs, bridged onto the host's Realtek USB 2.5GbE adapter, holding
`192.168.0.115` statically. On the VM the repo lives at `/opt/lantern`.

Where a script runs matters and is never obvious from the filename, so it is the
first column below.

| Script | Runs on | What it does |
|---|---|---|
| `Start-LANtern.cmd` | Windows | `VBoxManage startvm lantern --type headless`. What the Desktop shortcut points at. |
| `windows-setup.ps1` | Windows, elevated | Points the Windows host at the VM: retires the old WSL autostart, writes the shortcut, registers the nightly backup. `-DisableHypervisor` turns the Windows hypervisor off. |
| `reclaim-space.ps1` | Windows, elevated | Unregisters the WSL distros and deletes `E:\DockerData` and `E:\WSL`. Already run — it freed 449 GB. |
| `backup-pull.ps1` | Windows | Runs `backup-all.sh` on the VM and copies the result to `D:\LANtern-Backups\data`. The scheduled task runs this. |
| `export-vm-image.ps1` | Windows | Exports the whole VM to a `.ova` in `D:\LANtern-Backups\images`. The VM must be off. |
| `backup-all.sh` | the VM | The nightly data backup: databases, node token, worlds, saves, `.env` files. |
| `install-vm-services.sh` | the VM | Installs `lantern-dbnet.timer`, creates the swap file, symlinks `lantern` onto `PATH`. |
| `normalize-line-endings.sh` | the VM | Strips CR from a tree copied over from Windows. |
| `build-lantern-vm.sh` | historical | Built the VM from Ubuntu's cloud image. |
| `make-seed-iso.py` | historical | Builds the cloud-init NoCloud seed ISO `build-lantern-vm.sh` hands the VM. |
| `migrate-to-vm.sh` | historical | Moved volumes, `/etc/pelican` and the repo off the WSL stack. |
| `cutover.sh` | historical | Handed `192.168.0.115` from Windows to the VM. |
| `cloud-init/` | — | `user-data.tmpl`, `meta-data`, `network-config` — the seed's contents. |

> **The four marked historical were written to run from WSL**, and there is no
> WSL on this machine any more — both distros were unregistered. They document
> how the VM came to exist and are the starting point for rebuilding it
> somewhere else; they are not runnable here as written.

---

## Day to day

There is exactly one thing to do by hand: start the VM.

```
Desktop -> "Start LANtern"
```

which runs `Start-LANtern.cmd`. **Nothing autostarts the VM** — `windows-setup.ps1`
unregistered the old "LANtern startup" scheduled task and removed Docker Desktop
from the Run key, and deliberately put nothing in their place. A Windows reboot
therefore leaves LANtern off until you ask for it.

Once the VM boots, the compose services come back on their own
(`restart: unless-stopped`), and **Wings restores whichever game server was
running when the VM went down**. So "nothing autostarts" is true of the VM and
not of the game inside it; check the landing page rather than assuming.

Stopping it:

```powershell
VBoxManage controlvm lantern acpipowerbutton
```

`acpipowerbutton`, not `poweroff` — the latter is the equivalent of pulling the
plug on a machine holding a live MariaDB and a Minecraft world.

---

## Backups

Two different questions, answered by two different scripts.

**"Can I get the data back?"** — `backup-pull.ps1`, nightly.

A Windows scheduled task named **"LANtern backup"** runs it at **03:00 daily**.
It exits quietly and successfully when the VM is off, which is the normal case
now that the VM is started by hand. `windows-setup.ps1` registers that task.

It SSHes to the VM, runs `backup-all.sh` there, and copies the resulting dated
directory to `D:\LANtern-Backups\data` as plain files — keeping 14 sets. Both
halves matter:

- **Produced on the VM**, because only the VM can dump MariaDB consistently and
  quiesce Minecraft over RCON. A tar of a live datadir restores to something that
  looks fine until it does not.
- **Pulled to D: as plain files**, because the failure being insured against is
  losing the VM. D: is the Toshiba HDD; the VM lives on the Samsung SSD — a
  different physical disk, which is the whole point. A backup you can only read
  by booting the thing that died is not a backup.

The set is roughly 165 MB and covers everything that cannot be downloaded again:

| | |
|---|---|
| panel database | including `cs2_weaponpaints`, where every loadout and preset lives |
| `/etc/pelican` | Wings' node token; without it the panel cannot re-adopt its own node |
| Minecraft world | quiesced over RCON first, so nobody is kicked |
| CS2 `cfg` + `addons` | CounterStrikeSharp, WeaponPaints and their configuration |
| Stardew saves + config | the farm, and the SMAPI config beside it |
| `.env` files | gitignored, so they exist nowhere else |

CS2's ~67 GB of game content and Stardew's game install are deliberately not in
there. SteamCMD fetches both again on demand.

> `config.tgz` inside each set holds every password in the stack. It is written
> mode 600 on the VM, and it lands on D: as an ordinary file. Treat
> `D:\LANtern-Backups` as a secret store. See [../docs/SECRETS.md](../docs/SECRETS.md).

Run one by hand:

```powershell
powershell -ExecutionPolicy Bypass -File vm\backup-pull.ps1
```

Or, from a shell on the VM, just the VM-side half:

```bash
bash /opt/lantern/vm/backup-all.sh
```

`stack/bootstrap/backup.sh` is the single-game version of the same idea — quick
to reach for before a risky change to one server. It writes to
`/var/backups/lantern` and stays on the VM.

**"How long until we are playing again?"** — `export-vm-image.ps1`, by hand.

Restoring from data alone means rebuilding the VM, reinstalling Docker,
re-adopting the Wings node and re-downloading CS2: an evening. Importing an OVA
is one command and a wait. Run it before anything risky — a modpack update, a
Pelican upgrade, a hypervisor change:

```powershell
powershell -ExecutionPolicy Bypass -File vm\export-vm-image.ps1 -StopVm
```

The VM must be off; `-StopVm` asks the guest to shut down cleanly and waits.
Expect roughly 25–40 GB and 15–30 minutes, and only two images are kept.

> **After importing an OVA, re-point the router's DHCP reservation** at the
> imported VM's new MAC, or set the MAC back to the original with
> `VBoxManage modifyvm lantern --macaddress1 080027F263BA`. The reservation is
> what stops the router handing `.115` to something else. See
> [../docs/ROUTER-MCP.md](../docs/ROUTER-MCP.md).

> `backup-pull.ps1`'s own `.NOTES` block says to register the schedule with
> `vm\register-backup-task.ps1`. **That script does not exist** — the task is
> registered by `windows-setup.ps1`. Stale comment, not a missing file.

---

## Host services on the VM

`install-vm-services.sh` is idempotent and safe to re-run. It installs three
things.

**`lantern-dbnet.timer`** — the one that matters. Wings puts game servers on its
own bridge, `pelican_nw`, which MariaDB is not attached to, so WeaponPaints
inside the CS2 container cannot resolve the hostname `database` and silently
falls back to no skins at all. Nothing logs an error an operator would see; the
loadout UI just returns defaults. The timer re-attaches the database container
every 60 seconds, idempotently. It cannot be a compose dependency: Wings creates
that network itself and only once it starts, so an `external: true` reference
fails on a machine where no game server has ever run.

**4 GB of swap, at `vm.swappiness=10`.** Minecraft is allocated 11 GB on a
17.6 GB box. Without swap a transient spike does not degrade — the kernel OOM
killer picks a process and ends it, with no warning and typically mid-save. The
swap file turns that into a stutter, and into a signal: the landing page's swap
gauge shows the first percent of use, so "you are over-allocated" is something
you can see coming. `swappiness` is 10 rather than the default 60 because this is
a shock absorber, not tiered memory; at 60 the kernel pages out idle game-server
heap during normal play and you feel it.

**The `lantern` symlink**, so `lantern status` and `lantern use cs2` work from
any directory on the VM.

---

## The hypervisor

The Windows hypervisor is **off** (`bcdedit /set hypervisorlaunchtype off`), which
is what gives VirtualBox native VT-x rather than nesting it inside the Windows
Hypervisor Platform. `windows-setup.ps1 -DisableHypervisor` is what set it, and
it refuses to proceed while Memory Integrity (Core Isolation) is on, because that
holds VT-x regardless.

**WSL2 and Docker Desktop do not work on this machine while it is off.** That is
the price and it was paid deliberately; the measurements are in
[../docs/DECISIONS.md](../docs/DECISIONS.md). To undo it:

```powershell
bcdedit /set hypervisorlaunchtype auto     # then reboot
```

---

## Related

- [../docs/DECISIONS.md](../docs/DECISIONS.md) — why the VM, why the hypervisor trade, what it measured
- [../stack/README.md](../stack/README.md) — the stack that runs inside the VM
- [../docs/SECRETS.md](../docs/SECRETS.md) — what is in the backups and how to treat it
