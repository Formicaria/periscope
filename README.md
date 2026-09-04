# periscope

```
 ██████╗ ███████╗██████╗ ██╗███████╗ ██████╗ ██████╗ ██████╗ ███████╗
 ██╔══██╗██╔════╝██╔══██╗██║██╔════╝██╔════╝██╔═══██╗██╔══██╗██╔════╝
 ██████╔╝█████╗  ██████╔╝██║███████╗██║     ██║   ██║██████╔╝█████╗
 ██╔═══╝ ██╔══╝  ██╔══██╗██║╚════██║██║     ██║   ██║██╔═══╝ ██╔══╝
 ██║     ███████╗██║  ██║██║███████║╚██████╗╚██████╔╝██║     ███████╗
 ╚═╝     ╚══════╝╚═╝  ╚═╝╚═╝╚══════╝ ╚═════╝ ╚═════╝ ╚═╝     ╚══════╝
```

**Your infrastructure, seen from Discord.** One repo, one install, one CLI — switch on only the integrations you run. Live status boards pinned in a channel, alerts that resolve themselves, and buttons for the things you'd otherwise SSH in for. Several Discord servers from one install, one bot able to post in all of them, and every post editable in the web UI without touching code. MIT licensed, one-command install, one-command updates.

## The bots

| Bot | Watches | Slash group | Highlights |
|---|---|---|---|
| [`proxmox`](bots/proxmox) | Proxmox VE | `/pve` | node/VM/CT/storage board, threshold + offline alerts, backup watcher, start/stop/reboot with confirm buttons |
| [`prometheus`](bots/prometheus) | Prometheus · Alertmanager · Grafana | `/prom` | Alertmanager → Discord with **Silence 1h/24h** buttons, PromQL from chat, target watch, panel screenshots |
| [`arr`](bots/arr) | Sonarr · Radarr · Lidarr · Prowlarr · qBittorrent/SAB · Plex/Jellyfin | `/arr` | grabs/imports with posters, queue + stall alerts, calendar, now-playing |
| [`unifi`](bots/unifi) | UniFi | `/unifi` | WAN/devices/clients board, new-client + device-offline alerts, kick/block/restart |
| [`docker`](bots/docker) | Docker · Portainer | `/docker` | containers up/exited board, restart-loop and unhealthy alerts, image updates, restart/start/stop/logs/stats |
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

Then run `periscope web`: it prints the UI address (`http://<box ip>:8090`) and a **one-time sign-in link** — open
it and the first-run flow takes it from there: paste a bot token → invite link → pick the server → create the
channel layout → switch services on one by one, each with a **Test** button that checks the credentials against the
real thing. Optionally add a Discord OAuth application (Discord page) so members holding an admin role can sign in
with Discord. Prefer a terminal? `periscope init` does the token/server step, then
`periscope config <service> KEY=VALUE` and `periscope enable <service>`.

Everything lives in one process (`python -m periscope`, unit `periscope.service`) and one file, `config/periscope.yaml`
(mode 0600). Services post as *bots* — Discord identities (called presences in the config). New installs get one
shared bot; any service can be pointed at its own application in the UI (**Bots**) so it keeps its own name and avatar.

**Several Discord servers, one install.** The **Discord** page holds as many servers as you like — each with its own
name (the one embeds carry), colour, status/alert channels and admin roles. Every service picks the server it posts in,
so the media stack can live in one server while the Plex request buttons sit in another; a bot posts in every server
its services use, and registers its slash commands there. Everything that is not per-server (log level, board refresh)
sits in its own card.

**Nothing needs a restart.** Save a setting and that one service is rebuilt in place — its cogs come off, its
clients close, it starts again from the new settings — while every other service and bot carries on. Editing
`config/periscope.yaml` by hand does the same: the process watches the file. Only a new bot token asks for a restart.

**Periscope remembers.** An event log behind everything (alerts, CI runs, grabs, requests, container changes,
plus CPU/memory/disk/queue samples) feeds a **Trends** page — uptime, alert counts, sparklines, a CSV download —
and a recap post for what happened overnight.

**Alerts you can answer.** Ack, Snooze and Resolve buttons on every alert, repeats folded into one card with a
count, escalation to a role when a CRITICAL goes unacked, and maintenance windows so the nightly backup stops
paging you.

**It can find your services.** The Discover page scans the network you point it at, identifies what answers by
its own API, prefills the settings and runs the Test — or reads them out of a `docker-compose.yml` or an existing
*arr `config.xml`.

**Every post is editable.** The **Messages** page lists each kind of post — status boards, alerts, the GitHub feed,
media grabs and imports, the Plex invite and request embeds — with a preview drawn the way Discord draws it. *Simple*
gives you the title, text, colour, footer and extra fields as a form with clickable `{{ variables }}`; *Code* is the raw
template (embed-shaped JSON, sandboxed Jinja) for anything more involved. Save applies to the next post, no restart; a
kind can also be switched off, and **Send a test post** puts the current version in its channel. Customisations live in
`config/messages.yaml` — delete an entry (or hit Reset) to go back to what the bot ships.

The Overview says in plain words what state every service is in — `running`, `starting`, `needs setup`, `error`, `off` —
and lists what needs attention with a link to the page that fixes it. `periscope list` prints the same.

Status boards are one message each, for good: a board remembers its message and edits it in place; after a
restart, an upgrade or a lost state file it finds its earlier message in the channel (pins, then recent history)
and reuses it, deleting any stray copies, and only posts when there is nothing to reuse. Every board carries a
`· <name> board` footer so it recognises itself.

### Coming from displexia (the standalone Plex bot)

displexia is gone as a separate thing — its features are the `plexrequests` service inside periscope, running in the
same process as everything else:

| displexia | periscope |
|---|---|
| its own repo, venv and `displexia.service` unit | the `plexrequests` service in this repo; one `periscope.service` for everything |
| `/opt/displexia/.env` | `config/periscope.yaml` (its keys imported on the first `periscope update`) |
| `state.json` / `stats.json` in `/opt/displexia` | imported once into `data/state.json`, so sticky embeds and request history carry over |
| its own bot token and Discord server | a *bot* and a *server* in the UI — usually kept exactly as they were |
| Get Plex Access · Search & Request · status board · new-on-Plex · auto-revoke · usage stats | all of it, plus the Messages page for rewording any of those embeds |
| edits meant changing Python | settings on `/services/plexrequests`, wording on `/messages` |

`periscope update` on a box that ran displexia does the import, stops and removes the old unit, and leaves
`/opt/displexia` untouched — delete it once you're happy.

### Coming from v1 (one unit per bot)

`periscope update` does it: the runtime imports every `bots/*/.env` (and `/opt/displexia/.env` if that bot ran on
the box) into `config/periscope.yaml` on first start — one bot identity per old bot, so nothing changes in Discord — and
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
periscope web                  the admin UI address + a one-time sign-in link
periscope list                 every service: state · bot and server it posts in · what needs attention
periscope enable <svc…>        turn a service on (config must be complete)     periscope disable <svc…>
periscope check <svc>          test a service's credentials right now
periscope config <svc> [K=V]   show or set a service's settings, secrets masked
periscope bots …               add bot identities, set tokens, assign services (alias: presence)
periscope layout               apply the #git-* / #op-* channel convention, print the repo→channel map
periscope status | logs [svc]  runtime status, live log (filtered to one service)
periscope restart|start|stop   the service        periscope update   git pull, reinstall, restart
```

Config changes (UI or CLI) apply on restart; the UI shows a "restart to apply" banner and a button.

## Discord setup (once per bot)

<https://discord.com/developers/applications> → **New Application** → **Bot** → **Reset Token**. Paste it into the web
UI (or `periscope bots token default`); it prints the invite link with the right permissions and detects the
server once the bot joins. A bot only registers its slash commands on servers it is actually in; one it was never
invited to shows up under "needs attention" with the invite link. No privileged intents are needed unless you enable `plexrequests` (Server Members +
Message Content, for auto-revoke and typed requests) — the UI says so on that service's page.

For THE LAB specifically: [`docs/discord-apps.md`](docs/discord-apps.md) has the existing applications and channel
ids, [`docs/server-layout.md`](docs/server-layout.md) the channel plan.

## Several people, one server

Each member runs their own periscope against their own lab with a distinct `LAB_NAME`. Nothing needs to be exposed inbound except bots that receive webhooks: `github` must be reachable from GitHub; `prometheus` and `arr` only from their own LAN. Default `WEBHOOK_PORT`s are distinct per bot (8080–8084) so everything coexists on one box. Members can share the same Discord applications (Discord allows several gateway sessions per bot token) or create their own.

## Writing a new bot

Copy any `bots/<name>/`, swap `client.py` and `cogs/`, add it to the CI matrix. Conventions: [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md).

## License

MIT
