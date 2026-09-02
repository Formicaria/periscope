# periscope · prometheus

Discord bot that bridges **Prometheus**, **Alertmanager** and **Grafana** into your server.
Part of the [periscope](../../README.md) pack, built on its shared core; one instance per lab, several labs can share a channel.

## What it does

- **Alertmanager → Discord.** Alertmanager posts to the bot's webhook; every alert becomes a coloured embed
  (red critical / yellow warning / blue info) in `ALERT_CHANNEL_ID`. When the alert resolves the *same message*
  is edited green. CRITICAL alerts ping `ALERT_ROLE_ID`.
- **Silence from Discord.** `/prom alerts` pages through firing alerts; each page has **Silence 1h** / **Silence 24h**
  buttons (admin only, with a confirm step) that create an Alertmanager silence matching all of the alert's labels.
- **PromQL from chat.** `/prom query up == 0` returns an aligned table of series → value.
- **Scrape target watch.** A loop polls `api/v1/targets` and fires a WARNING alert (`prom:target:<job>:<instance>:down`)
  when a target stops being `up`, resolving it automatically when it recovers.
- **Grafana panels as images.** `/prom panel <dashboard> <panel_id> [range]` renders a panel PNG and posts it inline
  with a link back to the dashboard. Dashboard names autocomplete.
- **Live status board.** A pinned message in `STATUS_CHANNEL_ID`, refreshed every `STATUS_INTERVAL_S`:

  > 🟢 **Monitoring status** · Prometheus 🟢 up · Alertmanager 🟢 up · Grafana 🟢 up
  > Firing alerts: 🔴 0 critical / 🟡 2 warning / 🔵 0 info · Scrape targets: 🟢 41 up / 🔴 1 down · 12 jobs · Active silences: 🔕 1
  > Down targets: node: `nas:9100` · Links: Prometheus · Alertmanager · Grafana · Dashboard · [🔄 Refresh]

- **Self-monitoring.** If Prometheus, Alertmanager or Grafana fails 3 consecutive checks the bot fires a CRITICAL
  `<service> unreachable` alert and resolves it when the service is back.

## Slash commands

All commands live under one group, `/prom`.

| Command | What it does | Who |
|---|---|---|
| `/prom alerts` | Firing alerts from Alertmanager, one page per alert, with Silence 1h / 24h buttons | everyone (buttons: admin) |
| `/prom silences` | Active + pending silences with id, matchers, expiry, author, comment | everyone |
| `/prom unsilence <silence_id>` | Expire a silence (confirm button) | admin |
| `/prom query <expr>` | Instant PromQL query, up to 20 rows in a code block | everyone |
| `/prom targets` | Scrape target health grouped by job; lists the down instances with their last error | everyone |
| `/prom dashboards [search]` | Grafana dashboards (title, uid, link) | everyone |
| `/prom panel <dashboard> <panel_id> [range]` | Render a panel PNG (default range `6h`; accepts `30m`, `2d`, `1w`, …) | everyone |
| `/prom grafana` | Grafana health (`api/health`), version, dashboard count | everyone |
| `/prom status` | The status board embed, on demand | everyone |

"admin" = a member holding one of `ADMIN_ROLE_IDS` (or a server administrator if that variable is empty).

## Environment variables

Copy `.env.example` to `.env`. Secrets are never logged.

### Shared (from the shared core)

| Var | Required | Default | Meaning |
|---|---|---|---|
| `DISCORD_TOKEN` | yes | | Bot token |
| `LAB_NAME` | | `lab` | Footer on every embed; identifies this instance when several labs share a channel |
| `LAB_COLOR` | | `5865F2` | Accent colour (hex, no `#`) |
| `GUILD_ID` | | | Sync slash commands to this guild instantly (otherwise global, up to 1 h) |
| `ALERT_CHANNEL_ID` | for alerts | | Channel receiving alerts |
| `STATUS_CHANNEL_ID` | for the board | | Channel holding the pinned status board |
| `ALERT_ROLE_ID` | | | Role pinged on CRITICAL |
| `ADMIN_ROLE_IDS` | | | Comma-separated role ids allowed to silence/unsilence |
| `DATA_DIR` | | `data` | Persistent JSON state (alert message ids) |
| `LOG_LEVEL` | | `INFO` | |
| `STATUS_INTERVAL_S` | | `60` | Board refresh and target-watch interval |
| `WEBHOOK_HOST` / `WEBHOOK_PORT` | | `0.0.0.0` / `8081` | Webhook + `/health` listener |
| `WEBHOOK_SECRET` | strongly recommended | | Shared secret Alertmanager must present (see below) |

### This bot

| Var | Required | Default | Meaning |
|---|---|---|---|
| `PROM_URL` | yes | | Prometheus base URL, e.g. `http://prometheus:9090` |
| `ALERTMANAGER_URL` | yes | | Alertmanager base URL, e.g. `http://alertmanager:9093` |
| `PROM_BASIC_USER` / `PROM_BASIC_PASS` | | | HTTP basic auth for Prometheus **and** Alertmanager (set both or neither) |
| `VERIFY_SSL` | | `true` | `false` to accept self-signed certificates |
| `PROM_TARGET_WATCH` | | `true` | Alert when a scrape target goes down |
| `GRAFANA_URL` | | | Grafana base URL; leave empty to disable the Grafana commands |
| `GRAFANA_TOKEN` | if `GRAFANA_URL` | | Service account token (Viewer role is enough) |
| `GRAFANA_ORG_ID` | | `1` | Org id used for rendering |
| `GRAFANA_RENDER_WIDTH` / `GRAFANA_RENDER_HEIGHT` | | `1000` / `500` | Panel PNG size |
| `GRAFANA_DEFAULT_DASHBOARD_UID` | | | Dashboard linked from the status board |

The bot fails fast at startup with a clear message when a required variable is missing or malformed.

## Setup (about 10 minutes)

### 1. Discord application

1. <https://discord.com/developers/applications> → **New Application** → name it (e.g. `lab-prometheus`).
2. **Bot** tab → **Reset Token** → copy it into `DISCORD_TOKEN`. No privileged intents are needed.
3. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; bot permissions
   *Send Messages, Embed Links, Attach Files, Read Message History, Manage Messages* (to pin the board).
   Open the generated URL and invite the bot to your server.
4. Enable *Developer Mode* in Discord (User Settings → Advanced), then right-click → *Copy ID* for the server
   (`GUILD_ID`), the alert channel, the status channel and the admin role.

### 2. Grafana service account token (optional)

Grafana → **Administration → Service accounts → Add service account** → role **Viewer** → **Add token** → copy into `GRAFANA_TOKEN`.

`/prom panel` uses Grafana's `render/d-solo/...` endpoint, which needs the
[grafana-image-renderer](https://grafana.com/grafana/plugins/grafana-image-renderer/) plugin. Either install it
(`GF_INSTALL_PLUGINS=grafana-image-renderer`) or run the renderer as a sidecar:

```yaml
  renderer:
    image: grafana/grafana-image-renderer:latest
  grafana:
    environment:
      GF_RENDERING_SERVER_URL: http://renderer:8081/render
      GF_RENDERING_CALLBACK_URL: http://grafana:3000/
```

Without it, `/prom panel` replies with a clear "renderer missing" error; everything else works.

### 3. Run the bot

From the periscope checkout (see the [pack README](../../README.md) for install):

```bash
periscope init prometheus
nano bots/prometheus/.env       # tokens/ids/urls
periscope enable prometheus
periscope logs prometheus       # look for "ready as ..." and "webhook server listening"
curl localhost:8081/health      # {"ok": true} once connected to Discord
```

Docker instead: `docker compose up -d prometheus` from the repo root uses the same `bots/prometheus/.env`. If Prometheus/Alertmanager/Grafana run in another compose project, put the bot on the same Docker network or use LAN addresses in the URLs.

### 4. Point Alertmanager at the bot

Add a receiver to `alertmanager.yml`. The simplest authenticated path is the `?token=` query parameter
(`WebhookServer` also accepts an `X-Webhook-Secret` header, but Alertmanager's `http_config` cannot set custom
headers on older versions, so the query token is the documented route):

```yaml
route:
  receiver: discord-lab
  group_by: [alertname, job]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - receiver: discord-lab
      matchers: [severity=~"critical|warning|info"]

receivers:
  - name: discord-lab
    webhook_configs:
      - url: http://<periscope-host>:8081/alertmanager?token=YOUR_WEBHOOK_SECRET
        send_resolved: true
        max_alerts: 0
```

Alternatively (Alertmanager ≥ 0.26 supports `http_config.http_headers`):

```yaml
      - url: http://<periscope-host>:8081/alertmanager
        send_resolved: true
        http_config:
          http_headers:
            X-Webhook-Secret:
              values: ["YOUR_WEBHOOK_SECRET"]
```

Reload Alertmanager (`curl -X POST http://alertmanager:9093/-/reload`) and test it:

```bash
curl -s -X POST "http://localhost:8081/alertmanager?token=YOUR_WEBHOOK_SECRET" \
  -H 'Content-Type: application/json' -d '{
  "version": "4", "receiver": "discord-lab", "status": "firing",
  "alerts": [{"status": "firing", "fingerprint": "demo1",
    "labels": {"alertname": "DemoAlert", "severity": "warning", "instance": "nas:9100", "job": "node"},
    "annotations": {"summary": "This is a test", "description": "Fired from curl"},
    "generatorURL": "http://prometheus:9090/graph"}]}'
# → {"ok": true, "fired": 1, "resolved": 0}; re-send with "status": "resolved" to turn it green.
```

The bot maps each alert as: severity `labels.severity` (`critical` → CRITICAL, `warning` → WARNING, anything else →
INFO), title `labels.alertname`, description `annotations.summary` + `annotations.description`, fields `instance`,
`job` then the other labels (max 8), link `generatorURL`, and fingerprint `am:<alertmanager fingerprint>` so
resolved notifications edit the original message even across bot restarts.

## Development

```bash
python -m venv venv && . venv/bin/activate
pip install -e core -e bots/prometheus pytest pytest-asyncio
pytest bots/prometheus
```

Conventions: [docs/CONTRIBUTING.md](../../docs/CONTRIBUTING.md).

## License

MIT
