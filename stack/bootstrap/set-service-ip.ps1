<#
.SYNOPSIS
  Put the LANtern service address on one healthy interface, and only one.

.DESCRIPTION
  LANtern answers on 192.168.0.115. APP_URL, both control UIs, the Hyper-V
  firewall rules, every doc and every friend's connect line name that address,
  so it should never move -- what moves is which NIC holds it.

  This script does two things:

    1. Puts 192.168.0.115 on the interface you name.
    2. Neutralises every OTHER interface on 192.168.0.0/24.

  The second is the part that matters. Two interfaces on one subnet at similar
  route metrics means replies can leave by the interface the request did not
  arrive on, and clients drop them. The result is a server that answers
  sometimes: a panel that loads once and then does not, a login POST that never
  reaches the application at all, loopback behaving strangely inside WSL. It
  looks exactly like an application bug and is not one.

  Neutralising is done purely at the IP layer -- strip addresses, disable DHCP,
  push the metric to 9999 -- and never by disabling the adapter. On this
  machine the onboard Intel I226-V wedges in a way that stops it answering
  management calls at all: Get-NetAdapter blocked for five minutes,
  Disable-NetAdapter never returned, Device Manager spun for twenty, and
  Windows shutdown itself hung. IP-layer changes never enter the driver's halt
  path, so they work on a NIC that has otherwise stopped listening.

.PARAMETER Interface
  The adapter that should hold the service address, e.g. 'Ethernet 4'.

.PARAMETER Revert
  Hand the named interface back to DHCP and restore other interfaces' metrics.

.EXAMPLE
  .\set-service-ip.ps1 -Interface 'Ethernet 4'

.EXAMPLE
  .\set-service-ip.ps1 -Interface 'Ethernet 4' -Revert

.NOTES
  Requires an elevated PowerShell.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Interface,
    [string]$Address    = '192.168.0.115',
    [int]   $Prefix     = 24,
    [string]$Gateway    = '192.168.0.1',
    [string]$Subnet     = '192.168.0.',
    [string[]]$Dns      = @('192.168.0.1', '1.1.1.1'),
    [int]   $DeadMetric = 9999,
    [switch]$Revert
)

$ErrorActionPreference = 'Stop'

function Reaches([string]$src, [string]$dst) {
    ((& ping -S $src -n 3 -w 1500 $dst 2>&1) -join "`n") -match 'Reply from'
}

function RoutableIp([string]$alias) {
    (Get-NetIPAddress -InterfaceAlias $alias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '169.254.*' } | Select-Object -First 1).IPAddress
}

function LanRoutes { @(Get-NetRoute -DestinationPrefix "$($Subnet)0/24" -ErrorAction SilentlyContinue) }

# Interfaces sharing the subnet, excluding the one we want to keep. Virtual
# adapters on their own subnets are irrelevant and left alone.
function Rivals {
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -like "$Subnet*" -and $_.InterfaceAlias -ne $Interface } |
        Select-Object -ExpandProperty InterfaceAlias -Unique
}

function Neutralize([string]$alias) {
    Get-NetIPAddress -InterfaceAlias $alias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
    Remove-NetRoute -InterfaceAlias $alias -DestinationPrefix '0.0.0.0/0' -Confirm:$false -ErrorAction SilentlyContinue
    Set-NetIPInterface -InterfaceAlias $alias -Dhcp Disabled -ErrorAction SilentlyContinue
    Set-NetIPInterface -InterfaceAlias $alias -InterfaceMetric $DeadMetric -ErrorAction SilentlyContinue
}

function ToDhcp([string]$alias) {
    Get-NetIPAddress -InterfaceAlias $alias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
    Remove-NetRoute -InterfaceAlias $alias -DestinationPrefix '0.0.0.0/0' -Confirm:$false -ErrorAction SilentlyContinue
    Set-NetIPInterface -InterfaceAlias $alias -Dhcp Enabled -ErrorAction SilentlyContinue
    Set-DnsClientServerAddress -InterfaceAlias $alias -ResetServerAddresses -ErrorAction SilentlyContinue
    ipconfig /renew "$alias" | Out-Null
}

$admin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Write-Host 'This needs an elevated PowerShell.' -ForegroundColor Red; exit 1 }

$nic = Get-NetAdapter -Name $Interface -ErrorAction SilentlyContinue
if (-not $nic) { Write-Host ("No adapter named '$Interface'.") -ForegroundColor Red; exit 1 }

# ------------------------------------------------------------------- revert
if ($Revert) {
    Write-Host ''
    Write-Host "Reverting $Interface to DHCP." -ForegroundColor Cyan
    ToDhcp $Interface
    Start-Sleep -Seconds 6
    Write-Host ('  now: ' + (RoutableIp $Interface))
    Write-Host '  Other interfaces keep metric 9999; raise them by hand if you want them back.'
    exit 0
}

# --------------------------------------------------------------- preflight
Write-Host ''
Write-Host 'Preflight' -ForegroundColor Cyan
Write-Host ("  {0}: {1}, {2}" -f $nic.Name, $nic.Status, $nic.LinkSpeed)

if ($nic.Status -ne 'Up') {
    Write-Host '  Interface is not up. Aborting.' -ForegroundColor Red
    exit 1
}

$current = RoutableIp $Interface
if (-not $current) {
    Write-Host '  No routable address yet. Aborting -- bring it up on DHCP first.' -ForegroundColor Red
    exit 1
}
Write-Host ("  current address: {0}" -f $current)

# Prove it carries traffic BEFORE anything is changed. A link that is "Up"
# means nothing; the I226 was Up and full duplex while dropping every packet.
if (-not (Reaches $current $Gateway)) {
    Write-Host '  This interface cannot reach the gateway. Aborting before any change.' -ForegroundColor Red
    exit 1
}
Write-Host '  reaches the gateway: YES' -ForegroundColor Green

$rivals = @(Rivals)
if ($rivals.Count) {
    Write-Host ("  also on this subnet: {0}" -f ($rivals -join ', ')) -ForegroundColor Yellow
} else {
    Write-Host '  no other interface on this subnet'
}

# ----------------------------------------------------------------- apply
Write-Host ''
Write-Host 'Applying' -ForegroundColor Cyan

foreach ($r in $rivals) {
    Write-Host ("  neutralising {0} (IP layer only, no adapter disable)" -f $r)
    Neutralize $r
}

Write-Host ("  setting {0} to static {1}/{2}" -f $Interface, $Address, $Prefix)
Get-NetIPAddress -InterfaceAlias $Interface -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
Remove-NetRoute -InterfaceAlias $Interface -DestinationPrefix '0.0.0.0/0' -Confirm:$false -ErrorAction SilentlyContinue
Set-NetIPInterface -InterfaceAlias $Interface -Dhcp Disabled
Set-NetIPInterface -InterfaceAlias $Interface -InterfaceMetric 25 -ErrorAction SilentlyContinue

New-NetIPAddress -InterfaceAlias $Interface -IPAddress $Address -PrefixLength $Prefix `
    -DefaultGateway $Gateway -ErrorAction Stop | Out-Null
Set-DnsClientServerAddress -InterfaceAlias $Interface -ServerAddresses $Dns

Start-Sleep -Seconds 6

# ---------------------------------------------------------------- verify
Write-Host ''
Write-Host 'Verifying' -ForegroundColor Cyan

$ok = $false
for ($i = 1; $i -le 6; $i++) {
    if (Reaches $Address $Gateway) { $ok = $true; break }
    Write-Host ("  gateway not answering yet ({0}/6)" -f $i) -ForegroundColor DarkGray
    Start-Sleep -Seconds 5
}

if (-not $ok) {
    Write-Host ''
    Write-Host '  FAILED. Handing the interface back to DHCP.' -ForegroundColor Red
    ToDhcp $Interface
    Write-Host '  Reverted. Other interfaces are still neutralised; re-run or fix by hand.' -ForegroundColor Red
    exit 1
}
Write-Host ("  {0} answers on {1}: YES" -f $Gateway, $Address) -ForegroundColor Green

$dnsOk = $false
try { Resolve-DnsName -Name 'github.com' -QuickTimeout -ErrorAction Stop | Out-Null; $dnsOk = $true } catch { }
Write-Host ("  DNS resolves: {0}" -f $(if ($dnsOk) { 'YES' } else { 'no -- check DNS servers' })) `
    -ForegroundColor $(if ($dnsOk) { 'Green' } else { 'Yellow' })

Write-Host ''
Write-Host 'Addresses on this subnet:' -ForegroundColor Cyan
Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -like "$Subnet*" } |
    ForEach-Object { '  {0,-16} {1}' -f $_.IPAddress, $_.InterfaceAlias }

# Route count is the real test. Two adapters is fine; two routes is the bug.
$routes = LanRoutes
Write-Host ''
if ($routes.Count -gt 1) {
    Write-Host ("  {0} routes to {1}0/24 -- STILL SPLIT." -f $routes.Count, $Subnet) -ForegroundColor Red
    Write-Host '  Something else is on the subnet. LANtern will be intermittently unreachable.' -ForegroundColor Red
    exit 1
}
Write-Host '  One route to the LAN. No split routing.' -ForegroundColor Green

Write-Host ''
Write-Host 'LANtern keeps its address, so nothing else changes.'
Write-Host 'Restart WSL so the containers pick up the interface:'
Write-Host '    wsl --shutdown'
Write-Host ''
Write-Host 'Then, from another machine:  http://192.168.0.115'
Write-Host ''
