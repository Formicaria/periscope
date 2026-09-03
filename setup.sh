#!/bin/bash
# periscope installer (Debian/Ubuntu — LXC, VM, or bare metal). Idempotent, safe to re-run.
# Recommended: git clone the repo to /opt/periscope, cd into it, bash setup.sh
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv git >/dev/null

echo "==> Virtualenv + dependencies (core, every service, web UI)"
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -e core -e web
for b in bots/*/; do ./venv/bin/pip install --quiet -e "$b"; done
mkdir -p config data

echo "==> Installing the periscope CLI (/usr/local/bin/periscope)"
sed "s|__DIR__|$DIR|g" periscope.cli > /usr/local/bin/periscope
chmod +x /usr/local/bin/periscope

echo "==> Installing systemd service (periscope)"
# retire v1 per-bot units if this box ran them
for u in $(systemctl list-unit-files --no-legend 'periscope@*' 2>/dev/null | awk '{print $1}'); do
    systemctl disable --now "$u" >/dev/null 2>&1 || true
done
rm -f /etc/systemd/system/periscope@.service
# the standalone Plex bot is now the plexrequests service — stop the old unit so it doesn't double-post
if [ -f /etc/systemd/system/displexia.service ]; then
    systemctl disable --now displexia >/dev/null 2>&1 || true
    echo "    retired the standalone Plex bot unit (its config was imported as the plexrequests service)"
fi
sed "s|__DIR__|$DIR|g" periscope.service > /etc/systemd/system/periscope.service
systemctl daemon-reload
systemctl enable periscope >/dev/null
systemctl restart periscope

sleep 5
echo
periscope list || true
echo
echo "Done."
periscope web || true
echo "  Logs: periscope logs    Update: periscope update"
