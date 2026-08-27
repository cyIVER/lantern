<#
    LANtern startup sequencer.

    The Wings container bind-mounts /etc/pelican and /var/lib/pelican, which
    resolve into the Ubuntu WSL filesystem. If Docker Desktop starts its
    containers before that distro is running, Wings comes up against paths that
    are not there yet and dies -- which is what happened on the first reboot.

    Nothing in Docker expresses "wait for a WSL distro", so this script owns the
    ordering: bring up Ubuntu, wait for the engine, then start the panel stack.
    Game servers are deliberately NOT started; they stay on-demand from the UI.

    Register it to run at logon (elevated):
        bash/pwsh> see register-startup-task.ps1
#>
[CmdletBinding()]
param(
    [string]$Distro   = 'Ubuntu-26.04',
    [string]$StackDir = (Split-Path -Parent $PSScriptRoot),
    [int]$TimeoutSec  = 300
)

# Native tools here write progress to stderr (docker compose especially), which
# 'Stop' would treat as fatal. Failures are checked explicitly via $LASTEXITCODE.
$ErrorActionPreference = 'Continue'
$log = Join-Path $StackDir 'startup.log'
function Log($m) {
    $line = "{0}  {1}" -f (Get-Date -Format 's'), $m
    Write-Output $line
    try { Add-Content -Path $log -Value $line -EA SilentlyContinue } catch {}
}

Log "=== LANtern startup ==="

# 1. Ubuntu WSL must be running before any container that bind-mounts its paths.
Log "starting WSL distro $Distro"
& wsl.exe -d $Distro -e true 2>&1 | Out-Null
$state = (& wsl.exe -l -v 2>&1) -replace "`0", '' | Select-String $Distro
Log "distro state: $($state -replace '\s+', ' ')"

# 2. Docker Desktop.
if (-not (Get-Process 'Docker Desktop' -EA SilentlyContinue)) {
    $exe = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
    if (Test-Path $exe) { Log 'launching Docker Desktop'; Start-Process $exe }
    else { Log "WARNING: Docker Desktop not found at $exe" }
}

# 3. Wait for the engine to answer.
Log 'waiting for the Docker engine'
$deadline = (Get-Date).AddSeconds($TimeoutSec)
$ready = $false
while ((Get-Date) -lt $deadline) {
    & docker info --format '{{.ServerVersion}}' 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 5
}
if (-not $ready) { Log "ERROR: engine not ready after ${TimeoutSec}s"; exit 1 }
Log 'engine ready'

# 4. Panel stack only. Wings starts here; game servers stay off until asked for.
Log 'bringing up the panel stack'
$linuxDir = ($StackDir -replace '\\','/' -replace '^C:','/mnt/c')
& wsl.exe -d $Distro -e bash -lc "cd '$linuxDir' && docker compose up -d 2>&1" 2>&1 |
    ForEach-Object { Log "  $($_ -replace "`0",'')" }
if ($LASTEXITCODE -ne 0) { Log "ERROR: compose up exited $LASTEXITCODE"; exit 1 }

Start-Sleep -Seconds 5

# 5. Game servers run on Wings' own network (pelican_nw), which is separate from
# the compose network MariaDB sits on -- so plugins like WeaponPaints cannot
# resolve it. Wings creates that network at startup, so the attachment has to be
# re-made here each boot rather than declared in compose. Idempotent.
Log 'attaching MariaDB to the game network'
& docker network inspect pelican_nw 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    & docker network connect --alias database pelican_nw stack-database-1 2>&1 |
        ForEach-Object { Log "  $_" }
    if ($LASTEXITCODE -eq 0) { Log '  attached' } else { Log '  already attached' }
} else {
    Log '  pelican_nw does not exist yet (no server has started) -- skipping'
}

& docker ps --format '{{.Names}}\t{{.Status}}' 2>&1 | ForEach-Object { Log "  $_" }
Log '=== done ==='
