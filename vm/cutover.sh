#!/usr/bin/env bash
# Hand 192.168.0.115 from Windows to the VM.
#
# LANtern answers on 192.168.0.115. APP_URL, the Pelican node's FQDN, every
# allocation alias, both control UIs, the docs and every friend's connect line
# name that address. Moving the stack to the VM without moving the address
# would mean editing all of it; moving the address instead means editing none.
#
# WHY THIS SCRIPT IS SHAPED THE WAY IT IS
#
# The obvious order -- release the address on Windows, then SSH to the VM and
# change its address -- does not work, and fails in a way that is easy to
# misread. This script runs in WSL, and WSL reaches the LAN through the Windows
# host. Taking .115 off Windows drops WSL's route to the LAN with it, so the
# very next SSH lands on "No route to host". The VM is fine and reachable from
# Windows the whole time; only the machine driving the cutover has gone blind.
# That happened, and it left the address held by nobody.
#
# So nothing is driven over the network after the release. Everything the VM
# needs is staged first, while the path still works, and the VM then completes
# the change on its own:
#
#   1. write the new netplan on the VM, and validate it
#   2. arm a job on the VM that waits for the address to go quiet, then applies
#   3. release the address on Windows
#   4. watch for the VM to appear, using Windows' own stack, not WSL's
#
# Step 2 waits for silence rather than trusting a timer. If the release fails,
# the VM never claims the address and there is no conflict -- two hosts on one
# address is far worse than a failed cutover, because both half-work and the
# symptoms look like an application fault.
#
# THE ROUTER RESERVATION
#
# The DHCP reservation for .115 must point at the VM's bridged NIC MAC, not the
# Windows NIC's. The VM holds the address statically so it does not need the
# lease, but the pool is 192.168.0.2-253 with no exclusions, so without the
# reservation the router can hand .115 to some other device. This script prints
# the MAC to use.
#
# Run this from WSL, elevated. --revert puts Windows back and returns the VM.

set -uo pipefail

OLD_IP="${OLD_IP:-192.168.0.116}"
NEW_IP="${NEW_IP:-192.168.0.115}"
GW="${GW:-192.168.0.1}"
VM_NAME="${VM_NAME:-lantern}"
VM_USER="${VM_USER:-iverson}"
KEY="${KEY:-$HOME/.ssh/lantern_vm}"

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
ps1() { powershell.exe -NoProfile -Command "$1" 2>/dev/null | tr -d '\r'; }

# Reachability must be judged with Windows' stack, not WSL's -- WSL's view of
# the LAN is exactly what this operation breaks.
winping() { [ "$(ps1 "(Test-Connection $1 -Count 2 -Quiet -ErrorAction SilentlyContinue)")" = 'True' ]; }

# --------------------------------------------------------------- preflight
step 'Preflight'

grep -qi microsoft /proc/version || die 'Run this from WSL.'
[ -f "$KEY" ] || die "SSH key not found: $KEY"

at "$FROM" true 2>/dev/null || die "cannot reach the VM at $FROM -- is it running?"
ok "VM answers at $FROM"

MAC=$(at "$FROM" "ip -br link show | awk '/^en/{print \$3; exit}'" 2>/dev/null)
note "VM bridged NIC MAC: $MAC"

ADMIN=$(ps1 '([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)')
[ "$ADMIN" = 'True' ] || die 'Windows changes need elevation. Re-run from an elevated shell.'
ok 'running elevated'

WIN_IF=''
if [ "$REVERT" = 0 ]; then
  WIN_IF=$(ps1 "(Get-NetIPAddress -IPAddress $NEW_IP -ErrorAction SilentlyContinue).InterfaceAlias" | head -1)
  if [ -n "$WIN_IF" ]; then
    ok "$NEW_IP is on Windows interface '$WIN_IF' -- as expected"
  else
    note "$NEW_IP is not on any Windows interface"
    winping "$NEW_IP" && die "$NEW_IP answers but Windows does not hold it. Something else has it -- stop."
    ok "$NEW_IP is free"
  fi
fi

# ------------------------------------------------------------ stage on vm
step "Staging the change on the VM (while the path still works)"

at "$FROM" "sudo tee /etc/netplan/50-cloud-init.yaml >/dev/null <<EOF
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
sudo chmod 600 /etc/netplan/50-cloud-init.yaml" || die 'could not write the new netplan'

# cloud-init regenerates netplan on every boot unless told not to, which would
# silently put the VM back on its old address after a restart.
at "$FROM" "sudo mkdir -p /etc/cloud/cloud.cfg.d && \
  echo 'network: {config: disabled}' | sudo tee /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg >/dev/null" \
  || die 'could not disable cloud-init network management'

at "$FROM" 'sudo netplan generate' || die 'netplan rejected the configuration -- nothing has changed yet'
ok "netplan written for $TO and validated"

at "$FROM" "sudo tee /usr/local/sbin/lantern-take-ip.sh >/dev/null <<'EOF'
#!/usr/bin/env bash
# Wait for the target address to go quiet, then take it. Never take an address
# something else still answers on.
set -uo pipefail
TARGET=\"\$1\"
exec >>/var/log/lantern-cutover.log 2>&1
echo \"=== take-ip \$TARGET at \$(date -u +%FT%TZ) ===\"
for i in \$(seq 1 60); do
  if ! ping -c1 -W1 \"\$TARGET\" >/dev/null 2>&1; then
    echo \"  \$TARGET is quiet after \${i} checks; applying\"
    netplan apply
    echo \"  applied\"
    exit 0
  fi
  sleep 2
done
echo \"  \$TARGET still answers after 120s -- NOT applying, the old holder never released\"
exit 1
EOF
sudo chmod 0755 /usr/local/sbin/lantern-take-ip.sh" || die 'could not stage the take-ip helper'

at "$FROM" "sudo systemctl reset-failed lantern-takeip 2>/dev/null; \
  sudo systemd-run --unit=lantern-takeip --quiet /usr/local/sbin/lantern-take-ip.sh $TO" \
  || die 'could not arm the address change'
ok "VM armed: it will take $TO as soon as that address goes quiet"

# ------------------------------------------------------- windows releases
step "Windows releases $NEW_IP"

if [ "$REVERT" = 0 ] && [ -n "$WIN_IF" ]; then
  ps1 "
    Remove-NetIPAddress -IPAddress $NEW_IP -Confirm:\$false -ErrorAction SilentlyContinue
    Remove-NetRoute -InterfaceAlias '$WIN_IF' -DestinationPrefix '0.0.0.0/0' -Confirm:\$false -ErrorAction SilentlyContinue
    Set-NetIPInterface -InterfaceAlias '$WIN_IF' -Dhcp Enabled -ErrorAction SilentlyContinue
    Set-DnsClientServerAddress -InterfaceAlias '$WIN_IF' -ResetServerAddresses -ErrorAction SilentlyContinue
    ipconfig /renew '$WIN_IF' | Out-Null
  " >/dev/null
  STILL=$(ps1 "(Get-NetIPAddress -IPAddress $NEW_IP -ErrorAction SilentlyContinue).InterfaceAlias" | head -1)
  [ -z "$STILL" ] && ok "Windows no longer holds $NEW_IP" \
                  || die "Windows still holds $NEW_IP on '$STILL'. The VM will not claim it -- safe, but unfinished."
  note "Windows is now on: $(ps1 "(Get-NetIPAddress -InterfaceAlias '$WIN_IF' -AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress" | head -1)"
else
  note 'revert: leaving Windows on DHCP'
fi

# ----------------------------------------------------------------- verify
step 'Verify'
note 'watching from Windows -- WSL cannot see the LAN during this'

UP=0
for _ in $(seq 1 30); do
  winping "$TO" || continue
  [ "$(ps1 "(& 'C:\\Windows\\System32\\OpenSSH\\ssh.exe' -i (wsl.exe wslpath -w '$KEY') -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -o LogLevel=ERROR $VM_USER@$TO hostname)")" = "$VM_NAME" ] || continue
  UP=1; break
done

if [ "$UP" != 1 ]; then
  bad "$TO never came up."
  bad 'Read the VM'"'"'s own log for why it did or did not take the address:'
  bad "  ssh $VM_USER@$FROM sudo cat /var/log/lantern-cutover.log"
  bad "  or the serial console at E:\\LANtern-VM\\serial.log"
  exit 1
fi
ok "$TO answers, and it is the VM"

if winping "$FROM"; then
  bad "$FROM STILL answers. Two addresses are live -- investigate before using this."
  exit 1
fi
ok "$FROM is released"

printf '\n'
printf '  Cutover done. LANtern is at %s.\n\n' "$TO"
printf '  Do these next:\n'
printf '    1. Point the router DHCP reservation for %s at %s\n' "$TO" "$MAC"
printf '       (the pool has no exclusions, so without it the router can lease %s away)\n' "$TO"
printf '    2. Start the stack:  ssh %s@%s\n' "$VM_USER" "$TO"
printf '                         cd /opt/lantern/stack && docker compose up -d\n'
printf '    3. Test from ANOTHER machine. This one is not a fair test.\n'
printf '    4. WSL lost its LAN route when Windows changed address. If you still\n'
printf '       use WSL for anything, run: wsl --shutdown\n\n'
printf '  Roll back with:  %s --revert\n\n' "$0"
