<#
    Register lantern-startup.ps1 to run at logon.

    Deliberately a Scheduled Task rather than Docker Desktop's own "start on
    login": Docker has no way to express "wait for a WSL distro first", and
    starting containers before Ubuntu is up is what killed Wings on the first
    reboot. One task owns the whole ordering instead.

    Run this once. Re-running replaces the existing task.
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'LANtern startup',
    [string]$Script   = (Join-Path $PSScriptRoot 'lantern-startup.ps1'),
    [int]$DelaySeconds = 45
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $Script)) { throw "not found: $Script" }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Script`""

# A short delay lets networking and the user session settle before we start
# pulling up WSL and the Docker engine.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT${DelaySeconds}S"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description 'Starts Ubuntu WSL, Docker Desktop, and the Pelican panel stack in order.' `
    -Force | Out-Null

$t = Get-ScheduledTask -TaskName $TaskName
Write-Output "registered : $($t.TaskName)"
Write-Output "state      : $($t.State)"
Write-Output "trigger    : at logon +${DelaySeconds}s"
Write-Output ""
Write-Output "Test without rebooting:  Start-ScheduledTask -TaskName '$TaskName'"
