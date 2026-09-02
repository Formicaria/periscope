#!/bin/bash
# Pull the latest periscope from git, reinstall, refresh CLI + unit, restart enabled bots.
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
echo "==> git pull"
git pull --ff-only
echo "==> deps"
./venv/bin/pip install --quiet -e core
for b in bots/*/; do ./venv/bin/pip install --quiet -e "$b"; done
echo "==> refresh CLI + unit"
sed "s|__DIR__|$DIR|g" periscope.cli > /usr/local/bin/periscope && chmod +x /usr/local/bin/periscope
sed "s|__DIR__|$DIR|g" periscope@.service > /etc/systemd/system/periscope@.service
systemctl daemon-reload
echo "==> restart enabled bots"
for u in $(systemctl list-units --type=service --all --no-legend 'periscope@*' | awk '{print $1}'); do
    systemctl is-enabled -q "$u" 2>/dev/null && systemctl restart "$u" && echo "    ↻ $u"
done
sleep 2
/usr/local/bin/periscope status
echo "Updated. Logs: periscope logs <bot>"
