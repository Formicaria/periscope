# THE LAB — proposed server layout

Current server (Sept 2026): `Events`, `dev-ops` (op-homelab, op-desktank, op-cb, op-arkitekt, op-dewbot, op-cd, op-hexapod, op-codebase), `Voice Channels` (general, vox-1, vox-2), `Formicaria` (formicaria, op-anthill, op-microround, op-sovrgn, op-pherosphere).

Nothing below removes an existing channel. It adds a bot-facing layer and gives Formicaria its own git feed.

## Categories & channels

### 🧪 LAB STATUS  *(bots post, humans read — slowmode 0, bots need Manage Messages to pin)*
| Channel | Purpose | Env var |
|---|---|---|
| `#lab-status` | One pinned live board per bot per lab (edited in place every 60 s) | `STATUS_CHANNEL_ID` |
| `#lab-alerts` | Firing/resolved alerts from every bot. Criticals ping `@lab-oncall` | `ALERT_CHANNEL_ID`, `ALERT_ROLE_ID` |
| `#media` | Grabs, downloads, now-playing from the *arr bots | `MEDIA_CHANNEL_ID` |
| `#network` | New client joined, device offline, firmware available (UniFi) | can reuse `ALERT_CHANNEL_ID` or set per-bot |
| `#backups` | Proxmox vzdump summaries (optional; else goes to `#lab-alerts`) | — |

### 🕹️ LAB CONTROL  *(where people run slash commands so `#lab-status` stays clean)*
| Channel | Purpose |
|---|---|
| `#lab-cmd` | `/pve`, `/prom`, `/arr`, `/unifi` commands. Restrict slash commands to this channel in *Server Settings → Integrations → <bot> → Channels* |

### dev-ops *(existing — unchanged)*

### 🐜 Formicaria *(existing, plus:)*
| Channel | Purpose | Env var |
|---|---|---|
| `#formicaria-git` | Every push / PR / issue / release / CI run in the org | `GITHUB_FEED_CHANNEL_ID` |
| `#formicaria-ci` | Workflow failures on default branches, pings `@formicaria-dev` | route via `GITHUB_REPO_CHANNEL_MAP` or leave in `#formicaria-git` |
| existing `#op-anthill`, `#op-sovrgn`, … | Per-repo activity can be routed here with `GITHUB_REPO_CHANNEL_MAP=anthill=<id>,sovrgn=<id>` | — |

### Voice Channels *(existing — unchanged)*

## Roles
| Role | Who | Why |
|---|---|---|
| `@lab-admin` | People allowed to start/stop VMs, silence alerts, kick clients, restart APs | `ADMIN_ROLE_IDS` |
| `@lab-oncall` | Mentionable; pinged on CRITICAL | `ALERT_ROLE_ID` |
| `@formicaria-dev` | Mentionable; pinged on CI failure | `GITHUB_CI_FAILURE_ROLE_ID` |
| `@bots` | Assign to every bot user; grant it Send Messages / Embed Links / Attach Files / Manage Messages in the LAB STATUS category, deny Send in `#lab-cmd` (slash replies still work) | — |

## Why this shape
- One status channel with pinned live boards means a phone glance shows every lab. Boards are edited, not re-posted, so there's no scroll.
- Alerts have their own channel with resolve-in-place, so `#lab-alerts` reads as a current incident list rather than a log.
- Commands in their own channel keep dashboards readable.
- Formicaria gets a git feed inside its existing category instead of a new server or a generic `#github`.

## Apply it
```bash
cd /opt/periscope
DISCORD_TOKEN=<any bot token with Manage Channels + Manage Roles> GUILD_ID=1439743165845344468 \
  ./venv/bin/python scripts/setup_server.py --dry-run     # shows what it would create
./venv/bin/python scripts/setup_server.py                 # creates missing categories/channels/roles, prints ids for .env
```
