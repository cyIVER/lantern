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
  windows-setup.ps1 registers this as the daily "LANtern backup" task.
#>
[CmdletBinding()]
param(
    [string]$Dest = 'D:\LANtern-Backups\data',
    [string]$VmName = 'lantern',
    [string]$VmHost = '192.168.0.115',
    [string]$VmUser = 'iverson',
    [string]$Key = "$env:USERPROFILE\.ssh\lantern_vm",
    [int]$Keep = 14,
    [string]$SshExecutable = 'C:\Windows\System32\OpenSSH\ssh.exe',
    [string]$ScpExecutable = 'C:\Windows\System32\OpenSSH\scp.exe',
    [string]$VBoxManageExecutable = 'C:\Program Files\Oracle\VirtualBox\VBoxManage.exe'
)

$ErrorActionPreference = 'Stop'
$ssh = $SshExecutable
$scp = $ScpExecutable
$vbm = $VBoxManageExecutable
$o   = @('-i', $Key, '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=NUL',
         '-o', 'LogLevel=ERROR', '-o', 'ConnectTimeout=10')

function Log($m) { Write-Host ("  {0}  {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }

function Test-BackupStatus {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedBackupId
    )

    try {
        $raw = Get-Content -LiteralPath $Path -Raw
        # Windows PowerShell 5.1 unwraps a one-item top-level JSON array during
        # ConvertFrom-Json. Check the JSON shape before parsing so an array can
        # never masquerade as the one required status object.
        if ([string]::IsNullOrWhiteSpace($raw) -or -not $raw.TrimStart().StartsWith('{')) {
            return $false
        }
        $candidate = $raw | ConvertFrom-Json
    } catch {
        return $false
    }

    if ($candidate -isnot [System.Management.Automation.PSCustomObject]) {
        return $false
    }
    $schemaIsInteger = $candidate.schema -is [int] -or $candidate.schema -is [long]
    $failureCountIsInteger = (
        $candidate.failure_count -is [int] -or $candidate.failure_count -is [long]
    )
    $failureCodesAreEmptyArray = (
        $candidate.failure_codes -is [System.Array] -and $candidate.failure_codes.Count -eq 0
    )
    $componentsAreObject = (
        $candidate.components -is [System.Management.Automation.PSCustomObject]
    )
    $validMinecraftStates = @('offline_consistent', 'quiesced_consistent')
    $minecraftStateIsValid = $componentsAreObject `
        -and $candidate.components.minecraft_world -is [string] `
        -and $validMinecraftStates -ccontains $candidate.components.minecraft_world

    return $schemaIsInteger `
        -and $candidate.schema -eq 1 `
        -and $candidate.event -is [string] `
        -and $candidate.event -ceq 'backup.completed' `
        -and $candidate.backup_id -is [string] `
        -and $candidate.backup_id -ceq $ExpectedBackupId `
        -and $candidate.status -is [string] `
        -and $candidate.status -ceq 'complete' `
        -and $failureCountIsInteger `
        -and $candidate.failure_count -eq 0 `
        -and $failureCodesAreEmptyArray `
        -and $minecraftStateIsValid
}

function Test-BackupTransferIntegrity {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    $manifestPath = Join-Path $Path 'SHA256SUMS'
    try {
        $root = Get-Item -LiteralPath $Path -Force
        if (-not $root.PSIsContainer `
            -or ($root.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            return $false
        }
        if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
            return $false
        }
        $manifest = Get-Item -LiteralPath $manifestPath -Force
        if ($manifest.Name -cne 'SHA256SUMS' `
            -or ($manifest.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            return $false
        }
        $lines = @(Get-Content -LiteralPath $manifestPath)
    } catch {
        return $false
    }

    if ($lines.Count -eq 0 -or $lines.Count -gt 4096) {
        return $false
    }
    $expectedHashes = [Collections.Generic.Dictionary[string, string]]::new(
        [StringComparer]::Ordinal
    )
    $seenNames = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    foreach ($line in $lines) {
        if ($line.Length -gt 400 -or $line -cnotmatch '^(?<hash>[0-9a-f]{64})  (?<name>.+)$') {
            return $false
        }
        $hash = $Matches.hash
        $name = $Matches.name
        # Backup sets are intentionally flat. A conservative filename alphabet
        # also prevents rooted, traversal, separator, stream, and control syntax.
        if ($name.Length -gt 255 `
            -or $name -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$' `
            -or $name.EndsWith('.') `
            -or $name -ceq 'SHA256SUMS') {
            return $false
        }
        $baseName = $name.Split('.')[0]
        if ($baseName -imatch '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$') {
            return $false
        }
        if (-not $seenNames.Add($name)) {
            return $false
        }
        $expectedHashes.Add($name, $hash.ToUpperInvariant())
    }
    if (-not $expectedHashes.ContainsKey('MANIFEST.txt') `
        -or -not $expectedHashes.ContainsKey('BACKUP_STATUS.json')) {
        return $false
    }

    try {
        $children = @(Get-ChildItem -LiteralPath $Path -Force)
        if (@($children | Where-Object { $_.PSIsContainer }).Count -ne 0) {
            return $false
        }
        $localFiles = @(
            $children | Where-Object { -not $_.PSIsContainer -and $_.Name -cne 'SHA256SUMS' }
        )
        if ($localFiles.Count -ne $expectedHashes.Count) {
            return $false
        }
        foreach ($file in $localFiles) {
            if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                return $false
            }
            if (-not $expectedHashes.ContainsKey($file.Name)) {
                return $false
            }
            $stream = $null
            $sha256 = $null
            try {
                $stream = [IO.File]::OpenRead($file.FullName)
                $sha256 = [Security.Cryptography.SHA256]::Create()
                $actualHash = [BitConverter]::ToString($sha256.ComputeHash($stream)).Replace('-', '')
            } finally {
                if ($null -ne $stream) { $stream.Dispose() }
                if ($null -ne $sha256) { $sha256.Dispose() }
            }
            if ($actualHash -cne $expectedHashes[$file.Name]) {
                return $false
            }
        }
    } catch {
        return $false
    }

    return $true
}

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
$out = @(& $ssh @o "$VmUser@$VmHost" 'set -o pipefail; bash /opt/lantern/vm/backup-all.sh 2>&1 | tail -4')
$backupExitCode = $LASTEXITCODE
$resultMarkerPrefix = 'LANTERN_BACKUP_RESULT_V1:'
$resultMarkers = @($out | Where-Object { $_.StartsWith($resultMarkerPrefix) })
$out | Where-Object { -not $_.StartsWith($resultMarkerPrefix) } |
    ForEach-Object { Log "    $($_ -replace '\x1b\[[0-9;]*m','')" }
$backupFailed = $backupExitCode -ne 0
if ($backupFailed) {
    Log 'backup-all.sh reported failures -- pulling anyway so you have what it did get'
}

if ($resultMarkers.Count -ne 1 `
    -or $out.Count -eq 0 `
    -or $out[-1] -cne $resultMarkers[0]) {
    Log 'backup result path marker is missing or ambiguous'
    exit 1
}
$remoteResultPath = $resultMarkers[0].Substring($resultMarkerPrefix.Length)
if ($remoteResultPath -cmatch '^/var/backups/lantern/(?<stamp>[0-9]{8}-[0-9]{6})/$') {
    $stamp = $Matches.stamp
} elseif ($backupFailed `
    -and $remoteResultPath -cmatch '^/var/backups/lantern/\.(?<stamp>[0-9]{8}-[0-9]{6})\.staging\.[A-Za-z0-9]{6}/$') {
    $stamp = $Matches.stamp
} else {
    Log 'invalid backup result path marker'
    exit 1
}

# -------------------------------------------------------------------- pull
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$destPath = [IO.Path]::GetFullPath($Dest)
$target = [IO.Path]::GetFullPath((Join-Path $destPath $stamp))
$destComparison = $destPath.TrimEnd('\', '/')
$targetParent = [IO.Directory]::GetParent($target).FullName.TrimEnd('\', '/')
if ($targetParent -ine $destComparison) {
    Log 'refusing backup target outside the destination root'
    exit 1
}
$staging = Join-Path $destPath (".$stamp.transfer-{0}" -f [Guid]::NewGuid().ToString('N'))

Log "pulling $stamp to $Dest"
& $scp @o -r "${VmUser}@${VmHost}:$remoteResultPath" $staging 2>$null
if ($LASTEXITCODE -ne 0) { Log 'scp failed'; exit 1 }

# Verify against the VM's own manifest rather than trusting scp's exit code.
$remoteCount = [int](& $ssh @o "$VmUser@$VmHost" "ls -1 $remoteResultPath | wc -l").Trim()
$localCount  = (Get-ChildItem $staging -File).Count
if ($localCount -lt $remoteCount) {
    $backupFailed = $true
    Log "INCOMPLETE: $localCount of $remoteCount files arrived"
}
$size = '{0:N1} MB' -f ((Get-ChildItem $staging -File | Measure-Object Length -Sum).Sum / 1MB)
Log "$localCount files, $size"

if (-not (Test-BackupTransferIntegrity -Path $staging)) {
    $backupFailed = $true
    Log 'INCOMPLETE: SHA256SUMS transfer verification failed'
}

$statusPath = Join-Path $staging 'BACKUP_STATUS.json'
if (-not (Test-Path -LiteralPath $statusPath)) {
    $backupFailed = $true
    Log 'INCOMPLETE: BACKUP_STATUS.json is missing'
} else {
    if (-not (Test-BackupStatus -Path $statusPath -ExpectedBackupId $stamp)) {
        $backupFailed = $true
        Log 'INCOMPLETE: backup status contract is not restore-eligible'
    }
}

# A set is published under its stable timestamp only after the remote command,
# transfer inventory, and restore-eligibility status all agree. Failed evidence
# stays diagnostic and is excluded from retention eligibility.
if (-not $backupFailed) {
    $targetExistedBeforePublish = Test-Path -LiteralPath $target
    try {
        # Directory.Move is an atomic, no-replace rename on the same volume.
        # Unlike Move-Item, it cannot silently nest staging inside a target
        # created after the advisory existence check above.
        [IO.Directory]::Move($staging, $target)
    } catch {
        $backupFailed = $true
        if ($targetExistedBeforePublish) {
            Log "INCOMPLETE: backup set $stamp already exists -- refusing to overwrite it"
        } else {
            Log 'INCOMPLETE: verified backup set could not be published atomically'
        }
    }
}

if (-not $backupFailed) {
    # Revalidate the stable path after the atomic rename so a published set is
    # never trusted solely on checks performed under its staging name.
    $publishedIntegrityValid = Test-BackupTransferIntegrity -Path $target
    $publishedStatusValid = Test-BackupStatus `
        -Path (Join-Path $target 'BACKUP_STATUS.json') `
        -ExpectedBackupId $stamp
    if (-not $publishedIntegrityValid -or -not $publishedStatusValid) {
        $backupFailed = $true
        Log 'INCOMPLETE: backup set failed validation after publication'
        try {
            [IO.Directory]::Move($target, $staging)
            Log 'invalid published set returned to diagnostic staging'
        } catch {
            Log 'INCOMPLETE: invalid published set could not be returned to diagnostic staging'
        }
    }
}

# ------------------------------------------------------------------- prune
$sets = @(Get-ChildItem $Dest -Directory | Sort-Object Name)
if (-not $backupFailed) {
    $completeSets = @($sets | Where-Object {
        $candidateStatus = Join-Path $_.FullName 'BACKUP_STATUS.json'
        (Test-BackupTransferIntegrity -Path $_.FullName) `
            -and (Test-BackupStatus -Path $candidateStatus -ExpectedBackupId $_.Name)
    })
    if ($completeSets.Count -gt $Keep) {
        $old = $completeSets | Select-Object -First ($completeSets.Count - $Keep)
        Log "pruning $($old.Count) complete set(s) beyond the last $Keep"
        $old | Remove-Item -Recurse -Force
    }
} else {
    Log 'skipping retention pruning because this backup is incomplete'
}

Log "done. $($sets.Count) set(s) in $Dest"
if ($backupFailed) {
    Log 'backup set retained for diagnosis but is not eligible for restore'
    exit 1
}
