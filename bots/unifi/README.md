# periscope · unifi

A Discord bot that watches your UniFi network and posts a live status board, alerts, and `/unifi …` slash commands. Works with UniFi OS consoles (UDM, UDM Pro/SE, UDR, UCG-Ultra/Max, Cloud Key Gen2+) and self-hosted Network Application controllers.

Part of the [periscope](../../README.md) pack, built on its shared core. One instance per lab; several labs can share the same Discord channels — every embed carries your `LAB_NAME`.

## What it does

**Live status board** — one pinned message in `STATUS_CHANNEL_ID`, edited in place every `STATUS_INTERVAL_S` seconds, with a 🔄 Refresh button:

> 🟢 **UniFi — default**
> 🟢 **WAN** `203.0.113.5` · 12 ms · up 14d 3h · Example ISP
> 🟢 LAN · 🟢 WLAN
> 👥 **37 clients** · 21 wired · 14 wireless · 2 guest
> ⬇ 4.2 MB/s ⬆ 640.0 KB/s
>
> **Devices (4)**
> 🟢 **Dream Machine** (UDMPRO) · cpu 9% · 51°C · 0 clients
> 🟢 **Core Switch** (USW-24-PoE) · cpu 12% · 47°C · 18 clients · ⬆ fw
> 🟢 **Office AP** (U6-LR) · cpu 4% · 9 clients
> 🔴 **Garage AP** (U6-Lite) · 0 clients

Embed color goes red when the WAN or any device is down, yellow when a subsystem is in warning or latency is high.

**Alerts** posted to `ALERT_CHANNEL_ID` (CRITICAL ones ping `ALERT_ROLE_ID`; resolved alerts are edited green in place):

| Alert | Severity | Fingerprint | Resolves |
|---|---|---|---|
| Device offline | CRITICAL | `unifi:device:<mac>:down` | when it comes back |
| WAN down | CRITICAL | `unifi:wan:down` | when WAN is back |
| Controller unreachable (3 failed polls) | CRITICAL | `unifi:unreachable` | on next successful poll |
| WAN latency > `UNIFI_WAN_LATENCY_WARN_MS` for 3 polls | WARNING | `unifi:wan:latency` | when it drops below |
| Device CPU > `UNIFI_DEVICE_CPU_WARN` for 3 polls | WARNING | `unifi:device:<mac>:cpu` | when it drops below |
| Firmware update available | INFO | `unifi:device:<mac>:upgrade:<version>` | never — posted once per version |
| New client joined | INFO | (plain embed: name/hostname, MAC, IP, SSID or switch port, vendor) | n/a |

"New" means a MAC the bot has not seen in `UNIFI_KNOWN_CLIENTS_TTL_DAYS`. On the very first poll the bot silently learns every current client so you don't get 40 alerts at once.

## Slash commands

All commands live under one group so several bots can coexist in a server.

| Command | What it does | Admin |
|---|---|---|
| `/unifi clients [wireless_only] [search]` | Paginated list of active clients: name, IP, MAC, SSID or switch port, signal, uptime, traffic | |
| `/unifi client <mac_or_name>` | Detail card for one client (autocompletes from the live client list) | |
| `/unifi kick <mac>` | Disconnect a client (it may reconnect) — asks for confirmation | ✅ |
| `/unifi block <mac>` | Block a client — asks for confirmation | ✅ |
| `/unifi unblock <mac>` | Unblock a client — asks for confirmation | ✅ |
| `/unifi devices` | Every adopted device: state, model, type, firmware, CPU/mem/temp, clients, uptime | |
| `/unifi device <name>` | Detail card for one device; admins get a **Restart** button | |
| `/unifi restart <name>` | Restart a device — asks for confirmation | ✅ |
| `/unifi wan` | WAN IP, latency, uptime, ISP, throughput, last speedtest | |
| `/unifi events [limit]` | Last N controller events (default 20, max 50) | |
| `/unifi alarms` | Active (unarchived) controller alarms | |

Admin = a role in `ADMIN_ROLE_IDS`, or the server Administrator permission if that is unset. Command responses are ephemeral (only you see them).

## Setup (≈10 minutes)

### 1. Create the Discord bot

1. Go to <https://discord.com/developers/applications> → **New Application** → name it (e.g. `unifi · my-lab`).
2. **Bot** tab → **Reset Token** → copy it into `DISCORD_TOKEN`. No privileged intents are needed.
3. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; bot permissions **Send Messages**, **Embed Links**, **Manage Messages** (to pin the status board), **Read Message History**. Open the generated URL and add the bot to your server.
4. Turn on Developer Mode in Discord (Settings → Advanced), then right-click → **Copy ID** for the server (`GUILD_ID`), the alert channel, the status channel, and optionally an alert role and admin roles.

### 2. Create a UniFi local user

The bot needs a **local** account (Ubiquiti SSO / cloud accounts and 2FA do not work with the API).

**UniFi OS console (UDM/UDR/UCG/Cloud Key):** open the console UI → **Settings → Admins & Users → Admins → Add** (Create New Admin) → set **Restrict to local access only**, choose a username/password, and give the **Network** app the **View Only** role (monitoring only) or **Site Admin** (if you want `/unifi kick|block|restart`). Console-level role can stay at *Limited Admin*/none.

**Self-hosted Network Application:** **Settings → System → Admins → Add Admin** → *Manually set and share the password* → role **Read Only** (or **Administrator** for the admin commands).

A read-only user is enough for the status board, alerts, and every read command. `kick`, `block`, `unblock` and `restart` return an error from the controller if the account cannot write.

### 3. Configure

```bash
periscope init unifi
nano bots/unifi/.env    # fill in DISCORD_TOKEN, GUILD_ID, channel ids, UNIFI_URL, UNIFI_USER, UNIFI_PASS
```

Pick `UNIFI_URL` / `UNIFI_IS_UNIFI_OS` for your setup:

| You have | `UNIFI_URL` | `UNIFI_IS_UNIFI_OS` |
|---|---|---|
| UDM / UDM Pro / UDR / UCG / Cloud Key Gen2+ | `https://192.168.1.1` (no port) | `true` |
| Self-hosted controller (Docker, VM, Cloud Key Gen1) | `https://host:8443` | `false` |

### 4. Run

From the periscope checkout (see the [pack README](../../README.md) for install):

```bash
periscope init unifi          # creates bots/unifi/.env
nano bots/unifi/.env          # paste the values from the steps above
periscope enable unifi
periscope logs unifi          # look for "ready as ... (lab=my-lab)" and "synced N app commands"
```

Docker instead: `docker compose up -d unifi` from the repo root uses the same `bots/unifi/.env`.

## Environment variables

Common variables come from the shared core; UniFi ones are specific to this bot. Everything is listed with comments in [`.env.example`](.env.example).

| Var | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | yes | | Bot token |
| `LAB_NAME` | | `lab` | Footer on every embed; identifies your lab |
| `LAB_COLOR` | | `5865F2` | Hex accent color |
| `GUILD_ID` | | | Server id — set it so commands sync instantly |
| `ALERT_CHANNEL_ID` | | | Channel for alerts and new-client notices |
| `STATUS_CHANNEL_ID` | | | Channel for the pinned status board |
| `ALERT_ROLE_ID` | | | Role pinged on CRITICAL |
| `ADMIN_ROLE_IDS` | | | Comma-separated role ids allowed to kick/block/restart |
| `STATUS_INTERVAL_S` | | `60` | Poll and board refresh interval |
| `DATA_DIR` | | `data` | Persistent state (alert message ids, known clients) |
| `WEBHOOK_PORT` | | `8083` | Port of the `/health` endpoint |
| `LOG_LEVEL` | | `INFO` | |
| `UNIFI_URL` | yes | | `https://192.168.1.1` (UniFi OS) or `https://host:8443` (self-hosted) |
| `UNIFI_USER` | yes | | Local admin username |
| `UNIFI_PASS` | yes | | Local admin password |
| `UNIFI_SITE` | | `default` | Site name from the controller URL |
| `UNIFI_IS_UNIFI_OS` | | `true` | `true`: login at `/api/auth/login`, API under `/proxy/network`. `false`: `/api/login`, no prefix |
| `VERIFY_SSL` | | `false` | Verify the controller's TLS certificate |
| `UNIFI_ALERT_NEW_CLIENTS` | | `true` | Post an embed when an unknown client joins |
| `UNIFI_WAN_LATENCY_WARN_MS` | | `100` | WARNING after 3 polls above this |
| `UNIFI_DEVICE_CPU_WARN` | | `80` | WARNING after 3 polls above this % |
| `UNIFI_KNOWN_CLIENTS_TTL_DAYS` | | `30` | A client unseen for this long counts as new again |

Missing required variables abort startup with a clear message.

## Health check and ports

The bot receives no webhooks, but it still starts the `periscope` webhook server with **no routes** so that `GET /health` exists (`{"ok": true}` once connected to Discord, `503` otherwise). The Dockerfile's `HEALTHCHECK` curls it from inside the container, so `docker-compose.yml` publishes **no ports**. Publish `WEBHOOK_PORT` yourself only if an external monitor should probe it.

## How it talks to UniFi

- Cookie session: `POST <login_path>` with the local credentials; the `TOKEN` (UniFi OS) or `unifises` (self-hosted) cookie and the `X-CSRF-Token` header are captured from the response and sent on every request. Cookies are handled explicitly because aiohttp's default jar ignores cookies from bare-IP hosts.
- Any `401`/`403` triggers one transparent re-login and retry.
- Endpoints used (all under `<prefix>/api/s/<site>/`): `stat/sta`, `stat/device`, `stat/health`, `stat/alarm`, `stat/event`, `rest/user`, `cmd/devmgr` (restart), `cmd/stamgr` (kick/block/unblock).
- Only outbound connections: to Discord and to your controller. Nothing needs to be exposed from your lab.

## State

`DATA_DIR/state.json` holds the status-board message id, active alert message ids (so restarts resolve alerts in place), the set of known client MACs with last-seen timestamps, and which firmware versions have already been announced. Delete it to start fresh (the next poll re-seeds known clients silently).

## Development

```bash
python -m venv venv && . venv/bin/activate
pip install -e core -e bots/unifi pytest pytest-asyncio
pytest bots/unifi
```

Conventions: [docs/CONTRIBUTING.md](../../docs/CONTRIBUTING.md).

## License

MIT
