# periscope · plexrequests

**The Discord front door for your Plex server.** Automatic library invites and movie/TV requests for any stack:
route requests through Seerr ([Overseerr](https://overseerr.dev) / [Jellyseerr](https://github.com/fallenbagel/jellyseerr))
**or** talk natively to [Radarr](https://radarr.video) + [Sonarr](https://sonarr.tv) — no Seerr required. Plus a live
status board, a new-on-Plex feed, auto-revoke when members leave, and per-user usage stats.

Part of the [periscope](../../README.md) pack. It is a v2 *service* (`plexrequests`) hosted by the periscope runtime,
so it can share a Discord identity with the other services or keep its own — and it can live in a **different Discord
server than the lab** (`PLEXREQ_GUILD_ID`), which is where the invite and request channels usually are.

## What it does

**Invite channel (`CHANNEL_ID`, by default `#join-plex`) — Plex access**

- 🎟️ **Get Plex Access** button → private modal asks for the member's Plex email
- ⌨️ Typing an email in the channel works too — the message is deleted immediately for privacy, the result arrives by DM
- 🔍 `/plexinvite email:...` — private reply
- Success = Plex invite email sent (or the existing share refreshed) + the `ROLE_NAME` role assigned (created if missing)
- Shares the libraries in `LIBRARIES` (`all` = everything); pending invites and existing friends are detected

**Requests channel (`REQUESTS_CHANNEL_ID`) — movies & TV, gated by `REQUESTS_ROLE_NAME`**

- 🔎 **Search & Request** button, ⌨️ typing a title in the channel, or 🎯 `/requests request title:...`
- Fully private flow: typed titles are deleted, result menus are ephemeral or self-destruct — nothing shows up
  publicly until a request is actually sent
- Announcements are rich cards (poster, title, year, description) routed per type to `MOVIES_CHANNEL` / `TV_CHANNEL`
  (channel name or id), else the requests channel
- Knows what is already on Plex or already queued, and says so instead of double-requesting
- When the download lands the card turns **green** — *"Requested by X • Available to watch on plex.yourdomain.com now"*
  — and the requester gets a 🎉 ping (polled every 5 minutes, survives restarts, watches expire after 30 days)
- `/requests mystatus` shows everyone their own last 15 requests, privately
- **Pluggable backend** (`REQUEST_BACKEND`): `seerr` goes through Overseerr/Jellyseerr; `arr` searches and adds straight
  to Radarr/Sonarr — quality profile and root folder by *name*, search kicked off immediately; `auto` prefers Seerr
  when configured
- **Fallback profile for old titles** (`arr` backend): anything released before `FALLBACK_BEFORE_YEAR` (default 2016)
  is added with `RADARR_FALLBACK_PROFILE` / `SONARR_FALLBACK_PROFILE` — auto-detected as your 1080p profile when the
  main profile is 4K-only — because old films and shows rarely exist in 4K and a 4K-only profile would never grab anything
- The button embeds re-post themselves so they always sit at the bottom of their channel

**Server extras (optional channels, name or id; empty = off)**

- 📊 **Live status board** (`STATUS_CHANNEL`): one embed edited every 60 s — who is streaming what on Plex,
  Radarr/Sonarr queue depth with ETAs, disk space
- 🆕 **New-on-Plex feed** (`NEW_CHANNEL`): announces new arrivals (max 5 per pass, oldest first); baselines silently
  on first run so it never spams history
- 🔐 **Auto-revoke** (`AUTO_REVOKE=1`): leaving the server or losing `ROLE_NAME` removes the Plex share (and cancels
  a pending invite) for the email that member used to get in

**Ops**

- 📈 Every interaction is counted per user — `/requests plexstats` (admin) prints the report
- 🛡️ Rate-limited (3 invite attempts / 5 searches per 10 minutes), server admins bypass the request role gate,
  everything logged, secrets never logged
- The **Test** button in the web UI (and `check()`) verifies the Plex token against `/identity` + `/status/sessions`
  and, when configured, Seerr, Radarr and Sonarr

## Slash commands

| Command | Who | What it does |
|---|---|---|
| `/plexinvite email:` | everyone | Invite that Plex account to the server; private reply |
| `/requests request title:` | `REQUESTS_ROLE_NAME` (admins always) | Search the backend and pick from a private menu; the request is announced as a card |
| `/requests mystatus` | everyone | Your recent requests and whether they are queued or available |
| `/requests plexstats` | admins | Usage report: buttons, searches, picks, requests, invites — totals and per user |

Commands are registered in the Plex server (`PLEXREQ_GUILD_ID`). When that is the lab server too, the presence syncs
them like every other service's. "Admin" for `/requests plexstats` means a server administrator of the Plex server,
or a member of the lab's `ADMIN_ROLE_IDS`.

## Settings

All keys are the same as the standalone bot this service replaces, so an existing `.env` is imported as is. Channel
settings that take a *name* are given without `#`.

| Key | Required | Default | What |
|---|---|---|---|
| `PLEXREQ_GUILD_ID` | | lab `GUILD_ID` | Discord server the invite/request channels live in — set it when the Plex server is not the lab server |
| `CHANNEL_ID` | **yes** | | Invite channel id (`#join-plex`) |
| `CHANNEL_NAME` | | `join-plex` | Invite channel name, shown in messages (and used to find the channel when `CHANNEL_ID` is empty) |
| `REQUESTS_CHANNEL_ID` | | | Requests channel id; empty = no typed requests and no requests embed |
| `PLEX_URL` | **yes** | `http://YOUR_PLEX_IP:32400` | Plex server URL |
| `PLEX_TOKEN` | **yes** | | Account token of the server owner — see *Plex token* below |
| `LIBRARIES` | | `all` | Libraries shared with invitees: `all` or comma-separated names |
| `PLEX_LINK` | | | Shown on green "available" cards, e.g. `plex.yourdomain.com` (empty = "Plex") |
| `SERVER_NAME` | | | Branding in embeds and command text, e.g. `yourdomain.com` (empty = plain "Plex") |
| `REQUEST_BACKEND` | | `auto` | `auto` \| `seerr` \| `arr` |
| `OVERSEERR_URL` / `OVERSEERR_API_KEY` | | | Overseerr / Jellyseerr base URL + API key (Settings → General) |
| `RADARR_URL` / `RADARR_API_KEY` | | | Radarr base URL + API key (Settings → General → Security) |
| `RADARR_PROFILE` / `RADARR_ROOT` | | first | Quality profile *name* / root folder path to add movies with |
| `SONARR_URL` / `SONARR_API_KEY` | | | Sonarr base URL + API key |
| `SONARR_PROFILE` / `SONARR_ROOT` | | first | Quality profile *name* / root folder path to add series with |
| `FALLBACK_BEFORE_YEAR` | | `2016` | Titles released before this year use the fallback profile; `0` = off |
| `RADARR_FALLBACK_PROFILE` / `SONARR_FALLBACK_PROFILE` | | auto | Fallback profile *name* (auto = your 1080p profile, only when the main one is 4K-only) |
| `STATUS_CHANNEL` | | | Live status board channel (name or id) |
| `NEW_CHANNEL` | | | New-on-Plex feed channel (name or id) |
| `MOVIES_CHANNEL` / `TV_CHANNEL` | | requests channel | Where announcement cards go, per type (name or id) |
| `ROLE_NAME` | | `plex members` | Role granted after a successful invite (created if missing) |
| `REQUESTS_ROLE_NAME` | | `plex members` in the example, empty = anyone | Role required to request media; server admins always may |
| `AUTO_REVOKE` | | `0` | `1` = revoke the Plex share on leave / role loss |

In the v2 runtime these live under `services.plexrequests.env` in `config/periscope.yaml` (edit them in the web UI);
`DISCORD_TOKEN` comes from the presence the service is assigned to. Run standalone (`python -m periscope_plexrequests`,
the `periscope@plexrequests` unit or the Docker image), the same keys are read from `bots/plexrequests/.env` together
with `DISCORD_TOKEN` and `GUILD_ID`.

## Setup

### 1. Discord application

1. <https://discord.com/developers/applications> → **New Application** (e.g. `Plex`) → **Bot** → **Reset Token**.
2. **Bot → Privileged Gateway Intents**: enable **Message Content Intent** (typed emails and titles) and
   **Server Members Intent** (auto-revoke and role-loss detection). The service declares both intents; the presence
   that hosts it will not connect until they are enabled for that application.
3. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; permissions **View Channels**, **Send Messages**,
   **Embed Links**, **Read Message History**, **Manage Messages** (deletes typed emails/titles, re-posts the button
   embeds) and **Manage Roles**. Open the URL and add the bot to the *Plex* server.
4. **Server Settings → Roles**: drag the bot's role **above** `ROLE_NAME`, or it cannot assign it.
5. Enable *Developer Mode* in Discord, then right-click → **Copy ID** for `PLEXREQ_GUILD_ID`, `CHANNEL_ID`,
   `REQUESTS_CHANNEL_ID` (and `MOVIES_CHANNEL` / `TV_CHANNEL` / `STATUS_CHANNEL` / `NEW_CHANNEL` if you prefer ids
   over names).

### 2. Plex token

The service needs the account token of the Plex **server owner** (it sends invites on that account). Fetch one with the
official plex.tv/link flow:

```bash
./venv/bin/python bots/plexrequests/scripts/plex_token.py                 # prints PLEX_TOKEN=...
./venv/bin/python bots/plexrequests/scripts/plex_token.py --env bots/plexrequests/.env   # standalone: writes it into .env
```

It prints a 4-character code; open <https://plex.tv/link> signed in as the owner, enter the code, done. Paste the token
into the service's `PLEX_TOKEN` (web UI or `config/periscope.yaml`). `check()` — the **Test** button — tells you when
the token is missing or rejected and points at this script.

### 3. Channels and roles

Create the invite channel (`#join-plex`) and the requests channel; optionally `#movies`, `#tv`, `#plex-status`,
`#new-on-plex`. Members should be able to *send* messages in the invite and requests channels (the bot deletes what
they type). `ROLE_NAME` is created automatically on first start if it does not exist.

### 4. Enable the service

From a periscope v2 checkout: open the web UI (or edit `config/periscope.yaml`), enable **Plex requests**, pick the
presence (the shared one, or a dedicated `plex` presence with the token from step 1), fill in the settings, hit
**Test**, save. On first start the service posts its two button embeds and syncs the slash commands to the Plex server.

Coming from the standalone bot? The v1 migration (first start of the v2 runtime) imports its `.env` into
`services.plexrequests.env` on its own presence (so nothing changes in Discord — same bot user, same avatar), sets
`PLEXREQ_GUILD_ID` from its `GUILD_ID`, and the service imports the old `state.json`
(sticky embed and board message ids, invitee emails, availability watches, request history, new-on-Plex baseline) and
`stats.json` (counters) on its first build. The existing button embeds keep working (their old button ids are still
handled) and are refreshed in place; disable the old systemd unit once the new process is up.

## How it works

```
service.py        ServiceSpec: settings from .env.example, build() wiring, check(), import_legacy_state()
config.py         PlexRequestsSettings.from_env() — every key, validated once
context.py        what the cogs share: settings, Plex gateway, backend, records, stats, sticky embeds, the /requests group
plex.py           PlexGateway — plexapi (invite / revoke / sessions / recently added), run in a thread
seerr.py          Overseerr/Jellyseerr client (search, request with season fallback, media status)
arr.py            native Radarr/Sonarr client (lookup, add with profile/root by name + year fallback, availability, queue, disk)
backend.py        RequestBackend — auto/seerr/arr selection, interleaved arr search, watch status
records.py        typed accessors over the service state (message ids, emails, watches, history, baseline)
stats.py          usage counters + the text report behind /requests plexstats
sticky.py         ensure() / restick() for the button embeds
cogs/invites.py   Get Plex Access button + modal, typed emails, /plexinvite, role handling, on_ready embed + command sync
cogs/requests.py  Search & Request button + modal, typed titles, result menus, submit + announce, watcher, /requests request|mystatus
cogs/board.py     live status board loop
cogs/newonplex.py new-on-Plex feed loop
cogs/revoke.py    on_member_remove / on_member_update → revoke
cogs/stats.py     /requests plexstats
scripts/plex_token.py   plex.tv/link token helper
```

State lives in the runtime's `data/state.json` under the `svc:plexrequests:` prefix — nothing is written next to the code.

## Development

```bash
python -m venv venv && . venv/bin/activate
pip install -e core -e bots/plexrequests pytest pytest-asyncio
pytest bots/plexrequests
```

Tests build the service through the real `Store → Runtime.assemble() → spec.build()` path with fake Plex/Seerr/Radarr/
Sonarr clients and fake Discord objects; nothing touches the network. Conventions: [docs/CONTRIBUTING.md](../../docs/CONTRIBUTING.md).

## License

MIT
