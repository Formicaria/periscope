#!/bin/bash
# Pull the latest periscope from git, reinstall, refresh CLI + unit, restart.
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
echo "==> git pull"
git pull --ff-only
echo "==> deps"
# one invocation: pip resolves core, the web UI and every service together, so a version bump can never
# leave the box half-installed on "conflicting dependencies"
EDITABLES=(-e core -e web)
for b in bots/*/; do EDITABLES+=(-e "$b"); done
./venv/bin/pip install --quiet "${EDITABLES[@]}"
echo "==> refresh CLI + unit"
sed "s|__DIR__|$DIR|g" periscope.cli > /usr/local/bin/periscope && chmod +x /usr/local/bin/periscope
for u in $(systemctl list-unit-files --no-legend 'periscope@*' 2>/dev/null | awk '{print $1}'); do
    systemctl disable --now "$u" >/dev/null 2>&1 || true
done
rm -f /etc/systemd/system/periscope@.service
# the standalone Plex bot is now the plexrequests service — stop the old unit so it doesn't double-post
if [ -f /etc/systemd/system/displexia.service ]; then
    systemctl disable --now displexia >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/displexia.service
    echo "    retired the standalone Plex bot unit (its config was imported as the plexrequests service)"
fi
sed "s|__DIR__|$DIR|g" periscope.service > /etc/systemd/system/periscope.service
systemctl daemon-reload
systemctl enable periscope >/dev/null
echo "==> restart"
systemctl restart periscope
sleep 5
echo
periscope list || true
echo
periscope web || true
echo "  Logs: periscope logs"
