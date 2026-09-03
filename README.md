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
| [`plexrequests`](bots/plexrequests) | Plex · Overseerr/Jellyseerr or Radarr/Sonarr | `/requests` + `/plexinvite` | **Get Plex Access** invites with role grant, **Search & Request** with availability cards, live status board, new-on-Plex feed, auto-revoke, usage stats |

Everything shares [`core/`](core): branded embeds, an alert router (dedupe, cooldown, resolve-in-place, role ping on critical), pinned live status boards, Confirm/Paginate/Refresh views, an HMAC-checked webhook server, and JSON state. Every integration is a *service* you switch on individually; they all run in one process and are supervised separately, so one failing never touches the others. Design notes: [`docs/SPEC-v2.md`](docs/SPEC-v2.md).

## Install (Debian/Ubuntu — LXC, VM, or bare metal)

```bash
apt-get update && apt-get install -y git
git clone https://github.com/formicaria/periscope /opt/periscope
cd /opt/periscope
bash setup.sh          # python + venv + every service + web UI, one systemd service: periscope
```

Then open the web UI (`periscope web` prints the address, default `http://<box>:8090`), sign in once with the
setup token from the log (`journalctl -u periscope | grep 'setup token'`), and the first-run flow takes it from
there: paste a bot token → invite link → pick the server → create the channel layout → enable services one by one,
each with a **Test** button that checks the credentials against the real thing. Afterwards sign-in is Discord OAuth
for members holding `@lab-admin`. Prefer a terminal? `periscope init` does the token/server step, then
`periscope config <service> KEY=VALUE` and `periscope enable <service>`.

Everything lives in one process (`python -m periscope`, unit `periscope.service`) and one file, `config/periscope.yaml`
(mode 0600). Services post through *presences* — Discord identities. New installs get one shared bot; any service can
be pointed at its own application in the UI (**Presences**) so it keeps its own name and avatar.

### Coming from v1 (one unit per bot)

`periscope update` does it: the runtime imports every `bots/*/.env` (and `/opt/displexia/.env` if that bot ran on
the box) into `config/periscope.yaml` on first start — one presence per old bot, so nothing changes in Discord — and
retires the `periscope@<bot>` units. The old `.env` files are left untouched; delete them when you're happy.

### Proxmox LXC in one command

On the PVE host, as root — creates a Debian 12 unprivileged CT (2 cores / 1 GB / 6 GB, DHCP on `vmbr0`) with periscope installed:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/formicaria/periscope/main/deploy/lxc-create.sh)"
```

Override anything with env vars: `CTID=210 IP=192.168.1.50/24 GW=192.168.1.1 VLAN=20 STORAGE=zfs bash lxc-create.sh`.

### Docker (secondary)

`docker compose up -d` from the repo root runs the same single process from `ghcr.io/formicaria/periscope` with
`./config` and `./data` mounted; ports 8080 (webhooks) and 8090 (web UI).

## The `periscope` CLI

```
periscope web                  where the admin UI is
periscope list                 every service: enabled · state · presence
periscope enable <svc…>        turn a service on (config must be complete)     periscope disable <svc…>
periscope check <svc>          test a service's credentials right now
periscope config <svc> [K=V]   show or set a service's settings, secrets masked
periscope presence …           add identities, set tokens, assign services
periscope layout               apply the #git-* / #op-* channel convention, print the repo→channel map
periscope status | logs [svc]  runtime status, live log (filtered to one service)
periscope restart|start|stop   the service        periscope update   git pull, reinstall, restart
```

Config changes (UI or CLI) apply on restart; the UI shows a "restart to apply" banner and a button.

## Discord setup (once per presence)

<https://discord.com/developers/applications> → **New Application** → **Bot** → **Reset Token**. Paste it into the web
UI (or `periscope presence token default`); it prints the invite link with the right permissions and detects the
server once the bot joins. No privileged intents are needed unless you enable `plexrequests` (Server Members +
Message Content, for auto-revoke and typed requests) — the UI says so on that service's page.

For THE LAB specifically: [`docs/discord-apps.md`](docs/discord-apps.md) has the existing applications and channel
ids, [`docs/server-layout.md`](docs/server-layout.md) the channel plan.

## Several people, one server

Each member runs their own periscope against their own lab with a distinct `LAB_NAME`. Nothing needs to be exposed inbound except bots that receive webhooks: `github` must be reachable from GitHub; `prometheus` and `arr` only from their own LAN. Default `WEBHOOK_PORT`s are distinct per bot (8080–8084) so everything coexists on one box. Members can share the same Discord applications (Discord allows several gateway sessions per bot token) or create their own.

## Writing a new bot

Copy any `bots/<name>/`, swap `client.py` and `cogs/`, add it to the CI matrix. Conventions: [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md).

## License

MIT
