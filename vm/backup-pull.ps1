<#
.SYNOPSIS
  Take a fresh LANtern backup on the VM and copy it to D:.

.DESCRIPTION
  The backup is produced ON the VM, by vm/backup-all.sh, and then pulled here
  as plain files. Both halves of that matter.

  Produced on the VM, because only the VM can quiesce Minecraft through RCON
  and dump MariaDB consistently. Pulled to D: as plain files -- not left in the
  VM, and not written into a second virtual disk -- because the failure this is
  really insuring against is losing the VM itself. A backup you can only read
  by booting the thing that died is not a backup.

  D: is the Toshiba HDD; the VM lives on the Samsung SSD. Different physical
  disk, which is the point.

  If the VM is not running this exits quietly and successfully. It is meant to
  run on a schedule, and the VM is started by hand, so "not running" is the
  normal case rather than an error worth alerting about.

.PARAMETER Keep
  How many dated sets to keep on D:. Default 14.

.PARAMETER Dest
  Where to put them. Default D:\LANtern-Backups\data.

.NOTES
  Register it to run daily with vm\register-backup-task.ps1.
#>
[CmdletBinding()]
param(
    [string]$Dest = 'D:\LANtern-Backups\data',
    [string]$VmName = 'lantern',
    [string]$VmHost = '192.168.0.115',
    [string]$VmUser = 'iverson',
    [string]$Key = "$env:USERPROFILE\.ssh\lantern_vm",
    [int]$Keep = 14
)

$ErrorActionPreference = 'Stop'
$ssh = 'C:\Windows\System32\OpenSSH\ssh.exe'
$scp = 'C:\Windows\System32\OpenSSH\scp.exe'
$vbm = 'C:\Program Files\Oracle\VirtualBox\VBoxManage.exe'
$o   = @('-i', $Key, '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=NUL',
         '-o', 'LogLevel=ERROR', '-o', 'ConnectTimeout=10')

function Log($m) { Write-Host ("  {0}  {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }

# ------------------------------------------------------------------ preflight
if (-not (Test-Path $Key)) { Log "no SSH key at $Key"; exit 1 }

$state = (& $vbm showvminfo $VmName --machinereadable 2>$null |
          Select-String '^VMState=' | ForEach-Object { $_ -replace '.*="|"$' })
if ($state -ne 'running') {
    Log "$VmName is '$state' -- nothing to back up right now"
    exit 0
}

& $ssh @o "$VmUser@$VmHost" 'true' 2>$null
if ($LASTEXITCODE -ne 0) { Log 'VM is running but SSH did not answer'; exit 1 }

# ----------------------------------------------------------------- back up
Log 'running backup-all.sh on the VM'
$out = & $ssh @o "$VmUser@$VmHost" 'bash /opt/lantern/vm/backup-all.sh 2>&1 | tail -4'
$out | ForEach-Object { Log "    $($_ -replace '\x1b\[[0-9;]*m','')" }
if ($LASTEXITCODE -ne 0) { Log 'backup-all.sh reported failures -- pulling anyway so you have what it did get' }

$latest = (& $ssh @o "$VmUser@$VmHost" 'ls -1d /var/backups/lantern/*/ | tail -1').Trim()
if (-not $latest) { Log 'no backup directory produced'; exit 1 }
$stamp = (Split-Path $latest.TrimEnd('/') -Leaf)

# -------------------------------------------------------------------- pull
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$target = Join-Path $Dest $stamp
if (Test-Path $target) { Remove-Item $target -Recurse -Force }

Log "pulling $stamp to $Dest"
& $scp @o -r "${VmUser}@${VmHost}:$latest" $target 2>$null
if ($LASTEXITCODE -ne 0) { Log 'scp failed'; exit 1 }

# Verify against the VM's own manifest rather than trusting scp's exit code.
$remoteCount = [int](& $ssh @o "$VmUser@$VmHost" "ls -1 $latest | wc -l").Trim()
$localCount  = (Get-ChildItem $target -File).Count
if ($localCount -lt $remoteCount) {
    Log "INCOMPLETE: $localCount of $remoteCount files arrived"
    exit 1
}
$size = '{0:N1} MB' -f ((Get-ChildItem $target -File | Measure-Object Length -Sum).Sum / 1MB)
Log "$localCount files, $size"

# ------------------------------------------------------------------- prune
$sets = Get-ChildItem $Dest -Directory | Sort-Object Name
if ($sets.Count -gt $Keep) {
    $old = $sets | Select-Object -First ($sets.Count - $Keep)
    Log "pruning $($old.Count) set(s) beyond the last $Keep"
    $old | Remove-Item -Recurse -Force
}

Log "done. $($sets.Count) set(s) in $Dest"
