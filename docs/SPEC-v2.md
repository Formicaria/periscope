# periscope v2 — one service, many integrations, a web UI

Status: implemented (core runtime, presences, YAML store, service contract, arr split, plexrequests, web UI).
Roadmap services in §2 are not built yet. v1 `.env` installs migrate in place via `periscope update`.

## 1. Shape

```
periscope (one systemd service, one process)
├── core            config store · Discord gateway(s) · embeds/boards/alerts · webhook server · scheduler
├── web             admin UI + API on :8090 (Discord OAuth, lab-admin only)
└── services/       one module per integration, each independently enable-able
    proxmox  prometheus  alertmanager  grafana
    sonarr  radarr  lidarr  prowlarr  qbittorrent  sabnzbd
    plex  jellyfin  overseerr  (plex-requests = displexia, renamed)
    unifi  github
    docker  uptime-kuma  truenas  pihole  nut  tautulli  homeassistant   (roadmap)
```

- **One process.** Services are asyncio tasks in one loop; a crashing service is restarted by a supervisor
  inside the process, the others never notice. `periscope logs [service]` filters one service's log lines.
- **One or many Discord identities.** Each service posts through a *presence* — a Discord bot token. The
  default is one shared presence ("periscope"); any service can be given its own token (e.g. keep the
  existing Proxmox / Arr / Unifi / Github apps) so its posts carry that name and avatar. Presences are
  multiplexed gateway clients in the same process.
- **One config store.** `config/periscope.yaml` (mode 0600) replaces the per-bot `.env` files. The web UI and
  the CLI both read/write it; the wizard becomes the web UI's first-run flow. A migration step imports every
  existing `bots/*/.env` on first start.
- **Many Discord servers.** `servers:` in that file holds one entry per Discord server (name, colour, id,
  status/alert channels, admin roles); every service names the server it posts in, and a bot serves every server
  its services use, registering its slash commands in each. Settings that are not per-server (log level, board
  refresh) live in their own block.
- **Every post is a template.** Each service registers its posts as *message kinds* with sample data
  (`core/src/periscope/messages.py`); the Messages page previews them as Discord draws them and lets a user
  reword, recolour, restructure or switch off any of them. Customisations live in `config/messages.yaml` and
  apply to the next post — no restart, no code change. A broken template never blocks a post: the bot's own
  version goes out and the error is logged.
- **Channels by convention** (unchanged): `#lab-status` boards, `#lab-alerts`, `#media`, `#network`,
  `#git-<project>` per-repo feeds + CI trains, `#op-*` humans only. `periscope layout` applies permissions.

## 2. Service catalog

Every service implements the same contract:

| hook | what it does |
|---|---|
| `check()` | verify URL/credentials — used by the web UI "Test" button and the first-run flow |
| `board()` | a pinned live embed refreshed every N s in `#lab-status` (optional) |
| `poll()` | scheduled fetch → alerts via the shared router (dedupe · cooldown · resolve-in-place · @role on critical) |
| `webhook(path)` | optional inbound route on the shared webhook server, secret-checked |
| `commands` | one slash group per service (`/pve`, `/sonarr`, …), admin-only mutations behind Confirm buttons |
| `settings` | typed fields (url, secret, choice, int, channel, role) — rendered by the web UI, validated once |

Per-service spec. **Poll** = default interval, **Port** = inbound listener only where needed.

### Infrastructure

| service | auth | board | alerts | commands | poll / port |
|---|---|---|---|---|---|
| **proxmox** | API token (`user@pve!name`) | nodes CPU/MEM/uptime, guests running/total, storage bars | node offline, CPU/MEM over threshold (3 polls), storage warn/crit, backup task failed, guest crashed | `/pve status · vm list · start · stop · shutdown · reboot · backups` | 60 s |
| **prometheus** | none / basic | targets up/down, active alerts count | target down, Prometheus unreachable | `/prom query · targets · alerts` | 60 s |
| **alertmanager** | none / basic | — (posts into `#lab-alerts`) | every Alertmanager alert, resolve in place, **Silence 1h/24h** buttons | `/am silences · silence <fp>` | webhook `/alertmanager` :8081 |
| **grafana** | service-account token | — | — | `/grafana panel <dashboard> <panel>` → PNG screenshot | on demand |
| **unifi** | local admin (read-only) | WAN ip/latency/ISP, LAN/WLAN, clients wired/wireless/guest, devices cpu/temp/clients | new client, device offline, WAN latency, device cpu | `/unifi clients · devices · kick · block · restart-ap` | 60 s |
| **docker** *(roadmap)* | socket or Portainer token | containers up/exited, images with updates | container exited/unhealthy, restart loop | `/docker ps · restart · logs` | 60 s |
| **truenas** *(roadmap)* | API key | pools health/usage, scrub status | pool degraded, SMART failure, scrub errors | `/nas pools · disks` | 300 s |
| **uptime-kuma** *(roadmap)* | webhook | monitors up/down count | monitor down/up | `/uptime` | webhook `/uptime` |
| **pihole / adguard** *(roadmap)* | API token | queries/blocked %, top clients | blocking disabled, upstream failing | `/dns top · disable 5m` | 120 s |
| **nut** *(roadmap)* | upsd | UPS load/charge/runtime | on battery, low battery, back on mains | `/ups` | 30 s |

### Media — the *arr stack split into individual services*

Each is its own toggle, own credentials, own commands; they share the "Media stack" board so one glance still
shows the whole pipeline. Webhooks land on the shared server at `/<service>?token=…` (:8082).

| service | auth | on the shared board | alerts | commands | feed events (webhook) |
|---|---|---|---|---|---|
| **sonarr** | API key | queue depth · downloading · missing count · health | health issue/restored, stalled queue item, unreachable | `/sonarr search <title> · add · queue · calendar · missing · remove` | Grab, Download/Import, Upgrade, Rename, SeriesAdd/Delete, EpisodeFileDelete, Health, ManualInteraction |
| **radarr** | API key | same | same | `/radarr search · add · queue · calendar · missing · remove` | Grab, Download, Upgrade, Rename, MovieAdded/Delete, MovieFileDelete, Health, ManualInteraction |
| **lidarr** | API key | queue · missing | health, stalled, unreachable | `/lidarr search · add · queue` | Grab, Download, Upgrade, Retag, ArtistAdd/Delete, Health |
| **prowlarr** | API key | indexers ok/failing count | indexer failing > 6 h, health, unreachable | `/prowlarr indexers · test · search` | Grab, Health, ApplicationUpdate |
| **qbittorrent** | API key (≥5.2) or user/pass | active/seeding/paused, ↓↑ speed, free space | client unreachable, disk almost full, stalled torrents | `/qbit list · pause · resume · delete · speed-limit` | — (polled) |
| **sabnzbd** | API key | queue size, speed, ETA | unreachable, disk full, paused > N min | `/sab queue · pause · resume` | — (polled) |
| **plex** | X-Plex-Token | now playing (user · title · transcode), library counts | server unreachable | `/plex playing · recent · libraries` | — (polled) |
| **plex-requests** *(was displexia)* | Plex token + Sonarr/Radarr or Overseerr | requests board in `#plex-status` (streams / queue / disk) | request stuck (no grab after N h) | `/plexinvite email · request title · mystatus`, **Get Plex Access** + **Search & Request** buttons, `#new-on-plex` feed, auto-revoke on leave, per-year fallback profile | — (polled) |
| **jellyfin** | API key | now playing, library counts | unreachable | `/jellyfin playing · recent` | — |
| **overseerr / jellyseerr** | API key | pending requests count | — | request backend for plex-requests; `/requests pending · approve · decline` | webhook `/seerr` |
| **tautulli** *(roadmap)* | API key | watch stats | — | `/tautulli history · stats` | webhook `/tautulli` |

Quality-profile logic from displexia moves into a shared `arr` helper used by sonarr/radarr/plex-requests:
profile by name, root folder by name, **fallback profile for titles older than `FALLBACK_BEFORE_YEAR`** (2016)
when the main profile is 4K-only.

### Dev

| service | auth | board | alerts | commands | feed |
|---|---|---|---|---|---|
| **github** | fine-grained PAT (+ optional org webhook) | org overview (repos, open PRs/issues, last events) | CI failing on default branch (resolve on green), API unreachable | `/gh repos · prs · issues · runs · watch` | every event per repo → `#git-<project>`; **CI trains** edited live per job; bots + every action shown by default |
| **gitea / gitlab** *(roadmap)* | token | same | same | same | same, if anyone self-hosts one |

## 3. Web UI

Runs inside the same process at `http://<box>:8090` (put behind your reverse proxy for TLS). Login = **Discord
OAuth**, allowed = members holding `@lab-admin` in the configured guild, so there is no separate password to manage.

Pages:

1. **Overview** — a "needs attention" list (plain-language problem + link to the page that fixes it), then every service as a card: state (`running · starting · needs setup · error · off`) · the bot it posts as. Switch on/off + Test per card; one Restart (header) once settings changed.
2. **Service settings** — the typed `settings` of that service rendered as a form (secrets masked, "Test" runs `check()` live, channel/role pickers pull the guild's channels/roles by name). Save = validate → write config → hot-reload that service only.
3. **Bots** — presences in the config: tokens (checked against Discord), invite links, which services post as which, why one is offline. **Discord** — lab settings, web sign-in, channel convention with a "create missing" button, `periscope layout` as a button.
4. **Feeds & routing** — the GitHub repo→channel map as a table (repo · channel · CI channel · mirror), alert routing (severity → channel, role to ping).
5. **Logs** — live tail per service (SSE), download.
5b. **Messages** — every kind of post the bots make, previewed the way Discord draws it, editable as a form
   (Simple) or as the raw template (Code), with a live preview, reset, switch-off and a test post.
6. **First run** — `periscope web` prints a one-time sign-in link; then: paste Discord token → invite link → pick server → create channels → add services one by one, each with Test.

### Stack options

| option | stack | build step | look & feel | effort | notes |
|---|---|---|---|---|---|
| **A — FastAPI + HTMX + Tailwind/daisyUI** *(recommended)* | Python (same process), Jinja templates, HTMX for partial updates, Alpine.js for tiny interactions, Tailwind via CDN | none — ships in the repo, no Node | clean, dark-first, dashboard-like; daisyUI components look modern out of the box | ~2 days | Zero extra runtime, one venv, one port. Live log tail via SSE. Easiest for other members to hack on. |
| B — FastAPI API + SvelteKit SPA | Python API, Svelte + Tailwind + shadcn-svelte front end, prebuilt static bundle committed | Node at dev time only (bundle committed) | the slickest option: real app feel, animations, optimistic updates | ~4 days | Two codebases; contributors need Node for UI changes. CI builds the bundle. |
| C — Streamlit / NiceGUI | Python only | none | functional, but reads as "data app", limited polish | ~1 day | Fast to ship, hard to make sleek; heavier runtime (websocket per session). |
| D — Homarr / Homepage integration | config through their widget/API systems | — | matches existing dashboards | n/a | They can *display* periscope status, but neither can edit our config — not a config UI. |

Recommendation: **A**. It gives the "simple, modern, streamlined" target without adding a Node toolchain to a
Python project, and every member can run it from the same `periscope` install. If the SPA polish matters more
later, B can replace the templates without touching the API.

## 4. Migration (v1 → v2), all via `periscope update`

1. New process boots, finds `bots/*/.env`, imports them into `config/` (tokens → secrets.env), one presence per
   existing bot so nothing changes in Discord. Old units are disabled and removed.
2. `periscope` CLI keeps the same verbs (`list / enable / disable / logs / restart / update / layout`) plus
   `web` (open/print the UI URL) and `presence add <name>`.
3. displexia: its `/opt/displexia` state (`state.json`, stats) is imported by the `plex-requests` service; its
   systemd unit is disabled; the invite/request embeds re-post themselves under the new presence. The name
   disappears from repos, CLI and docs.
4. Docker path: one image `ghcr.io/formicaria/periscope`, one container, config volume.

## 5. Order of work

1. core: config store + service contract + supervisor + presences (v1 services wrapped as-is)
2. split arr into sonarr/radarr/lidarr/prowlarr/qbittorrent/sabnzbd/plex/jellyfin/overseerr services
3. plex-requests service = displexia merged (invites, requests, status board, new-on-plex, auto-revoke, stats)
4. web UI (option A unless told otherwise) with first-run flow, then retire the terminal wizard
5. migration + docs + `periscope update` path; roadmap services after
