#!/usr/bin/env bash
# Build the LANtern host as a bridged VirtualBox VM.
#
# WHY A VM AT ALL
#
# LANtern ran on Docker inside WSL2 and was never reliably reachable from the
# LAN. WSL2's NAT publishes ports through a relay that binds IPv6 loopback
# only; mirrored mode swaps that for a Hyper-V firewall that defaults to Block;
# Docker Desktop's port-publishing service on this machine refuses to stay
# started. Each of those has a workaround, and none of them covers UDP -- so
# CS2, whose gameplay is UDP 27015, could not work at all.
#
# A bridged VM has none of that. From the router's point of view it is just
# another machine on the LAN: every port is open, TCP and UDP alike, and
# nothing on Windows has to forward anything.
#
# WHAT THIS DOES
#
# Boots Ubuntu's official cloud image -- already installed, no installer to
# script -- and hands it a cloud-init seed that sets the hostname, the user,
# the SSH key, a static address and Docker. Roughly five minutes end to end.
#
# Safe to re-run: -Recreate destroys and rebuilds the VM. The downloaded cloud
# image is kept and checksummed rather than fetched again.
#
# Run this from WSL, not Git Bash.

set -uo pipefail

VM_NAME="${VM_NAME:-lantern}"
VM_USER="${VM_USER:-iverson}"
VM_IP="${VM_IP:-192.168.0.116}"          # temporary; .115 is still on Windows
VM_GW="${VM_GW:-192.168.0.1}"
VM_RAM_MB="${VM_RAM_MB:-18432}"          # of 32 GB, leaving Windows 13
VM_CPUS="${VM_CPUS:-12}"                 # of 20 logical cores
VM_DISK_MB="${VM_DISK_MB:-262144}"       # 256 GB, dynamically allocated
BRIDGE="${BRIDGE:-Realtek Gaming USB 2.5GbE Family Controller}"

BUILD=/mnt/e/LANtern-VM
WBUILD='E:\LANtern-VM'
IMG=ubuntu-26.04-server-cloudimg-amd64.vmdk
MIRROR=https://cloud-images.ubuntu.com/releases/26.04/release
KEY="$HOME/.ssh/lantern_vm"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VBM="/mnt/c/Program Files/Oracle/VirtualBox/VBoxManage.exe"

RECREATE=0
[ "${1:-}" = "-Recreate" ] && RECREATE=1

ok()   { printf '  \033[32m%s\033[0m\n' "$*"; }
bad()  { printf '  \033[31m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
step() { printf '\n\033[36m%s\033[0m\n' "$*"; }
die()  { bad "$*"; exit 1; }
vbm()  { "$VBM" "$@"; }

# --------------------------------------------------------------- preflight
step 'Preflight'

[ -x "$VBM" ] || die "VBoxManage not found at $VBM"
ok "VirtualBox $(vbm --version 2>/dev/null | tr -d '\r')"

grep -qi microsoft /proc/version || die 'Run this from WSL.'

mkdir -p "$BUILD"
if [ ! -f "$BUILD/$IMG" ] || ! ( cd "$BUILD" && sha256sum -c vmdk.sha256 >/dev/null 2>&1 ); then
  note 'fetching the Ubuntu cloud image (~790 MB, resumable)'
  ( cd "$BUILD" \
    && curl -fsSL -o SHA256SUMS "$MIRROR/SHA256SUMS" \
    && curl -fL --retry 5 --retry-delay 3 -C - -o "$IMG" "$MIRROR/$IMG" --progress-bar \
    && grep "$IMG" SHA256SUMS | sed 's/\*//' > vmdk.sha256 ) || die 'download failed'
  ( cd "$BUILD" && sha256sum -c vmdk.sha256 >/dev/null 2>&1 ) \
    || die 'downloaded image does not match its published checksum'
fi
ok "cloud image verified ($(du -m "$BUILD/$IMG" | cut -f1) MB)"

python3 -c 'import pycdlib' 2>/dev/null \
  || die 'pycdlib missing: python3 -m pip install --user --break-system-packages pycdlib'
ok 'pycdlib present'

vbm list bridgedifs 2>/dev/null | tr -d '\r' | grep -qF "$BRIDGE" \
  || die "VirtualBox cannot see a bridge called '$BRIDGE' (VBoxManage list bridgedifs)"
ok "bridge: $BRIDGE"

# The address must be free. Building onto an address something else already
# answers on produces a VM that looks fine and is unreachable for reasons that
# have nothing to do with the VM.
if ping -c2 -W1 "$VM_IP" >/dev/null 2>&1; then
  die "$VM_IP already answers. Pick another with VM_IP=..., or free it."
fi
ok "$VM_IP is free"

if vbm list vms 2>/dev/null | tr -d '\r' | grep -q "\"$VM_NAME\""; then
  [ "$RECREATE" = 1 ] || die "VM '$VM_NAME' already exists. Re-run with -Recreate to replace it."
  note "removing existing VM '$VM_NAME'"
  vbm controlvm "$VM_NAME" poweroff >/dev/null 2>&1
  sleep 3
  vbm unregistervm "$VM_NAME" --delete >/dev/null 2>&1
  rm -f "$BUILD/$VM_NAME.vdi" "$BUILD/serial.log"
fi

# ------------------------------------------------------------------ secrets
step 'Credentials'

if [ ! -f "$KEY" ]; then
  ssh-keygen -t ed25519 -N '' -C 'lantern-vm' -f "$KEY" >/dev/null
  ok "generated $KEY"
else
  ok "using existing $KEY"
fi
PUBKEY="$(cat "$KEY.pub")"

# Alphanumeric: this may have to be typed by hand at a serial console, where
# punctuation and keyboard layout are a needless way to lock yourself out.
PW="$(openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c 20)"
PW_HASH="$(openssl passwd -6 "$PW")"

umask 077
{
  echo 'LANtern VM console login'
  echo '------------------------'
  echo "host      $VM_NAME  ($VM_IP, temporary -- becomes 192.168.0.115 at cutover)"
  echo "user      $VM_USER"
  echo "password  $PW"
  echo "ssh       ssh -i $KEY $VM_USER@$VM_IP"
  echo
  echo 'The password is only for the VirtualBox console. SSH uses the key.'
  echo 'Delete this file once the password is stored somewhere sensible.'
} > "$BUILD/.vm-login"
umask 022
ok 'password generated, written to E:\LANtern-VM\.vm-login (not printed)'

# --------------------------------------------------------------- seed image
step 'Cloud-init seed'

SEED="$(mktemp -d)"
trap 'rm -rf "$SEED"' EXIT
cp "$HERE/cloud-init/meta-data" "$SEED/meta-data"

sed -e "s|@SSH_KEY@|$PUBKEY|" -e "s|@PW_HASH@|$PW_HASH|" \
    "$HERE/cloud-init/user-data.tmpl" > "$SEED/user-data"
if grep -q '@SSH_KEY@\|@PW_HASH@' "$SEED/user-data"; then
  die 'template placeholders were not substituted'
fi

sed -e "s|192\.168\.0\.116/24|$VM_IP/24|" -e "s|via: 192\.168\.0\.1|via: $VM_GW|" \
    "$HERE/cloud-init/network-config" > "$SEED/network-config"
grep -q "$VM_IP/24" "$SEED/network-config" || die 'network-config did not take the address'

python3 "$HERE/make-seed-iso.py" "$BUILD/seed.iso" \
  "$SEED/user-data" "$SEED/meta-data" "$SEED/network-config" || die 'seed ISO build failed'

# ------------------------------------------------------------------- disk
step 'Disk'

note 'converting the cloud image to VDI (takes a minute)'
vbm clonemedium disk "$WBUILD\\$IMG" "$WBUILD\\$VM_NAME.vdi" --format VDI >/dev/null 2>&1 \
  || die 'clonemedium failed'
vbm modifymedium disk "$WBUILD\\$VM_NAME.vdi" --resize "$VM_DISK_MB" >/dev/null 2>&1 \
  || die 'resize failed'
ok "$VM_NAME.vdi at $((VM_DISK_MB / 1024)) GB (sparse; cloud-init grows the filesystem on boot)"

# --------------------------------------------------------------------- vm
step 'VM'

OSTYPE_ID="$(vbm list ostypes 2>/dev/null | tr -d '\r' | awk '/^ID:/{print $2}' \
             | grep -ix 'ubuntu24_lts_64' || true)"
[ -n "$OSTYPE_ID" ] || OSTYPE_ID=Ubuntu_64
note "ostype $OSTYPE_ID"

vbm createvm --name "$VM_NAME" --ostype "$OSTYPE_ID" --register \
     --basefolder "$WBUILD" >/dev/null 2>&1 || die 'createvm failed'

vbm modifyvm "$VM_NAME" \
  --memory "$VM_RAM_MB" --cpus "$VM_CPUS" \
  --ioapic on --rtcuseutc on --paravirtprovider kvm \
  --graphicscontroller vmsvga --vram 16 --audio-driver none \
  --nic1 bridged --bridgeadapter1 "$BRIDGE" --nictype1 virtio \
  --uart1 0x3f8 4 --uartmode1 file "$WBUILD\\serial.log" \
  --boot1 disk --boot2 dvd --boot3 none --boot4 none >/dev/null 2>&1 \
  || die 'modifyvm failed'
ok "$VM_RAM_MB MB RAM, $VM_CPUS vCPUs, bridged, serial console to E:\\LANtern-VM\\serial.log"

vbm storagectl "$VM_NAME" --name SATA --add sata --controller IntelAhci \
     --portcount 2 --bootable on >/dev/null 2>&1 || die 'storagectl failed'
vbm storageattach "$VM_NAME" --storagectl SATA --port 0 --device 0 \
     --type hdd --medium "$WBUILD\\$VM_NAME.vdi" >/dev/null 2>&1 || die 'attach disk failed'
vbm storageattach "$VM_NAME" --storagectl SATA --port 1 --device 0 \
     --type dvddrive --medium "$WBUILD\\seed.iso" >/dev/null 2>&1 || die 'attach seed failed'
ok 'disk and seed attached'

# ------------------------------------------------------------------- boot
step 'Boot'

rm -f "$BUILD/serial.log"
vbm startvm "$VM_NAME" --type headless >/dev/null 2>&1 || die 'startvm failed'
ok 'started headless'

# Watch the serial console rather than polling the network. If the boot fails,
# the reason is on the console and nowhere else -- a VM that never gets an
# address is otherwise completely silent about why.
note 'waiting for cloud-init (up to 12 minutes; watch E:\LANtern-VM\serial.log)'
DONE=0
for i in $(seq 1 144); do
  sleep 5
  [ -f "$BUILD/serial.log" ] || continue
  if grep -q 'LANTERN-FIRSTBOOT-OK\|LANTERN-CLOUD-INIT-DONE' "$BUILD/serial.log" 2>/dev/null; then
    DONE=1; break
  fi
  if grep -q 'FAILED:' "$BUILD/serial.log" 2>/dev/null; then
    bad 'the first-boot script reported a failure:'
    grep -n 'FAILED:' "$BUILD/serial.log" | tail -5
    die 'see E:\LANtern-VM\serial.log'
  fi
  [ $((i % 12)) -eq 0 ] && note "  still booting ($((i * 5))s)"
done
[ "$DONE" = 1 ] || die 'cloud-init did not finish in 12 minutes -- read E:\LANtern-VM\serial.log'
ok 'cloud-init finished'

# ----------------------------------------------------------------- verify
step 'Verify'

FAIL=0
if ping -c3 -W2 "$VM_IP" >/dev/null 2>&1; then
  ok "$VM_IP answers ping"
else
  bad "$VM_IP does not answer ping"; FAIL=1
fi

SSH_OPTS=(-i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
          -o ConnectTimeout=8 -o LogLevel=ERROR)
for i in $(seq 1 12); do
  ssh "${SSH_OPTS[@]}" "$VM_USER@$VM_IP" true 2>/dev/null && break
  sleep 5
done

if ssh "${SSH_OPTS[@]}" "$VM_USER@$VM_IP" true 2>/dev/null; then
  ok 'SSH with the key works'
  ssh "${SSH_OPTS[@]}" "$VM_USER@$VM_IP" '
    . /etc/os-release; echo "  $PRETTY_NAME"
    echo "  $(docker --version 2>/dev/null || echo "docker: MISSING")"
    echo "  $(docker compose version 2>/dev/null || echo "compose: MISSING")"
    df -h / | awk "NR==2{print \"  root fs: \"\$2\" total, \"\$4\" free\"}"
    free -g | awk "/Mem:/{print \"  memory:  \"\$2\" GB\"}"
    nproc | awk "{print \"  cpus:    \"\$1}"
  ' 2>/dev/null
  if ssh "${SSH_OPTS[@]}" "$VM_USER@$VM_IP" 'test -f /var/lib/lantern-firstboot.ok' 2>/dev/null; then
    ok 'Docker verified inside the guest'
  else
    bad 'first-boot marker missing -- Docker may not be installed'; FAIL=1
  fi
else
  bad 'SSH failed'; FAIL=1
fi

printf '\n'
if [ "$FAIL" != 0 ]; then
  bad 'VM is up but not fully verified. Read E:\LANtern-VM\serial.log.'
  exit 1
fi

printf '  LANtern VM is up.\n\n'
printf '    ssh -i %s %s@%s\n' "$KEY" "$VM_USER" "$VM_IP"
printf '    console password:  cat %s/.vm-login\n\n' "$BUILD"
printf '  Still to do:\n'
printf '    1. Copy the stack and its data over\n'
printf '    2. Cut the address over: Windows back to DHCP, VM to 192.168.0.115\n'
printf '    3. Point APP_URL and the docs at the new host\n\n'
printf '  Manage it with:\n'
printf '    VBoxManage startvm %s --type headless\n' "$VM_NAME"
printf '    VBoxManage controlvm %s acpipowerbutton\n\n' "$VM_NAME"
