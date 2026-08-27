<#
.SYNOPSIS
  Export the whole LANtern VM to D: as a single restorable appliance.

.DESCRIPTION
  The nightly data backup covers the ~2.7 GB that cannot be re-downloaded. This
  covers the other question: how long it takes to be playing again. Restoring
  from data alone means rebuilding the VM, reinstalling Docker, re-adopting the
  Wings node and re-downloading 67 GB of CS2 -- an evening. Importing an OVA is
  one command and a wait.

  Run it before anything risky: a modpack update, a Pelican upgrade, changing
  the hypervisor. That is the point at which the cost of not having one lands.

  THE VM MUST BE OFF. VirtualBox will not export a running machine, and a
  copy of a running VM's disk is a copy of a filesystem mid-write -- it would
  import to something that boots into fsck, if it boots. -StopVm asks the guest
  to shut down cleanly first and waits for it.

  Expect roughly 25-40 GB and 15-30 minutes: the OVA gzips the disk, and D: is
  a spinning disk.

.PARAMETER StopVm
  Shut the VM down cleanly first, export, and leave it off.

.PARAMETER Keep
  How many images to keep. Default 2 -- they are large, and an old whole-VM
  image is far less useful than yesterday's data backup.
#>
[CmdletBinding()]
param(
    [string]$Dest = 'D:\LANtern-Backups\images',
    [string]$VmName = 'lantern',
    [switch]$StopVm,
    [int]$Keep = 2
)

$ErrorActionPreference = 'Stop'
$vbm = 'C:\Program Files\Oracle\VirtualBox\VBoxManage.exe'
function Log($m) { Write-Host ("  {0}  {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }

function VmState { (& $vbm showvminfo $VmName --machinereadable 2>$null |
                    Select-String '^VMState=' | ForEach-Object { $_ -replace '.*="|"$' }) }

if (-not (Test-Path $vbm)) { Log 'VBoxManage not found'; exit 1 }

# --------------------------------------------------------------- quiesce
if ((VmState) -eq 'running') {
    if (-not $StopVm) {
        Log "$VmName is running. Exporting it would capture a filesystem mid-write."
        Log 'Re-run with -StopVm, or shut it down yourself first.'
        exit 1
    }
    Log 'asking the guest to shut down'
    & $vbm controlvm $VmName acpipowerbutton | Out-Null
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 5
        if ((VmState) -ne 'running') { break }
        if ($i % 6 -eq 5) { Log "  still shutting down ($(($i+1)*5)s)" }
    }
    if ((VmState) -eq 'running') {
        Log 'the guest did not shut down in 5 minutes -- refusing to export a running VM'
        exit 1
    }
    Log "shut down cleanly (state: $(VmState))"
}

# ---------------------------------------------------------------- export
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$file = Join-Path $Dest ("lantern-{0}.ova" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

Log "exporting to $file -- this takes a while"
& $vbm export $VmName -o $file --ovf20 `
    --vsys 0 --product 'LANtern' --vendor 'lantern' 2>&1 |
  Where-Object { $_ -match '\d+%' } | Select-Object -Last 1 | ForEach-Object { Log "    $_" }

if (-not (Test-Path $file)) { Log 'export produced no file'; exit 1 }
$gb = (Get-Item $file).Length / 1GB
if ($gb -lt 1) { Log ('export is only {0:N2} GB -- that is wrong, deleting it' -f $gb); Remove-Item $file; exit 1 }
Log ('wrote {0:N1} GB' -f $gb)

# ----------------------------------------------------------------- prune
$images = Get-ChildItem $Dest -Filter 'lantern-*.ova' | Sort-Object Name
if ($images.Count -gt $Keep) {
    $old = $images | Select-Object -First ($images.Count - $Keep)
    Log "pruning $($old.Count) older image(s)"
    $old | Remove-Item -Force
}

Write-Host ''
Log 'To restore:'
Write-Host "      VBoxManage import `"$file`""
Write-Host '      then re-point the router DHCP reservation at the imported VM''s new MAC,'
Write-Host '      or set the MAC to match the original with modifyvm --macaddress1.'
Write-Host ''
if ($StopVm) { Log "The VM is off. Start it with:  VBoxManage startvm $VmName --type headless" }
