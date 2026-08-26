<#
.SYNOPSIS
  Forward LANtern's ports from Windows to the WSL VM so the LAN can reach them.

.DESCRIPTION
  With WSL on default NAT networking, containers listen inside the VM on its
  private address (172.x). Docker Desktop is supposed to republish those onto
  Windows, but on this machine it refuses to: it keeps its own helper service
  stopped and resets it to demand-start within a minute of being enabled, and
  leaves port forwarding to WSL's relay -- which binds IPv6 loopback only. The
  result is a stack that works perfectly from inside WSL and is invisible
  everywhere else.

      TCP [::1]:80  LISTENING  wslrelay.exe      <- all you get
      127.0.0.1:80  BLOCKED
      192.168.0.115:80  BLOCKED

  Rather than fight Docker Desktop's service management, this forwards the
  ports itself with netsh portproxy, straight from 0.0.0.0 on Windows to the
  WSL VM's address, and opens the matching Windows firewall rules.

  A LIMITATION WORTH KNOWING BEFORE YOU RELY ON IT

  netsh portproxy is TCP only. There is no UDP equivalent in Windows. So this
  covers the panel, both control UIs, Wings and Minecraft -- but NOT CS2, whose
  gameplay is UDP 27015. Stardew is unaffected because it joins over Steam's
  relay with an invite code rather than a listening port.

  WSL's NAT address changes on most restarts, so re-run this after any
  `wsl --shutdown` or reboot. It is idempotent: stale rules are replaced.

.PARAMETER Remove
  Tear down the port forwards and firewall rules.

.PARAMETER Distro
  WSL distribution to target. Default Ubuntu-26.04.

.NOTES
  Requires an elevated PowerShell.
#>
[CmdletBinding()]
param(
    [string]$Distro = 'Ubuntu-26.04',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

# TCP only -- see the note above about UDP.
$ports = @(
    @{ Port = 80;    What = 'Pelican panel' }
    @{ Port = 8080;  What = 'Wings API (panel -> daemon)' }
    @{ Port = 8090;  What = 'CS2 control UI' }
    @{ Port = 8092;  What = 'Stardew control UI' }
    @{ Port = 25565; What = 'Minecraft' }
    @{ Port = 5800;  What = 'Stardew VNC console' }
    @{ Port = 2022;  What = 'Wings SFTP' }
)

$RuleName = 'LANtern forwarded ports'

$admin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Write-Host 'This needs an elevated PowerShell.' -ForegroundColor Red; exit 1 }

# ------------------------------------------------------------------- remove
if ($Remove) {
    Write-Host ''
    foreach ($p in $ports) {
        & netsh interface portproxy delete v4tov4 listenport=$($p.Port) listenaddress=0.0.0.0 2>&1 | Out-Null
        Write-Host ("  removed forward for {0}" -f $p.Port)
    }
    Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    Write-Host '  removed firewall rule'
    Write-Host ''
    exit 0
}

# --------------------------------------------------------- find the WSL VM
Write-Host ''
Write-Host 'Preflight' -ForegroundColor Cyan

$wslIp = (& wsl.exe -d $Distro -e sh -c "ip -4 addr show eth0 | grep -oP 'inet \K[0-9.]+'" 2>$null |
          Select-Object -First 1).ToString().Trim()

if (-not ($wslIp -match '^\d+\.\d+\.\d+\.\d+$')) {
    Write-Host ("  could not read an IPv4 address from {0}. Is it running?" -f $Distro) -ForegroundColor Red
    exit 1
}
Write-Host ("  {0} is at {1}" -f $Distro, $wslIp)

# Prove the stack answers inside the VM before forwarding to it. Forwarding to
# a dead target produces a connection that opens and then hangs, which is far
# harder to diagnose than a refusal.
$probe = (& wsl.exe -d $Distro -e sh -c "curl -s -o /dev/null -m 8 -w '%{http_code}' http://127.0.0.1" 2>$null | Out-String).Trim()
if ($probe -eq '200' -or $probe -eq '302') {
    Write-Host ("  panel answers inside WSL: HTTP {0}" -f $probe) -ForegroundColor Green
} else {
    Write-Host ("  panel did not answer inside WSL (got '{0}')." -f $probe) -ForegroundColor Yellow
    Write-Host '  Forwarding anyway, but start the stack before testing from the LAN.' -ForegroundColor Yellow
}

# ------------------------------------------------------------------ forward
Write-Host ''
Write-Host 'Forwarding' -ForegroundColor Cyan

foreach ($p in $ports) {
    # Delete first: a stale entry pointing at a previous WSL address silently
    # swallows connections rather than erroring.
    & netsh interface portproxy delete v4tov4 listenport=$($p.Port) listenaddress=0.0.0.0 2>&1 | Out-Null
    $out = & netsh interface portproxy add v4tov4 `
        listenport=$($p.Port) listenaddress=0.0.0.0 `
        connectport=$($p.Port) connectaddress=$wslIp 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host ("  {0,-6} -> {1}:{0,-6} {2}" -f $p.Port, $wslIp, $p.What) -ForegroundColor Green
    } else {
        Write-Host ("  {0,-6} FAILED: {1}" -f $p.Port, ($out -join ' ')) -ForegroundColor Red
    }
}

# ----------------------------------------------------------------- firewall
Write-Host ''
Write-Host 'Firewall' -ForegroundColor Cyan
Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort ($ports.Port) -Profile Any | Out-Null
Write-Host ("  inbound allow for {0}" -f (($ports.Port) -join ', ')) -ForegroundColor Green

# ------------------------------------------------------------------- verify
Write-Host ''
Write-Host 'Verifying' -ForegroundColor Cyan
Start-Sleep -Seconds 3

$hostIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
           Where-Object { $_.IPAddress -like '192.168.0.*' } | Select-Object -First 1).IPAddress
$fail = 0
foreach ($t in @('127.0.0.1', $hostIp)) {
    if (-not $t) { continue }
    $ok = Test-NetConnection $t -Port 80 -WarningAction SilentlyContinue -InformationLevel Quiet
    Write-Host ("  {0,-16}:80  {1}" -f $t, $(if ($ok) { 'OPEN' } else { 'BLOCKED' })) `
        -ForegroundColor $(if ($ok) { 'Green' } else { 'Red' })
    if (-not $ok) { $fail++ }
}

Write-Host ''
if ($fail) {
    Write-Host '  Ports are forwarded but not answering. Check the stack is up:' -ForegroundColor Red
    Write-Host '    wsl -d Ubuntu-26.04 -e docker ps'
    exit 1
}

Write-Host ("  Panel:       http://{0}" -f $hostIp)
Write-Host ("  CS2 UI:      http://{0}:8090" -f $hostIp)
Write-Host ("  Stardew UI:  http://{0}:8092" -f $hostIp)
Write-Host ("  Minecraft:   {0}:25565" -f $hostIp)
Write-Host ''
Write-Host '  CS2 is NOT covered -- its gameplay is UDP and portproxy is TCP only.' -ForegroundColor Yellow
Write-Host ''
Write-Host '  Re-run this after any `wsl --shutdown` or reboot; the WSL address moves.'
Write-Host '  Test from ANOTHER machine -- the host is not a fair test.' -ForegroundColor Yellow
Write-Host ''
