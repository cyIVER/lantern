<#
.SYNOPSIS
  Allow LAN traffic into the LANtern servers when WSL runs in mirrored mode.

.DESCRIPTION
  With networkingMode=mirrored, WSL shares the Windows network stack and
  inbound traffic to it is policed by the *Hyper-V* firewall, which is a
  different thing from the normal Windows firewall and defaults to:

      DefaultInboundAction : Block
      LoopbackEnabled      : True

  That combination is quietly misleading. Everything works from the host,
  because loopback is exempt, so 127.0.0.1:8090 and localhost:80 respond
  perfectly. Nothing works from any other machine, because inbound is blocked
  and there are no allow rules. You do not find out until a friend tries to
  connect.

  This adds one narrow allow rule per port LANtern actually needs, rather than
  flipping DefaultInboundAction to Allow, which would expose every listening
  socket in the WSL VM to the LAN.

  Deliberately NOT opened:
    25575  Minecraft RCON     - a control channel; only the host needs it
    8080   Wings API          - the panel talks to it locally
    8091   Stardew HTTP API   - the Stardew UI reaches it over the compose network
    3306   MariaDB            - never
    2022   Wings SFTP         - add it yourself if you want remote file access

.NOTES
  Requires an elevated PowerShell. Idempotent: re-running replaces the rules.
  Reverse it with  -Remove.

.EXAMPLE
  # In an Administrator PowerShell:
  .\open-lan-firewall.ps1

.EXAMPLE
  .\open-lan-firewall.ps1 -Remove
#>
[CmdletBinding()]
param(
    [switch]$Remove,
    # WSL's VM creator id. Stable across machines; it identifies the WSL
    # platform to the Hyper-V firewall.
    [string]$VMCreatorId = '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'
)

$ErrorActionPreference = 'Stop'

$rules = @(
    @{ Name = 'LANtern-Panel';      Proto = 'TCP'; Ports = '80';          What = 'Pelican panel' }
    @{ Name = 'LANtern-CS2-UI';     Proto = 'TCP'; Ports = '8090';        What = 'CS2 control UI' }
    @{ Name = 'LANtern-Stardew-UI'; Proto = 'TCP'; Ports = '8092';        What = 'Stardew control UI' }
    @{ Name = 'LANtern-Minecraft';  Proto = 'TCP'; Ports = '25565';       What = 'Minecraft' }
    @{ Name = 'LANtern-CS2-TCP';    Proto = 'TCP'; Ports = '27015';       What = 'CS2' }
    @{ Name = 'LANtern-CS2-UDP';    Proto = 'UDP'; Ports = '27015,27020'; What = 'CS2 + SourceTV' }
    @{ Name = 'LANtern-VNC';        Proto = 'TCP'; Ports = '5800';        What = 'Stardew VNC console' }
)

# --- elevation ------------------------------------------------------------
$admin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host 'This needs an elevated PowerShell.' -ForegroundColor Red
    Write-Host 'Right-click PowerShell -> Run as administrator, then run it again.'
    exit 1
}

if (-not (Get-Command New-NetFirewallHyperVRule -ErrorAction SilentlyContinue)) {
    Write-Host 'New-NetFirewallHyperVRule is unavailable.' -ForegroundColor Red
    Write-Host 'It needs Windows 11 22H2 or newer. Without it, the alternative is'
    Write-Host 'dropping networkingMode=mirrored from ~/.wslconfig and using NAT.'
    exit 1
}

# --- remove ---------------------------------------------------------------
if ($Remove) {
    foreach ($r in $rules) {
        try { Remove-NetFirewallHyperVRule -Name $r.Name -ErrorAction Stop; Write-Host ('  removed  ' + $r.Name) }
        catch { Write-Host ('  absent   ' + $r.Name) -ForegroundColor DarkGray }
    }
    Write-Host ''
    Write-Host 'LAN access is closed again. Only the host can reach the servers.'
    exit 0
}

# --- state before ---------------------------------------------------------
$vm = Get-NetFirewallHyperVVMSetting -PolicyStore ActiveStore |
      Where-Object { $_.VMCreatorId -eq $VMCreatorId } | Select-Object -First 1
if ($vm) {
    Write-Host ('Hyper-V firewall default inbound: ' + $vm.DefaultInboundAction)
    if ($vm.DefaultInboundAction -eq 'Allow') {
        Write-Host '  Already Allow -- every WSL socket is reachable from the LAN.' -ForegroundColor Yellow
        Write-Host '  These rules are harmless but redundant. Consider setting it back to Block.' -ForegroundColor Yellow
    }
}
Write-Host ''

# --- apply ----------------------------------------------------------------
foreach ($r in $rules) {
    # Replace rather than skip, so changing a port here actually takes effect.
    try { Remove-NetFirewallHyperVRule -Name $r.Name -ErrorAction Stop | Out-Null } catch { }

    New-NetFirewallHyperVRule -Name $r.Name -DisplayName $r.Name `
        -Direction Inbound -VMCreatorId $VMCreatorId `
        -Protocol $r.Proto -LocalPorts $r.Ports -Action Allow | Out-Null

    '  {0,-22} {1,-4} {2,-14} {3}' -f $r.Name, $r.Proto, $r.Ports, $r.What
}

Write-Host ''
Write-Host 'Done. Verify from ANOTHER machine on the LAN -- testing from this one'
Write-Host 'proves nothing, because loopback was never blocked:' -ForegroundColor Yellow
Write-Host ''
Write-Host '    http://192.168.0.115         Pelican panel'
Write-Host '    http://192.168.0.115:8090    CS2 control UI'
Write-Host '    http://192.168.0.115:8092    Stardew control UI'
Write-Host ''
