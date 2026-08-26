#!/usr/bin/env bash
# Hand 192.168.0.115 from Windows to the VM.
#
# LANtern answers on 192.168.0.115. APP_URL, the Pelican node's FQDN, every
# allocation alias, both control UIs, the docs and every friend's connect line
# name that address. Moving the stack to the VM without moving the address
# would mean editing all of it; moving the address instead means editing none.
#
# ORDER MATTERS AND IS NOT REVERSIBLE MID-FLIGHT
#
# Two machines must not hold .115 at once, so Windows releases it before the VM
# claims it. Between those two steps nothing answers on .115 -- that gap is
# expected and lasts a few seconds.
#
# The VM's address change is applied through systemd-run rather than directly,
# because `netplan apply` tears down the interface this SSH session is riding
# on. Run it in the foreground and the session dies mid-reconfigure, sometimes
# before the new address is written.
#
# THE ROUTER RESERVATION IS NOT HANDLED HERE
#
# The DHCP reservation for .115 is bound to the Windows NIC's MAC
# (A0-36-BC-BA-5A-C3). The VM's bridged adapter has a different one. The VM
# holds .115 statically so it does not need the lease, but the router's pool is
# 192.168.0.2-253 with no exclusions, so it can still hand .115 to some other
# device and cause an address conflict. Re-point the reservation to the VM's
# MAC -- this script prints it -- before trusting the setup overnight.
#
# Run this from WSL. --revert puts Windows back and returns the VM to .116.

set -uo pipefail

OLD_IP="${OLD_IP:-192.168.0.116}"
NEW_IP="${NEW_IP:-192.168.0.115}"
GW="${GW:-192.168.0.1}"
VM_NAME="${VM_NAME:-lantern}"
VM_USER="${VM_USER:-iverson}"
KEY="${KEY:-$HOME/.ssh/lantern_vm}"
WIN_IF="${WIN_IF:-Ethernet 4}"

REVERT=0
[ "${1:-}" = '--revert' ] && REVERT=1
if [ "$REVERT" = 1 ]; then FROM="$NEW_IP"; TO="$OLD_IP"; else FROM="$OLD_IP"; TO="$NEW_IP"; fi

ok()   { printf '  \033[32m%s\033[0m\n' "$*"; }
bad()  { printf '  \033[31m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
step() { printf '\n\033[36m%s\033[0m\n' "$*"; }
die()  { bad "$*"; exit 1; }

SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
          -o LogLevel=ERROR -o ConnectTimeout=8)
at()  { ssh -i "$KEY" "${SSH_OPTS[@]}" "$VM_USER@$1" "${@:2}"; }
ps1() { powershell.exe -NoProfile -Command "$1" 2>&1 | tr -d '\r'; }

# --------------------------------------------------------------- preflight
step 'Preflight'

grep -qi microsoft /proc/version || die 'Run this from WSL.'
[ -f "$KEY" ] || die "SSH key not found: $KEY"

at "$FROM" true 2>/dev/null || die "cannot reach the VM at $FROM -- is it running?"
ok "VM answers at $FROM"

MAC=$(at "$FROM" "ip -br link show | awk '/^en/{print \$3; exit}'" 2>/dev/null)
note "VM bridged NIC MAC: $MAC"

# An address conflict is far worse than a failed cutover: both hosts half-work
# and the symptoms look like an application fault.
if [ "$REVERT" = 0 ]; then
  HOLDER=$(ps1 "(Get-NetIPAddress -IPAddress $NEW_IP -ErrorAction SilentlyContinue).InterfaceAlias" | head -1)
  if [ -n "$HOLDER" ]; then
    ok "$NEW_IP is currently on Windows interface '$HOLDER' -- as expected"
    WIN_IF="$HOLDER"
  else
    note "$NEW_IP is not on any Windows interface"
    if ping -c2 -W2 "$NEW_IP" >/dev/null 2>&1; then
      die "$NEW_IP answers but Windows does not hold it. Something else has it -- stop."
    fi
    ok "$NEW_IP is free"
  fi
fi

ADMIN=$(ps1 '([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)' | head -1)
[ "$ADMIN" = 'True' ] || die 'Windows changes need elevation. Re-run from an elevated shell.'
ok 'running elevated'

# ------------------------------------------------------- windows releases
step "Windows releases $NEW_IP"

if [ "$REVERT" = 0 ]; then
  ps1 "
    Remove-NetIPAddress -IPAddress $NEW_IP -Confirm:\$false -ErrorAction SilentlyContinue
    Remove-NetRoute -InterfaceAlias '$WIN_IF' -DestinationPrefix '0.0.0.0/0' -Confirm:\$false -ErrorAction SilentlyContinue
    Set-NetIPInterface -InterfaceAlias '$WIN_IF' -Dhcp Enabled -ErrorAction SilentlyContinue
    Set-DnsClientServerAddress -InterfaceAlias '$WIN_IF' -ResetServerAddresses -ErrorAction SilentlyContinue
    ipconfig /renew '$WIN_IF' | Out-Null
  " >/dev/null
  sleep 6
  STILL=$(ps1 "(Get-NetIPAddress -IPAddress $NEW_IP -ErrorAction SilentlyContinue).InterfaceAlias" | head -1)
  [ -z "$STILL" ] && ok "Windows no longer holds $NEW_IP" \
                  || die "Windows still holds $NEW_IP on '$STILL' -- aborting before the VM claims it"
  note "Windows is now on: $(ps1 "(Get-NetIPAddress -InterfaceAlias '$WIN_IF' -AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress" | head -1)"
else
  note 'revert: leaving Windows on DHCP'
fi

# ------------------------------------------------------------- vm claims
step "VM takes $TO"

at "$FROM" "sudo tee /etc/netplan/50-cloud-init.yaml >/dev/null <<'EOF'
network:
  version: 2
  ethernets:
    lan:
      match:
        name: \"en*\"
      dhcp4: false
      dhcp6: false
      addresses: [$TO/24]
      routes:
        - to: default
          via: $GW
      nameservers:
        addresses: [$GW, 1.1.1.1]
EOF
sudo chmod 600 /etc/netplan/50-cloud-init.yaml
# cloud-init would rewrite this on the next boot; tell it not to manage the network.
sudo mkdir -p /etc/cloud/cloud.cfg.d
echo 'network: {config: disabled}' | sudo tee /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg >/dev/null
sudo netplan generate" 2>/dev/null || die 'could not write the new netplan'
ok "netplan written for $TO, cloud-init network management disabled"

# Detached: netplan apply drops the interface this session rides on.
at "$FROM" "sudo systemd-run --unit=lantern-cutover --quiet /usr/sbin/netplan apply" 2>/dev/null
ok 'apply dispatched (this SSH session is expected to die now)'

# ----------------------------------------------------------------- verify
step 'Verify'

UP=0
for i in $(seq 1 24); do
  sleep 5
  ping -c1 -W2 "$TO" >/dev/null 2>&1 || continue
  at "$TO" true 2>/dev/null || continue
  UP=1; break
done

if [ "$UP" != 1 ]; then
  bad "$TO never came up."
  bad 'The VM still has a console. Recover with:'
  bad "  VBoxManage controlvm $VM_NAME poweroff && VBoxManage startvm $VM_NAME --type headless"
  bad "  then read E:\\LANtern-VM\\serial.log"
  exit 1
fi
ok "$TO answers, and it is the VM"

HOST=$(at "$TO" hostname 2>/dev/null)
ADDR=$(at "$TO" "ip -4 -br addr show scope global | awk '{print \$3}'" 2>/dev/null | tr '\n' ' ')
note "hostname: $HOST"
note "addresses: $ADDR"

# Prove the old address is gone. Two live addresses is the split-brain that
# made this stack unreachable for days the last time.
if ping -c2 -W2 "$FROM" >/dev/null 2>&1; then
  bad "$FROM STILL answers. Two addresses are live -- investigate before using this."
  exit 1
fi
ok "$FROM is released"

printf '\n'
printf '  Cutover done. LANtern is at %s.\n\n' "$TO"
printf '  Do these next:\n'
printf '    1. Re-point the router DHCP reservation for %s to %s\n' "$TO" "$MAC"
printf '       (it is still bound to the Windows NIC, and the pool has no exclusions)\n'
printf '    2. Start the stack:  ssh -i %s %s@%s\n' "$KEY" "$VM_USER" "$TO"
printf '                         cd /opt/lantern/stack && docker compose up -d\n'
printf '    3. Test from ANOTHER machine. This one is not a fair test.\n\n'
printf '  Roll back with:  %s --revert\n\n' "$0"
