# periscope · arr

Discord bot for your media stack: **Sonarr, Radarr, Lidarr, Prowlarr**, the download clients behind them (**qBittorrent, SABnzbd**) and the media servers in front of them (**Plex, Jellyfin**).

Part of the [periscope](../../README.md) pack, built on its shared core. Every service is optional: set a URL and it's on, leave it blank and the bot never mentions it. One container per lab; several labs can share the same Discord channels.

## What it does

- **Webhook feed** — Sonarr/Radarr/Lidarr/Prowlarr push *Grab / Import / Upgrade / Rename / Added / Deleted / Health / Update* events to the bot, which posts a rich embed (poster thumbnail, title + year, `S06E01`, quality, release group, indexer, download client, size) to `MEDIA_CHANNEL_ID`.
- **Health alerts** — a `HealthIssue` webhook becomes a deduplicated alert in `ALERT_CHANNEL_ID`; the matching `HealthRestored` edits it green.
- **Stalled-download watcher** — every 5 minutes the bot compares each queue item's `sizeleft` to the last poll. Anything still `downloading` with no progress for `ARR_QUEUE_STALL_MIN` minutes raises a WARNING; it resolves itself when the item moves or leaves the queue.
- **Unreachable alerts** — 3 consecutive API failures for any service fire a CRITICAL `<service> unreachable` alert (pings `ALERT_ROLE_ID`), resolved when it responds again.
- **Live status board** — one pinned message in `STATUS_CHANNEL_ID`, edited every `STATUS_INTERVAL_S` seconds, with a 🔄 Refresh button.
- **Slash commands** under `/arr` for the queue, calendar, lookups, health, download clients and now-playing.

### What it looks like

**Status board** (pinned, updates in place):

> 🟢 **Media stack**
> 🟢 sonarr  🟢 radarr  🟢 lidarr  🟢 prowlarr  🟢 qbittorrent  🔴 sabnzbd  🟢 plex  🟢 jellyfin
> **Queues** — sonarr: **4** queued, 2 downloading · radarr: **1** queued, 1 downloading
> **Transfer** — qBit ⬇️ 41.2 MB/s ⬆️ 3.1 MB/s
> **Streams (2)** — ▶️ Severance – S02E04 Woe's Hollow — alice · ⏸️ Heat (1995) — bob
> **Disk** — `█████████░░░  74.8%` 4.1 TB free of 16.4 TB
> 🧪 lab-north · today at 21:04     [🔄 Refresh]

**Webhook event** posted to `MEDIA_CHANNEL_ID`:

> **Sonarr: ⬇️ Grabbed**  *(poster thumbnail on the right)*
> **The Expanse (2015) – S06E01**
> Strange Dogs
> Quality `WEBDL-1080p` · Group `NTb` · Indexer `NZBgeek` · Client `SABnzbd` · Size `2.0 GB`

**Stalled download alert** in `ALERT_CHANNEL_ID`:

> 🟡 **sonarr: download stalled**
> **The Expanse S06E02** has not progressed in 30 min.
> `██████░░░░░░  48.3%` 1.1 GB left
> Client `qBittorrent` · Indexer `TorrentLeech` · Queue id `1834`

## Slash commands

All commands live under one group so they don't collide with other lab bots.

| Command | What it does |
|---|---|
| `/arr queue [app]` | Active downloads across Sonarr/Radarr/Lidarr (or one app): progress bar, done/total size, status, ETA and the `#queue_id`. Paginated, 6 per page. |
| `/arr remove <app> <queue_id> [blocklist]` | **Admin.** Removes a queue item (`removeFromClient=true`) after a Confirm/Cancel prompt, optionally blocklisting the release, then triggers `RefreshMonitoredDownloads`. Ephemeral. |
| `/arr calendar [days=7]` | Upcoming episodes (📺), movie releases (🎬 cinema/digital/physical) and albums (🎵) grouped by day, 1–30 days ahead. ✅ marks items already on disk. |
| `/arr search <app> <term>` | Read-only lookup: top 5 matches with year, tvdb/tmdb/imdb/MusicBrainz ids, status, network, "📚 in library", a short overview and the poster of the first hit. Nothing is ever added. |
| `/arr health` | Every configured app's health messages, plus Prowlarr indexers currently disabled for failures (name, disabled-till, last error). |
| `/arr clients` | qBittorrent (dl/ul speed, active torrents, session totals, connection status) and SABnzbd (speed, slots, MB left, paused, disk free). |
| `/arr nowplaying` | Plex + Jellyfin sessions: user, `Show – SxxEyy Title` or `Movie (year)`, player, progress bar, ⚡ direct / 🔁 transcode, ▶️/⏸️. |

## Environment variables

Copy `.env.example` to `.env`. Core variables come from `periscope`; integration variables are below. **A service is enabled iff its URL is set.** If a URL is set but its key/token is missing, or if no service at all is configured, the bot exits immediately with a clear message.

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | — | **Required.** Bot token. |
| `LAB_NAME` | `lab` | Footer on every embed; use one per lab. |
| `LAB_COLOR` | `5865F2` | Hex color for INFO embeds. |
| `GUILD_ID` | — | Server id for instant slash-command sync (otherwise global, up to 1 h). |
| `ALERT_CHANNEL_ID` | — | Health, stalled and unreachable alerts. |
| `STATUS_CHANNEL_ID` | — | Where the status board is pinned. Leave blank to disable the board. |
| `ALERT_ROLE_ID` | — | Role mentioned on CRITICAL alerts. |
| `ADMIN_ROLE_IDS` | — | Comma-separated roles allowed to `/arr remove` (default: Discord administrators). |
| `DATA_DIR` | `data` | Persistent state (message ids so restarts don't orphan alerts/boards). |
| `LOG_LEVEL` | `INFO` | |
| `STATUS_INTERVAL_S` | `60` | Status board refresh interval. |
| `WEBHOOK_HOST` / `WEBHOOK_PORT` | `0.0.0.0` / `8082` | Inbound webhook + `/health` listener. |
| `WEBHOOK_SECRET` | — | Shared secret; *arr apps must send `?token=<secret>`. Strongly recommended. |
| `SONARR_URL` / `SONARR_API_KEY` | — | Sonarr v3/v4 (API v3). |
| `RADARR_URL` / `RADARR_API_KEY` | — | Radarr v3+ (API v3). |
| `LIDARR_URL` / `LIDARR_API_KEY` | — | Lidarr (API v1). |
| `PROWLARR_URL` / `PROWLARR_API_KEY` | — | Prowlarr (API v1): health + indexer status. |
| `QBIT_URL` / `QBIT_API_KEY` | — | qBittorrent Web API v2. API key = Options → Web UI → API keys (qBittorrent ≥ 5.2, sent as `Authorization: Bearer`). |
| `QBIT_USER` / `QBIT_PASS` | — | Older qBittorrent: cookie login instead of an API key. |
| `SABNZBD_URL` / `SABNZBD_API_KEY` | — | SABnzbd JSON API. |
| `PLEX_URL` / `PLEX_TOKEN` | — | Plex Media Server + X-Plex-Token. |
| `JELLYFIN_URL` / `JELLYFIN_API_KEY` | — | Jellyfin + API key. |
| `VERIFY_SSL` | `true` | `false` to accept self-signed certificates on any service. |
| `MEDIA_CHANNEL_ID` | `ALERT_CHANNEL_ID` | Where webhook events (Grab/Import/…) are posted. |
| `ARR_QUEUE_STALL_MIN` | `30` | Minutes without progress before a queue item is reported as stalled. |

URLs may omit the scheme (`sonarr:8989` → `http://sonarr:8989`). Use Docker service names if the bot shares a network with the stack.

## Setup (about 10 minutes)

### 1. Create the Discord application

1. Open <https://discord.com/developers/applications> → **New Application** → name it (e.g. `lab-arr`).
2. **Bot** tab → **Reset Token** → copy it into `DISCORD_TOKEN`. No privileged intents are needed.
3. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; bot permissions **Send Messages, Embed Links, Attach Files, Read Message History, Manage Messages** (pinning the board). Open the generated URL and add the bot to your server.
4. Enable *Developer Mode* in Discord (User Settings → Advanced), then right-click → *Copy ID* on your server (`GUILD_ID`), the alert channel, the status channel, the media channel, and optionally the on-call role and admin roles.

### 2. Collect service credentials

| Service | Where |
|---|---|
| Sonarr / Radarr / Lidarr / Prowlarr | *Settings → General → Security → API Key* |
| qBittorrent | *Tools → Options → Web UI → API keys → Add* (≥ 5.2) → `QBIT_API_KEY`. Older versions: user/password, or tick "Bypass authentication for clients in whitelisted IP subnets" for the bot's subnet and leave both blank. |
| SABnzbd | *Config → General → Security → API Key* (the full key, not the NZB key) |
| Plex | Open any item in Plex Web → ⋯ → *Get Info* → *View XML*; the `X-Plex-Token=` value at the end of that URL is your token. |
| Jellyfin | *Dashboard → Advanced → API Keys → +* |

### 3. Run it

From the periscope checkout (see the [pack README](../../README.md) for install):

```bash
periscope init arr          # creates bots/arr/.env
nano bots/arr/.env          # paste the values from steps 1–2
periscope enable arr
periscope logs arr          # look for "ready as ... (lab=my-lab)" and "synced N app commands"
```

Docker instead: `docker compose up -d arr` from the repo root uses the same `bots/arr/.env`.

## Alert fingerprints

Alerts are deduplicated on stable fingerprints, so a restart of the bot never double-posts and each one is resolved in place:

| Fingerprint | Severity | Fires when | Resolves when |
|---|---|---|---|
| `arr:<app>:health:<type>:<hash>` | WARNING (CRITICAL for `level=error`) | `HealthIssue` webhook | matching `HealthRestored` webhook |
| `arr:<app>:stalled:<queue_id>` | WARNING | no `sizeleft` change for `ARR_QUEUE_STALL_MIN` while `downloading` | item progresses, pauses or leaves the queue |
| `arr:<service>:unreachable` | CRITICAL | 3 consecutive API failures | next successful call |

## Development

```bash
python -m venv venv && . venv/bin/activate
pip install -e core -e bots/arr pytest pytest-asyncio
pytest bots/arr
```

Conventions: [docs/CONTRIBUTING.md](../../docs/CONTRIBUTING.md).

## License

MIT
