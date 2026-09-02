#!/usr/bin/env bash
# Create a Debian 12 LXC on a Proxmox VE host with periscope cloned and installed inside it.
# Run ON THE PVE HOST as root:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/formicaria/periscope/main/deploy/lxc-create.sh)"
# Non-interactive:  CTID=210 HOSTNAME=periscope BRIDGE=vmbr0 IP=dhcp bash lxc-create.sh
set -euo pipefail

CTID="${CTID:-$(pvesh get /cluster/nextid)}"
HOSTNAME="${HOSTNAME:-periscope}"
STORAGE="${STORAGE:-local-lvm}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
DISK_GB="${DISK_GB:-6}"
RAM_MB="${RAM_MB:-1024}"        # ~150 MB per enabled bot
CORES="${CORES:-2}"
BRIDGE="${BRIDGE:-vmbr0}"
IP="${IP:-dhcp}"                # dhcp | 192.168.1.50/24
GW="${GW:-}"                    # required when IP is static
VLAN="${VLAN:-}"
UNPRIVILEGED="${UNPRIVILEGED:-1}"
ONBOOT="${ONBOOT:-1}"
BRANCH="${BRANCH:-main}"

command -v pct >/dev/null || { echo "run this on a Proxmox VE host"; exit 1; }

TEMPLATE="$(pveam list "$TEMPLATE_STORAGE" 2>/dev/null | awk '/debian-12-standard/{print $1}' | tail -1)"
if [[ -z "$TEMPLATE" ]]; then
  echo "downloading debian-12 template"
  pveam update >/dev/null
  T="$(pveam available --section system | awk '/debian-12-standard/{print $2}' | tail -1)"
  pveam download "$TEMPLATE_STORAGE" "$T"
  TEMPLATE="$TEMPLATE_STORAGE:vztmpl/$T"
fi

NET="name=eth0,bridge=$BRIDGE,ip=$IP"
[[ -n "$GW" ]] && NET="$NET,gw=$GW"
[[ -n "$VLAN" ]] && NET="$NET,tag=$VLAN"

echo "creating CT $CTID ($HOSTNAME) from $TEMPLATE"
pct create "$CTID" "$TEMPLATE" \
  --hostname "$HOSTNAME" --cores "$CORES" --memory "$RAM_MB" --swap 0 \
  --rootfs "$STORAGE:$DISK_GB" --net0 "$NET" \
  --unprivileged "$UNPRIVILEGED" --features nesting=1 --onboot "$ONBOOT" \
  --tags "periscope" --description "periscope — homelab monitoring bots for Discord"

pct start "$CTID"
echo "waiting for network"
for _ in $(seq 1 30); do
  pct exec "$CTID" -- ping -c1 -W1 deb.debian.org >/dev/null 2>&1 && break
  sleep 2
done

pct exec "$CTID" -- bash -c "apt-get update -qq && apt-get install -y -qq git ca-certificates >/dev/null"
pct exec "$CTID" -- bash -c "git clone -q -b $BRANCH https://github.com/formicaria/periscope /opt/periscope && bash /opt/periscope/setup.sh"

CT_IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
cat <<EOM

✔ periscope installed in CT $CTID ($CT_IP) at /opt/periscope — no bots enabled yet.

Next:
  pct enter $CTID
  periscope init proxmox && nano /opt/periscope/bots/proxmox/.env    # repeat per bot you want
  periscope enable proxmox
  periscope list | logs <bot> | update
EOM
