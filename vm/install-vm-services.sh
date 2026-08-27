#!/usr/bin/env bash
# Install the host-level services LANtern needs on the VM.
#
# THE ONE THAT MATTERS: lantern-dbnet
#
# Game servers do not run on the compose network. Wings puts them on its own
# bridge, `pelican_nw` (172.23.0.1, named in /etc/pelican/config.yml), which
# MariaDB is not attached to. So a plugin running inside the CS2 container --
# WeaponPaints, which is where every loadout lives -- cannot resolve the
# hostname `database` and silently falls back to no skins at all. Nothing logs
# an error the operator will see; the loadout UI just returns defaults.
#
# The attachment cannot be declared in compose. Wings creates `pelican_nw`
# itself, and only once it starts, so an `external: true` reference fails at
# `compose up` on a machine where no server has ever run. On Windows this was
# papered over by a step in a logon script, which is why it never survived the
# move to Linux.
#
# A timer rather than a one-shot, because the network can appear at three
# different moments: at boot, when Wings first starts, and when the first game
# server is created. A one-shot ordered after docker.service catches only the
# first of those.
#
# Run this ON THE VM (or via ssh) after the stack is in place.

set -uo pipefail

STACK_DIR="${STACK_DIR:-/opt/lantern/stack}"
DB_CONTAINER="${DB_CONTAINER:-stack-database-1}"
NET="${NET:-pelican_nw}"
ALIAS="${ALIAS:-database}"

ok()   { printf '  \033[32m%s\033[0m\n' "$*"; }
bad()  { printf '  \033[31m%s\033[0m\n' "$*"; }
step() { printf '\n\033[36m%s\033[0m\n' "$*"; }
die()  { bad "$*"; exit 1; }

[ "$(uname -s)" = Linux ] || die 'Run this on the VM, not on Windows.'
command -v docker >/dev/null || die 'docker is not installed here'
[ -d "$STACK_DIR" ] || die "stack not found at $STACK_DIR"

step 'Installing lantern-dbnet'

sudo tee /usr/local/sbin/lantern-dbnet.sh >/dev/null <<EOF
#!/usr/bin/env bash
# Attach MariaDB to Wings' game network so plugins can resolve '$ALIAS'.
# Idempotent and quiet: it runs every minute.
set -uo pipefail
docker network inspect $NET >/dev/null 2>&1 || exit 0
docker inspect -f '{{.State.Running}}' $DB_CONTAINER 2>/dev/null | grep -q true || exit 0
docker network inspect $NET --format '{{range \$k,\$v := .Containers}}{{\$v.Name}}{{println}}{{end}}' \\
  | grep -qx '$DB_CONTAINER' && exit 0
docker network connect --alias $ALIAS $NET $DB_CONTAINER \\
  && logger -t lantern-dbnet "attached $DB_CONTAINER to $NET as $ALIAS"
EOF
sudo chmod 0755 /usr/local/sbin/lantern-dbnet.sh

sudo tee /etc/systemd/system/lantern-dbnet.service >/dev/null <<'EOF'
[Unit]
Description=Attach MariaDB to Wings' game network
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/lantern-dbnet.sh
EOF

sudo tee /etc/systemd/system/lantern-dbnet.timer >/dev/null <<'EOF'
[Unit]
Description=Keep MariaDB attached to Wings' game network

[Timer]
OnBootSec=45s
OnUnitActiveSec=60s
AccuracySec=5s

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now lantern-dbnet.timer >/dev/null 2>&1

systemctl is-enabled lantern-dbnet.timer >/dev/null 2>&1 \
  && ok 'lantern-dbnet.timer enabled (runs every 60s, idempotent)' \
  || die 'lantern-dbnet.timer did not enable'

step 'Swap'

# The VM has ~17.6 GB usable and Minecraft is allocated 11 GB of it. Without
# swap, a transient spike does not degrade -- the kernel OOM killer picks a
# process and ends it, with no warning and typically mid-save. A small swap
# file turns that into a stutter and, more usefully, into a signal: the control
# UI's swap gauge warns at the first 1% of use, so "you are over-allocated"
# becomes something you can see coming rather than something you find out about
# from a friend whose world is gone.
#
# swappiness 10, not the default 60: this is a shock absorber, not tiered
# memory. At 60 the kernel pages out idle game-server heap during normal play
# and you feel it.
SWAP_GB="${SWAP_GB:-4}"
if swapon --show 2>/dev/null | grep -q /swapfile; then
  ok "swap already active ($(free -h | awk '/Swap:/{print $2}'))"
else
  sudo swapoff -a 2>/dev/null
  # fallocate leaves holes on some filesystems, which swapon rejects.
  sudo dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_GB * 1024)) status=none
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab \
    || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  swapon --show | grep -q /swapfile \
    && ok "${SWAP_GB} GB swap active and in /etc/fstab" \
    || bad 'swap did not come up'
fi

printf 'vm.swappiness=10\n' | sudo tee /etc/sysctl.d/99-lantern-swap.conf >/dev/null
sudo sysctl -q -w vm.swappiness=10
ok "swappiness $(cat /proc/sys/vm/swappiness) (only swaps under real pressure)"

step 'Convenience'

# `lantern` is the one-server-at-a-time switch. On Windows it needed a .cmd
# shim to get into WSL; here it just needs to be on PATH.
if [ -x "$STACK_DIR/lantern" ]; then
  sudo ln -sf "$STACK_DIR/lantern" /usr/local/bin/lantern
  ok "lantern -> $STACK_DIR/lantern"
else
  bad "$STACK_DIR/lantern is not executable -- chmod +x it"
fi

id -nG "$USER" | grep -qw docker \
  && ok "$USER is in the docker group" \
  || bad "$USER is NOT in the docker group -- log out and back in, or: sudo usermod -aG docker $USER"

step 'Verify'
sudo /usr/local/sbin/lantern-dbnet.sh
if docker network inspect "$NET" >/dev/null 2>&1; then
  if docker network inspect "$NET" --format '{{range $k,$v := .Containers}}{{$v.Name}}{{println}}{{end}}' \
     | grep -qx "$DB_CONTAINER"; then
    ok "$DB_CONTAINER is attached to $NET"
  else
    bad "$DB_CONTAINER is not attached yet (is it running?)"
  fi
else
  ok "$NET does not exist yet -- expected until Wings starts; the timer will catch it"
fi
printf '\n'
