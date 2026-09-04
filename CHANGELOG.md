# Changelog

Every release is a tag (`vX.Y.Z`) whose section here becomes the GitHub release notes. Every package in the
repo carries the same version — every `__init__.py` and every `pyproject.toml` — and the release workflow
refuses a tag that disagrees with any of them or has no section here.

## v0.2.0

Four things periscope could not do before: change a setting without a restart, remember anything, be told to be
quiet, or find your services for you. Plus Docker.

**NO MORE RESTARTS.** Saving a setting used to bounce every bot in the process. Now the runtime rebuilds *that one
service* in place — its cogs come off, its clients close, it is built again from the new settings and its slash
commands are re-registered — while every other service and every other bot carries on untouched. Switching a
service on or off, pointing it at another bot or another server, and editing `config/periscope.yaml` by hand all
work the same way: the process watches that file and applies what changed within a second. `periscope reload`
hurries it along; `periscope config` and `periscope enable` no longer end with "restart to apply". The header
banner is gone unless something genuinely needs the process to start again — a new bot token, a brand-new bot —
and then it says which. A service that fails to rebuild reports why and leaves everything else running.

**PERISCOPE REMEMBERS.** A SQLite event log (`data/history.db`, WAL, pruned on a retention you set) sits behind
everything: alerts fired, acked and resolved, CI runs, grabs and imports, requests and invites, container state
changes, plus numeric samples for CPU, memory, disk, queue depth and stream counts. Out of it come a **Trends**
page — uptime per service, alert counts over 24 h / 7 d / 30 d, sparklines drawn as inline SVG, a filterable
event table with a CSV download — and a **recap** post that says what happened while you were asleep. Every write
is crash-proof and off the hot path; the bots run exactly the same when the log is not there.

**ALERTS YOU CAN ANSWER.** Every alert now carries **Ack**, **Snooze** (1 h / 8 h / 24 h) and **Resolve** buttons,
admin-gated and surviving restarts. Acking records who and when and stops the pings; a repeat of the same alert
edits the card and counts it instead of posting again; a CRITICAL nobody acked escalates to the alert role once
after a delay you choose. **Maintenance windows** (`config/maintenance.yaml`, editable on the new **Alerts** page)
keep periscope quiet on a schedule or for a one-off stretch — the backup window that pegs the CPU every night
stops paging you, and the card says it would have fired. A broken window file fails open: you get the alert.

**DOCKER.** A new service: containers running / stopped / unhealthy / restarting on the status board with image,
uptime, CPU and memory; alerts for a non-zero exit, an unhealthy check, a restart loop and an unreachable daemon;
optional image-update checks; and `/docker ps · restart · start · stop · logs · stats · updates`, the mutating
ones admin-gated behind Confirm. It talks to the socket, a TCP or TLS endpoint, or through Portainer for people
who do not expose the socket.

**FIND MY SERVICES.** A **Discover** page (and a step in the first-run flow) scans the network you point it at,
identifies what answers by its own API rather than by port alone — Sonarr, Radarr, Lidarr, Prowlarr, qBittorrent,
SABnzbd, Plex, Jellyfin, Overseerr, Prometheus, Alertmanager, Grafana, Proxmox, UniFi, Docker — prefills that
service's settings, runs its Test and offers to switch it on. Anything the scan cannot reach can come from a
`docker-compose.yml` or an existing *arr `config.xml` instead. Scans only ever start when an admin asks for one.

Under the hood: `bot.history` and `bot.windows` exist on every service (no-ops when those parts are absent), so a
send site can call them unconditionally. 598 tests.

## v0.1.3

**THE UPDATE INSTALLS IN ONE GO.** `setup.sh` and `periscope update` installed core and the web UI first and the
services afterwards, so a version bump made pip judge an inconsistent half of the repo and complain (v0.1.2 went
through with a warning about the six service packages). Both scripts now hand pip every package in a single
invocation — the resolver sees the whole set, and a bump cannot leave a box half-installed.

**`periscope list` stopped running the server into the title.** The SERVER column had no gap after a 12-character
name ("ztechnus.comGitHub").

**THE RELEASE BUILD FAILED ON THE IMAGE NAME.** Docker rejects a capital letter in a repository name, and the
release workflow built its tags from the owner verbatim (`ghcr.io/Formicaria/…`). Both the image tags and the
`docker pull` line in the notes now downcase it.

## v0.1.2

**THE INSTALL WOULD NOT UPDATE.** Renumbering to 0.1.1 left every package still requiring `periscope>=1.0`, so
`periscope update` stopped at "conflicting dependencies" and nothing was installed. Core, the web UI and all six
service packages now carry the same version and require `periscope>=0.1`, and the release workflow refuses a tag
unless every `__init__.py` and every `pyproject.toml` in the repo agrees with it — this cannot ship again.

## v0.1.1

**BOTS, LAID OUT PER DISCORD SERVER.** The Bots page was one flat table, so an install posting in two Discord
servers gave no clue which bot lived where. It is now one section per server — heading, Discord id, the bots
that post there, each row editable in place as before. A bot that serves several servers appears in each
section and says so; a bot nothing uses yet waits under "Not in use". Each row also states whether that bot is
actually a member of that server, with its invite link when it is not.

**A SERVICE'S OWN DISCORD SERVER BECOMES A SERVER.** Configs written before multiple servers existed hid a
second Discord server inside a service's settings (the Plex request service carried its own `GUILD_ID`). On
load, any such service is promoted to a server of its own — its channels come along, the override is dropped,
and the upgraded file is written back so the config and the UI agree. Two servers, both visible, both editable.

**SERVERS ARE TOLD APART BY THEIR REAL DISCORD NAME.** A server's `name` is only the wording embed footers
carry; two servers can share one. Cards now lead with the real Discord name (fetched from the guild) above the
id and key, the display-name field says what it actually is, and one click copies the Discord name into it.
Everywhere else a server is named — the "in server" picker, the Overview cards, routing rows, toasts — reads
`display name (Real Discord Name)` when the two differ.

**EVERY POST IS EDITABLE, WITH A PREVIEW.** A new Messages page lists every kind of post the bots make — status
boards, alerts, the whole GitHub feed, media grabs and imports, the Plex invite and request embeds — drawn the
way Discord draws them. *Simple* is a form (title, text, colour, footer, extra fields) with clickable
`{{ variables }}`; *Code* is the raw template, embed-shaped JSON with sandboxed Jinja (`if`, `repeat`, filters).
Live preview beside the shipped default, reset, switch a kind off, and a test post into its real channel.
Customisations live in `config/messages.yaml` and apply to the next post — no restart. A broken template never
blocks a post: the bot's own version goes out and the error is logged.

**MANY DISCORD SERVERS FROM ONE INSTALL.** `servers:` in `config/periscope.yaml` holds one entry per Discord
server (name, colour, id, status/alert channels, admin roles). Every service names the server it posts in, and
a bot serves every server its services use, registering its slash commands in each. Settings that are not
per-server — log level, board refresh — moved to their own card. The UI says "server" throughout; only the
`#lab-status` / `@lab-admin` names the channel convention creates keep the old word.

**HOUSEKEEPING.** `periscope list` gained a SERVER column and the bots' servers; `periscope init` asks for a
server name; the CI workflow's core job no longer needs the bot packages installed, and the actions run on
Node 24 with Python 3.13.

## v0.1.0

The first numbered release of the rebuilt periscope: one process and one systemd unit for every integration,
a YAML config store, bots (Discord identities) shared by services, the web UI on :8090 with a one-time
sign-in link, plain-language service states with a "needs attention" list, status boards that stay a single
message, and the standalone Plex bot folded in as the `plexrequests` service.
