# periscope

```
 ██████╗ ███████╗██████╗ ██╗███████╗ ██████╗ ██████╗ ██████╗ ███████╗
 ██╔══██╗██╔════╝██╔══██╗██║██╔════╝██╔════╝██╔═══██╗██╔══██╗██╔════╝
 ██████╔╝█████╗  ██████╔╝██║███████╗██║     ██║   ██║██████╔╝█████╗
 ██╔═══╝ ██╔══╝  ██╔══██╗██║╚════██║██║     ██║   ██║██╔═══╝ ██╔══╝
 ██║     ███████╗██║  ██║██║███████║╚██████╗╚██████╔╝██║     ███████╗
 ╚═╝     ╚══════╝╚═╝  ╚═╝╚═╝╚══════╝ ╚═════╝ ╚═════╝ ╚═╝     ╚══════╝
```

**Your homelab, seen from Discord.** One repo, one install, one CLI — enable only the bots you run. Live status boards pinned in a channel, alerts that resolve themselves, and buttons for the things you'd otherwise SSH in for. Built for a shared server where several people each run their own stack: every embed carries the `LAB_NAME` it came from. MIT licensed, one-command install, one-command updates.

## The bots

| Bot | Watches | Slash group | Highlights |
|---|---|---|---|
| [`proxmox`](bots/proxmox) | Proxmox VE | `/pve` | node/VM/CT/storage board, threshold + offline alerts, backup watcher, start/stop/reboot with confirm buttons |
| [`prometheus`](bots/prometheus) | Prometheus · Alertmanager · Grafana | `/prom` | Alertmanager → Discord with **Silence 1h/24h** buttons, PromQL from chat, target watch, panel screenshots |
| [`arr`](bots/arr) | Sonarr · Radarr · Lidarr · Prowlarr · qBittorrent/SAB · Plex/Jellyfin | `/arr` | grabs/imports with posters, queue + stall alerts, calendar, now-playing |
| [`unifi`](bots/unifi) | UniFi | `/unifi` | WAN/devices/clients board, new-client + device-offline alerts, kick/block/restart |
| [`github`](bots/github) | a GitHub organization | `/gh` | push/PR/issue/release/CI/fork/star feed, per-repo channel routing, CI-failure alerts, polling fallback |

Everything shares [`core/`](core): branded embeds, an alert router (dedupe, cooldown, resolve-in-place, role ping on critical), pinned live status boards, Confirm/Paginate/Refresh views, an HMAC-checked webhook server, and JSON state. Each bot is its own Discord application and its own systemd service, so one crashing or restarting never touches the others.

## Install (Debian/Ubuntu — LXC, VM, or bare metal)

```bash
apt-get update && apt-get install -y git
git clone https://github.com/formicaria/periscope /opt/periscope
cd /opt/periscope
bash setup.sh                      # python + venv + all bots + `periscope` CLI. Enables nothing yet.

periscope init proxmox             # guided setup — see below
```

`periscope init <bot>` walks you through it and checks every answer before saving: paste the bot token (verified against Discord, prints the invite link if the bot isn't in a server yet), it finds or **creates the channels and roles** it needs, asks for the service credentials (Proxmox token, *arr keys, UniFi login, GitHub PAT…) and tests each one against the real service, writes `bots/<bot>/.env`, and starts the bot. Shared answers (server, channels, roles, lab name) are remembered in `periscope.json`, so the second bot only asks for its own token and credentials. Repeat for every bot you want (`proxmox prometheus arr unifi github`). Prefer editing by hand? `periscope init <bot> --manual` copies the example `.env` for you.

Each bot's `.env` and `data/` stay inside `bots/<bot>/` and are gitignored. `setup.sh` is idempotent: re-running it (or `periscope update`) re-enables every bot whose `.env` has a token.

### Proxmox LXC in one command

On the PVE host, as root — creates a Debian 12 unprivileged CT (2 cores / 1 GB / 6 GB, DHCP on `vmbr0`) with periscope installed:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/formicaria/periscope/main/deploy/lxc-create.sh)"
```

Override anything with env vars: `CTID=210 IP=192.168.1.50/24 GW=192.168.1.1 VLAN=20 STORAGE=zfs bash lxc-create.sh`. Then `pct enter <CTID>` and `init` / `enable` the bots you want.

### Docker (secondary)

Same per-bot `.env` files, one service per bot:

```bash
periscope init proxmox && nano bots/proxmox/.env      # or cp bots/proxmox/.env.example bots/proxmox/.env
docker compose up -d proxmox unifi                    # only the bots you want
```

Images: `ghcr.io/formicaria/periscope-<bot>:latest` (amd64 + arm64), or uncomment `build:` in `docker-compose.yml`.

## The `periscope` CLI

```
periscope list              every bot with active / enabled / .env state
periscope init <bot>        guided setup (verifies credentials, creates channels/roles, starts the bot); --manual to skip
periscope enable <bot…>     start now + on boot (needs a filled .env)
periscope disable <bot…>    stop and remove from boot
periscope status [bot]      service status
periscope logs [bot]        follow the live logs (all bots when omitted)
periscope restart [bot…]    restart one bot, or every running one
periscope config <bot>      the active .env, secrets masked
periscope update            git pull, reinstall, restart every enabled bot
```

Every future update: `git push` from your machine, then `periscope update` on the box.

## Discord setup (once per bot)

The only thing you do in the Discord developer portal: <https://discord.com/developers/applications> → **New Application** (name it after the service, e.g. `Proxmox`) → **Bot** → **Reset Token**. No privileged intents are needed. `periscope init` takes it from there — it prints the invite link with the right permissions, detects the server once the bot joins, and creates or finds the channels and roles. (Doing it by hand instead: OAuth2 → URL Generator, scopes `bot` + `applications.commands`; then Developer Mode → right-click → Copy ID for every `*_ID` in `.env.example`.)

Service-side steps (API tokens, webhooks) are in each bot's README. For THE LAB specifically: [`docs/discord-apps.md`](docs/discord-apps.md) has the existing applications and invite links, [`docs/server-layout.md`](docs/server-layout.md) the channel plan, and `scripts/setup_server.py` creates those channels and roles idempotently.

## Several people, one server

Each member runs their own periscope against their own lab with a distinct `LAB_NAME`. Nothing needs to be exposed inbound except bots that receive webhooks: `github` must be reachable from GitHub; `prometheus` and `arr` only from their own LAN. Default `WEBHOOK_PORT`s are distinct per bot (8080–8084) so everything coexists on one box. Members can share the same Discord applications (Discord allows several gateway sessions per bot token) or create their own.

## Writing a new bot

Copy any `bots/<name>/`, swap `client.py` and `cogs/`, add it to the CI matrix. Conventions: [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md).

## License

MIT
