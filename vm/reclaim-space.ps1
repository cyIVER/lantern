<#
.SYNOPSIS
  Remove the WSL and Docker Desktop virtual disks after the move to the VM.

.DESCRIPTION
  WSL and Docker Desktop keep their filesystems in .vhdx files that grow and
  never shrink. After the migration they hold:

      E:\DockerData    ~365 GB   Docker Desktop's ext4.vhdx
      E:\WSL            ~84 GB   the Ubuntu distro

  Both sit on the Samsung SSD -- the fastest disk in the machine, and the one
  the VM runs from.

  TWO WAYS TO DO THIS, AND THE GOOD ONE DOES NOT NEED A REBOOT

  If WSL still works, `wsl --unregister` is the right tool: it deregisters the
  distro and deletes its disk, releasing the space cleanly and immediately.
  That works with the hypervisor still enabled, so reclaiming the space is not
  coupled to the reboot at all.

  Only when WSL is already gone -- the hypervisor disabled, the feature
  removed -- does this fall back to deleting the directories outright, which
  needs those files to be unlocked and therefore needs the hypervisor off.

  THIS IS THE POINT OF NO RETURN. Everything before it was reversible. So it
  refuses to run unless it can see the backups first -- not as ceremony: the
  two Docker volumes and the WSL home directory only exist in those archives
  now.

.PARAMETER WhatIf
  Report what would be removed and how much it would free, then stop.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string[]]$Paths = @('E:\DockerData', 'E:\WSL'),
    [string]$BackupRoot = 'D:\LANtern-Backups',
    [switch]$KeepDockerDesktop
)

$ErrorActionPreference = 'Continue'

function ok($m)   { Write-Host "  $m" -ForegroundColor Green }
function bad($m)  { Write-Host "  $m" -ForegroundColor Red }
function note($m) { Write-Host "  $m" }
function step($m) { Write-Host ''; Write-Host $m -ForegroundColor Cyan }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { bad 'This needs an elevated PowerShell.'; exit 1 }

# ------------------------------------------------------------- backups first
step 'Preflight'

$required = @(
    @{ Path = "$BackupRoot\docker-volumes\dco450-course_postgres_data.tgz"; MinMB = 1 }
    @{ Path = "$BackupRoot\docker-volumes\memory-system_memory-db.tgz";     MinMB = 0.1 }
    @{ Path = "$BackupRoot\wsl-home\iiverson-home.tgz";                     MinMB = 500 }
)
$missing = 0
foreach ($r in $required) {
    if (-not (Test-Path $r.Path)) { bad "MISSING  $($r.Path)"; $missing++; continue }
    $mb = (Get-Item $r.Path).Length / 1MB
    if ($mb -lt $r.MinMB) { bad ('TOO SMALL {0} ({1:N1} MB)' -f $r.Path, $mb); $missing++ }
    else { ok ('{0}  ({1:N0} MB)' -f (Split-Path $r.Path -Leaf), $mb) }
}
if ($missing) {
    bad ''
    bad "$missing backup(s) missing or suspect. Refusing to remove anything."
    exit 1
}

$before = (Get-PSDrive E).Free / 1GB
note ('E: free before: {0:N1} GB' -f $before)

# ------------------------------------------------------------ unregister wsl
step 'Removing WSL distributions'

$wslWorks = $false
$distros = @()
try {
    # --list --quiet emits UTF-16, which arrives here full of NULs. Strip them
    # or every name compares as a mismatch and nothing is ever found.
    $raw = (& wsl.exe --list --quiet 2>&1 | Out-String) -replace "`0", ''
    if ($LASTEXITCODE -eq 0) {
        $wslWorks = $true
        $distros = $raw -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    }
} catch { }

if ($wslWorks) {
    note ('WSL is available; distros: ' + (($distros -join ', ')))
    foreach ($d in $distros) {
        if ($KeepDockerDesktop -and $d -eq 'docker-desktop') {
            note "keeping $d as asked"
            continue
        }
        if ($PSCmdlet.ShouldProcess($d, 'wsl --unregister')) {
            & wsl.exe --unregister $d 2>&1 | ForEach-Object { note "  $_" }
            if ($LASTEXITCODE -eq 0) { ok "unregistered $d" } else { bad "could not unregister $d" }
        }
    }
} else {
    note 'WSL is not available, so the disks have to be deleted directly.'
    $hv = (& bcdedit /enum '{current}' | Select-String 'hypervisorlaunchtype')
    if ($hv -notmatch 'off') {
        bad 'and the hypervisor is still enabled, so those files may be locked.'
        bad 'Either re-enable WSL, or disable the hypervisor and reboot first.'
        exit 1
    }
    ok 'hypervisor is off, so nothing should be holding them'
}

# --------------------------------------------------------- sweep the residue
step 'Removing what is left'

foreach ($p in $Paths) {
    if (-not (Test-Path $p)) { ok "$p is gone"; continue }
    $b = (Get-ChildItem $p -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    if (-not $b) {
        if ($PSCmdlet.ShouldProcess($p, 'Remove empty directory')) {
            Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue
            ok "removed empty $p"
        }
        continue
    }
    note ('{0,-18} {1,8:N1} GB still present' -f $p, ($b / 1GB))
    if ($PSCmdlet.ShouldProcess($p, 'Remove permanently')) {
        try {
            Remove-Item $p -Recurse -Force -ErrorAction Stop
            ok "removed $p"
        } catch {
            bad "could not remove ${p}: $($_.Exception.Message)"
            bad '  something still has it open. If Docker Desktop is running, quit it.'
        }
    }
}

if ($WhatIfPreference) { Write-Host ''; note 'WhatIf: nothing was removed.'; exit 0 }

# ---------------------------------------------------------------- verify
step 'After'
$after = (Get-PSDrive E).Free / 1GB
ok ('E: free: {0:N1} GB -> {1:N1} GB  (+{2:N1} GB)' -f $before, $after, ($after - $before))

Write-Host ''
note 'Docker Desktop is still installed but has no data; uninstall it if you want.'
note 'The WSL feature is still enabled and can host a new distro if you ever need one.'
Write-Host ''
