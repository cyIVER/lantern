<#
.SYNOPSIS
  Delete the WSL and Docker Desktop virtual disks after the move to the VM.

.DESCRIPTION
  WSL and Docker Desktop keep their filesystems in .vhdx files that grow and
  never shrink. After the migration they hold:

      E:\DockerData    ~365 GB   Docker Desktop's ext4.vhdx
      E:\WSL            ~84 GB   the Ubuntu distro

  Both are inert once the hypervisor is off, and both sit on the Samsung SSD --
  the fastest disk in the machine, and the one the VM runs from.

  THIS IS THE POINT OF NO RETURN. Everything before it was reversible: the
  hypervisor can be switched back on, and the old stack would still be there.
  After this, going back means rebuilding WSL from scratch.

  So it refuses to run unless it can see the backups first. Not as ceremony --
  the two Docker volumes and the WSL home directory only exist in those
  archives now.

.PARAMETER WhatIf
  Report what would be deleted and how much it would free, and stop.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string[]]$Paths = @('E:\DockerData', 'E:\WSL'),
    [string]$BackupRoot = 'D:\LANtern-Backups'
)

$ErrorActionPreference = 'Continue'

function ok($m)   { Write-Host "  $m" -ForegroundColor Green }
function bad($m)  { Write-Host "  $m" -ForegroundColor Red }
function note($m) { Write-Host "  $m" }
function step($m) { Write-Host ''; Write-Host $m -ForegroundColor Cyan }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { bad 'This needs an elevated PowerShell.'; exit 1 }

# ------------------------------------------------------------- preflight
step 'Preflight'

$hv = (& bcdedit /enum '{current}' | Select-String 'hypervisorlaunchtype')
if ($hv -notmatch 'off') {
    bad 'The hypervisor is still enabled, so WSL and Docker Desktop can still hold'
    bad 'these files open. Run windows-setup.ps1 -DisableHypervisor and reboot first.'
    exit 1
}
ok 'hypervisor is off'

# The backups are the only remaining copy. Check they are real, not just present.
$required = @(
    @{ Path = "$BackupRoot\docker-volumes\dco450-course_postgres_data.tgz"; MinMB = 1 }
    @{ Path = "$BackupRoot\docker-volumes\memory-system_memory-db.tgz";     MinMB = 0.1 }
    @{ Path = "$BackupRoot\wsl-home\iiverson-home.tgz";                     MinMB = 500 }
)
$missing = 0
foreach ($r in $required) {
    if (-not (Test-Path $r.Path)) { bad "MISSING  $($r.Path)"; $missing++; continue }
    $mb = (Get-Item $r.Path).Length / 1MB
    if ($mb -lt $r.MinMB) {
        bad ('TOO SMALL {0} ({1:N1} MB)' -f $r.Path, $mb); $missing++
    } else {
        ok ('{0}  ({1:N0} MB)' -f (Split-Path $r.Path -Leaf), $mb)
    }
}
if ($missing) {
    bad ''
    bad "$missing backup(s) missing or suspect. Refusing to delete anything."
    exit 1
}

# ------------------------------------------------------------------ size
step 'What this frees'

$total = 0
$found = @()
foreach ($p in $Paths) {
    if (-not (Test-Path $p)) { note "$p does not exist -- already gone"; continue }
    $bytes = (Get-ChildItem $p -Recurse -File -ErrorAction SilentlyContinue |
              Measure-Object Length -Sum).Sum
    $total += $bytes
    $found += $p
    note ('{0,-18} {1,8:N1} GB' -f $p, ($bytes / 1GB))
}
if (-not $found) { ok 'nothing left to reclaim'; exit 0 }
note ('{0,-18} {1,8:N1} GB total' -f '', ($total / 1GB))

$before = (Get-PSDrive E).Free / 1GB

# ---------------------------------------------------------------- delete
step 'Deleting'
foreach ($p in $found) {
    if ($PSCmdlet.ShouldProcess($p, 'Remove permanently')) {
        try {
            Remove-Item $p -Recurse -Force -ErrorAction Stop
            ok "removed $p"
        } catch {
            bad "could not remove ${p}: $($_.Exception.Message)"
            bad '  something still has it open -- did you reboot after disabling the hypervisor?'
        }
    }
}

if ($WhatIfPreference) { Write-Host ''; note 'WhatIf: nothing was deleted.'; exit 0 }

# ---------------------------------------------------------------- verify
step 'After'
$after = (Get-PSDrive E).Free / 1GB
ok ('E: free: {0:N1} GB -> {1:N1} GB  (+{2:N1} GB)' -f $before, $after, ($after - $before))

Write-Host ''
note 'Docker Desktop and WSL are still installed but have no data. If you want'
note 'them fully gone: uninstall Docker Desktop, and'
note '  dism /online /disable-feature /featurename:Microsoft-Windows-Subsystem-Linux'
Write-Host ''
