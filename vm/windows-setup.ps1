<#
.SYNOPSIS
  Point the Windows host at the VM and stop it starting the old WSL stack.

.DESCRIPTION
  After the migration the Windows side is still configured for the machine it
  used to be. Three things are actively wrong:

    1. A "LANtern startup" scheduled task that, at every logon, starts the WSL
       distro, launches Docker Desktop and runs `docker compose up -d` on the
       old stack. That is now a second, competing copy of Pelican and Wings.
    2. Docker Desktop in the Run key, which exists only to serve that stack.
    3. Nothing that starts the VM, which is the machine that actually runs
       LANtern now.

  This fixes all three, and registers the nightly backup pull.

  -DisableHypervisor additionally turns the Windows hypervisor off. WSL2 keeps
  it running, and while it runs VirtualBox is stuck on the Windows Hypervisor
  Platform: roughly half the single-threaded CPU, and no AVX2 in the guest.
  Turning it off gives VirtualBox native VT-x. It also stops WSL2 and Docker
  Desktop working entirely, and needs a reboot. Reversible:

      bcdedit /set hypervisorlaunchtype auto     (then reboot)

.NOTES
  Requires an elevated PowerShell.
#>
[CmdletBinding()]
param(
    [string]$VmName = 'lantern',
    [string]$RepoDir = (Split-Path -Parent $PSScriptRoot),
    [switch]$DisableHypervisor,
    [switch]$SkipBackupTask
)

$ErrorActionPreference = 'Continue'
$vbm = 'C:\Program Files\Oracle\VirtualBox\VBoxManage.exe'

function ok($m)   { Write-Host "  $m" -ForegroundColor Green }
function bad($m)  { Write-Host "  $m" -ForegroundColor Red }
function note($m) { Write-Host "  $m" }
function step($m) { Write-Host ''; Write-Host $m -ForegroundColor Cyan }

function VmStateSafe($name) {
    if (-not (Test-Path $vbm)) { return 'absent' }
    $s = & $vbm showvminfo $name --machinereadable 2>$null |
          Select-String '^VMState=' | ForEach-Object { $_ -replace '.*="|"$' }
    if ($s) { $s } else { 'absent' }
}

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { bad 'This needs an elevated PowerShell.'; exit 1 }

# ------------------------------------------------------- stop the old stack
step 'Retiring the old WSL startup'

$task = Get-ScheduledTask -TaskName 'LANtern startup' -ErrorAction SilentlyContinue
if ($task) {
    Unregister-ScheduledTask -TaskName 'LANtern startup' -Confirm:$false
    ok 'unregistered the "LANtern startup" scheduled task'
} else {
    note 'no "LANtern startup" task (already gone)'
}

$run = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
if ((Get-ItemProperty $run -ErrorAction SilentlyContinue).PSObject.Properties.Name -contains 'Docker Desktop') {
    Remove-ItemProperty -Path $run -Name 'Docker Desktop' -ErrorAction SilentlyContinue
    ok 'removed Docker Desktop from autostart (it is still installed)'
} else {
    note 'Docker Desktop is not in the Run key'
}

# ------------------------------------------------------------- vm shortcut
step 'Making the VM easy to start'

# Headless via VBoxManage rather than the GUI: the VM has no display worth
# looking at, and a stray window is a stray window to close by accident.
$startCmd = Join-Path $RepoDir 'vm\Start-LANtern.cmd'
@"
@echo off
rem Start the LANtern VM headless. Closing this window does not stop the VM.
"$vbm" startvm $VmName --type headless
if errorlevel 1 (
  echo.
  echo Could not start $VmName. Is it already running?
  pause
) else (
  echo.
  echo LANtern is starting. Give it about 40 seconds, then open:
  echo     http://192.168.0.115:8090
  timeout /t 6 >nul
)
"@ | Set-Content -Path $startCmd -Encoding ASCII
ok "wrote $startCmd"

$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop 'Start LANtern.lnk'
$sh = New-Object -ComObject WScript.Shell
$s = $sh.CreateShortcut($lnk)
$s.TargetPath = $startCmd
$s.WorkingDirectory = Split-Path $startCmd
$s.Description = 'Start the LANtern game server VM (headless)'
$s.Save()
ok "shortcut on your Desktop: 'Start LANtern'"

# --------------------------------------------------------------- backups
if (-not $SkipBackupTask) {
    step 'Nightly backup to D:'
    $pull = Join-Path $RepoDir 'vm\backup-pull.ps1'
    if (Test-Path $pull) {
        Unregister-ScheduledTask -TaskName 'LANtern backup' -Confirm:$false -ErrorAction SilentlyContinue
        $action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
                     -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$pull`""
        # 03:00 daily. It exits quietly when the VM is off, which is the normal
        # case now that the VM is started by hand.
        $trigger = New-ScheduledTaskTrigger -Daily -At 3am
        $set     = New-ScheduledTaskSettingsSet -StartWhenAvailable `
                     -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
                     -ExecutionTimeLimit (New-TimeSpan -Hours 2)
        Register-ScheduledTask -TaskName 'LANtern backup' -Action $action `
            -Trigger $trigger -Settings $set -RunLevel Highest -Force | Out-Null
        ok 'registered "LANtern backup" daily at 03:00 -> D:\LANtern-Backups\data'
    } else {
        bad "backup-pull.ps1 not found at $pull"
    }
}

# ------------------------------------------------------------ hypervisor
if ($DisableHypervisor) {
    step 'Disabling the Windows hypervisor'
    note 'This stops WSL2 and Docker Desktop, and gives VirtualBox native VT-x.'

    $ci = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity' -ErrorAction SilentlyContinue
    if ($ci -and $ci.Enabled -eq 1) {
        bad 'Memory Integrity (Core Isolation) is ON and will hold VT-x regardless.'
        bad 'Turn it off in Windows Security > Device security > Core isolation, then re-run.'
        exit 1
    }
    note 'Memory Integrity is off, so nothing else is holding VT-x'

    if ((VmStateSafe $VmName) -eq 'running') {
        note 'shutting the VM down cleanly first'
        & $vbm controlvm $VmName acpipowerbutton | Out-Null
        for ($i = 0; $i -lt 48; $i++) {
            Start-Sleep -Seconds 5
            if ((VmStateSafe $VmName) -ne 'running') { break }
        }
        ok "VM state: $(VmStateSafe $VmName)"
    }

    & bcdedit /set hypervisorlaunchtype off | Out-Null
    $now = (& bcdedit /enum '{current}' | Select-String 'hypervisorlaunchtype')
    if ($now -match 'off') {
        ok 'hypervisorlaunchtype = off'
    } else {
        bad 'bcdedit did not take -- check the output of: bcdedit /enum {current}'
        exit 1
    }
}

# ---------------------------------------------------------------- summary
Write-Host ''
Write-Host '  Done.' -ForegroundColor Green
Write-Host ''
if ($DisableHypervisor) {
    Write-Host '  REBOOT NOW for the hypervisor change to take effect.'
    Write-Host ''
    Write-Host '  After the reboot:'
    Write-Host '    - WSL2 and Docker Desktop will no longer start. That is expected.'
    Write-Host '    - Start the VM from the Desktop shortcut.'
    Write-Host "    - Reclaim 449 GB with:  vm\reclaim-space.ps1"
    Write-Host ''
    Write-Host '  To undo:  bcdedit /set hypervisorlaunchtype auto   (then reboot)'
} else {
    Write-Host '  Start the VM from the Desktop shortcut, then open http://192.168.0.115:8090'
}
Write-Host ''
