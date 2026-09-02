#!/bin/bash
# periscope installer (Debian/Ubuntu — LXC, VM, or bare metal). Idempotent, safe to re-run.
# Recommended: git clone the repo to /opt/periscope, cd into it, bash setup.sh
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv git >/dev/null

echo "==> Virtualenv + dependencies (core + every bot; enabling is per bot)"
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -e core
for b in bots/*/; do ./venv/bin/pip install --quiet -e "$b"; done

echo "==> Installing the periscope CLI (/usr/local/bin/periscope)"
sed "s|__DIR__|$DIR|g" periscope.cli > /usr/local/bin/periscope
chmod +x /usr/local/bin/periscope

echo "==> Installing systemd template unit (periscope@<bot>)"
sed "s|__DIR__|$DIR|g" periscope@.service > /etc/systemd/system/periscope@.service
systemctl daemon-reload

# Enable every bot that has a configured .env; restart the ones already running.
enabled=0
for d in bots/*/; do
    b="$(basename "$d")"
    if [ -f "$d/.env" ] && grep -Eq '^DISCORD_TOKEN=.+' "$d/.env"; then
        chmod 600 "$d/.env"; mkdir -p "$d/data"
        systemctl enable --now "periscope@$b" >/dev/null 2>&1
        systemctl restart "periscope@$b"
        echo "    ● $b enabled"
        enabled=$((enabled+1))
    fi
done

echo
if [ "$enabled" -eq 0 ]; then
    cat <<MSG
No bots enabled yet. For each one you want:
    periscope init <bot>          # creates bots/<bot>/.env from the example
    nano $DIR/bots/<bot>/.env     # DISCORD_TOKEN + service credentials
    periscope enable <bot>

Available: $(ls bots | tr '\n' ' ')
MSG
else
    sleep 2; periscope status
    echo; echo "Done. Logs: periscope logs <bot>    Update: periscope update"
fi
