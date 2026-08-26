<#
.SYNOPSIS
  Recover the Intel I226-V NIC and stop Windows falling back to Wi-Fi.

.DESCRIPTION
  Symptom: the link reports Up at 2.5 Gbps, full duplex, zero errors, and
  passes no traffic at all. Windows notices, marks the profile NoTraffic, and
  routes everything over Wi-Fi instead. Because that Wi-Fi lands on the SAME
  192.168.0.0/24 subnet, the host then has two interfaces on one network with
  equal route metrics, replies leave by the wrong interface, and every LANtern
  service becomes intermittently unreachable from the LAN.

  So this looks like a server problem and is really a NIC problem.

  Confirm which interface is dead before believing anything else:

      ping -S 192.168.0.115 -n 3 192.168.0.1     # Ethernet
      ping -S 192.168.0.222 -n 3 192.168.0.1     # Wi-Fi

  The I226-V is well known for wedging specifically on 2.5 Gbps links. The
  durable fix is to pin it to 1.0 Gbps: the LAN is a gigabit router anyway, so
  nothing is lost, and the link stops dropping. A restart alone usually brings
  traffic back, but it comes back to the same 2.5 Gbps negotiation that broke.

.PARAMETER Speed
  Link mode to pin. Default '1.0 Gbps Full Duplex'.

.PARAMETER AutoNegotiate
  Restore 'Auto Negotiation' instead of pinning. Use to test whether a driver
  update has fixed the underlying problem.

.PARAMETER DisableWifi
  Turn off the Wi-Fi adapter once Ethernet is verified working. Deliberately
  NOT the default and never done before verification -- if Ethernet is dead,
  Wi-Fi is the only way this machine is online at all.

.NOTES
  Requires an elevated PowerShell. The adapter restart drops connections for a
  few seconds; do not run it over a remote session on this NIC.
#>
[CmdletBinding()]
param(
    [string]$Adapter       = 'Ethernet 5',
    [string]$WifiAdapter   = 'Wi-Fi 2',
    [string]$Gateway       = '192.168.0.1',
    [string]$Speed         = '1.0 Gbps Full Duplex',
    [switch]$AutoNegotiate,
    [switch]$DisableWifi
)

$ErrorActionPreference = 'Stop'

function Test-Path-Out([string]$src, [string]$dst) {
    # Source-bound ping. Without -S the packet can leave by the other
    # interface and cheerfully "prove" a dead NIC is fine.
    $out = & ping -S $src -n 3 -w 1500 $dst 2>&1 | Out-String
    return ($out -match 'Reply from')
}

$admin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host 'This needs an elevated PowerShell.' -ForegroundColor Red
    exit 1
}

$nic = Get-NetAdapter -Name $Adapter -ErrorAction SilentlyContinue
if (-not $nic) { Write-Host ("No adapter named '$Adapter'.") -ForegroundColor Red; exit 1 }

$ip = (Get-NetIPAddress -InterfaceAlias $Adapter -AddressFamily IPv4 -ErrorAction SilentlyContinue |
       Select-Object -First 1).IPAddress

Write-Host ''
Write-Host 'Before' -ForegroundColor Cyan
Write-Host ("  {0}  {1}  {2}" -f $nic.Name, $nic.Status, $nic.LinkSpeed)
Write-Host ("  driver {0} ({1})" -f $nic.DriverVersion, $nic.DriverDate)
Write-Host ("  ip     {0}" -f $ip)
$before = if ($ip) { Test-Path-Out $ip $Gateway } else { $false }
Write-Host ("  gateway reachable via this NIC: {0}" -f $(if ($before) { 'YES' } else { 'NO' })) `
    -ForegroundColor $(if ($before) { 'Green' } else { 'Red' })

# --- link mode -------------------------------------------------------------
$target = if ($AutoNegotiate) { 'Auto Negotiation' } else { $Speed }
$prop = Get-NetAdapterAdvancedProperty -Name $Adapter -DisplayName 'Speed & Duplex' -ErrorAction SilentlyContinue
if ($prop) {
    if ($prop.DisplayValue -eq $target) {
        Write-Host ''
        Write-Host ("Link mode already '{0}'." -f $target)
    } else {
        if ($prop.ValidDisplayValues -notcontains $target) {
            Write-Host ''
            Write-Host ("Driver will not accept '{0}'. Valid values:" -f $target) -ForegroundColor Red
            $prop.ValidDisplayValues | ForEach-Object { Write-Host ('  - ' + $_) }
            exit 1
        }
        Write-Host ''
        Write-Host ("Setting link mode: '{0}' -> '{1}'" -f $prop.DisplayValue, $target) -ForegroundColor Cyan
        Set-NetAdapterAdvancedProperty -Name $Adapter -DisplayName 'Speed & Duplex' -DisplayValue $target
    }
}

# --- restart ---------------------------------------------------------------
Write-Host ''
Write-Host 'Restarting the adapter (a few seconds of no link)...' -ForegroundColor Cyan
Restart-NetAdapter -Name $Adapter

for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    $nic = Get-NetAdapter -Name $Adapter
    if ($nic.Status -eq 'Up') { break }
}
Write-Host ("  link: {0}  {1}" -f $nic.Status, $nic.LinkSpeed)

# Give DHCP/ARP a moment to settle before judging it.
Start-Sleep -Seconds 5

# --- verify ----------------------------------------------------------------
$ip = (Get-NetIPAddress -InterfaceAlias $Adapter -AddressFamily IPv4 -ErrorAction SilentlyContinue |
       Select-Object -First 1).IPAddress
Write-Host ''
Write-Host 'After' -ForegroundColor Cyan
Write-Host ("  ip     {0}" -f $ip)

$ok = $false
for ($i = 1; $i -le 6; $i++) {
    if ($ip -and (Test-Path-Out $ip $Gateway)) { $ok = $true; break }
    Write-Host ("  gateway not answering yet (attempt {0}/6)" -f $i) -ForegroundColor DarkGray
    Start-Sleep -Seconds 5
}

if (-not $ok) {
    Write-Host ''
    Write-Host '  STILL DEAD. The NIC is up but passing no traffic.' -ForegroundColor Red
    Write-Host '  Leave Wi-Fi enabled -- it is how this machine is online.' -ForegroundColor Red
    Write-Host ''
    Write-Host '  Next things to try, in order:'
    Write-Host '    1. A different cable, and a different port on the router.'
    Write-Host '    2. Pin even lower:  .\fix-ethernet.ps1 -Speed "100 Mbps Full Duplex"'
    Write-Host '    3. Update the I226-V driver from Intel, then re-run with -AutoNegotiate.'
    Write-Host '    4. Reboot. This NIC has wedged before and come back after one.'
    exit 1
}

Write-Host '  gateway reachable via this NIC: YES' -ForegroundColor Green

$net = Get-NetConnectionProfile -InterfaceAlias $Adapter -ErrorAction SilentlyContinue
if ($net) { Write-Host ("  connectivity: {0}" -f $net.IPv4Connectivity) }

# --- wifi ------------------------------------------------------------------
Write-Host ''
if ($DisableWifi) {
    $w = Get-NetAdapter -Name $WifiAdapter -ErrorAction SilentlyContinue
    if ($w -and $w.Status -eq 'Up') {
        Write-Host ("Disabling {0} so it stops sharing the subnet." -f $WifiAdapter) -ForegroundColor Cyan
        Disable-NetAdapter -Name $WifiAdapter -Confirm:$false
        Write-Host '  done.'
    } else {
        Write-Host ("{0} is not up; nothing to disable." -f $WifiAdapter)
    }
} else {
    $w = Get-NetAdapter -Name $WifiAdapter -ErrorAction SilentlyContinue
    if ($w -and $w.Status -eq 'Up') {
        Write-Host 'Ethernet works again, but Wi-Fi is still on the same subnet.' -ForegroundColor Yellow
        Write-Host 'Two interfaces on one network is what made LANtern unreachable.' -ForegroundColor Yellow
        Write-Host 'Re-run with -DisableWifi, or turn Wi-Fi off from the taskbar.'
    }
}

Write-Host ''
Write-Host 'Then confirm the panel from another machine:  http://192.168.0.115'
Write-Host ''
