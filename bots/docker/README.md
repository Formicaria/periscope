# periscope · docker

A Discord bot that watches the containers on a Docker host and posts a live board, alerts, and `/docker …` slash commands. It talks to the Engine API directly — over the unix socket or a TCP endpoint — or through Portainer's proxy when you would rather not expose the socket at all.

Part of the [periscope](../../README.md) pack, built on its shared core. One instance per host; several hosts can share the same Discord channels — every embed carries your `LAB_NAME`.

## What it does

**Live status board** — one pinned message in `STATUS_CHANNEL_ID`, edited in place (never more often than `STATUS_INTERVAL_S`), with a 🔄 Refresh button:

> 🔴 **Docker — /var/run/docker.sock**
> **3/6** containers running · 1 unhealthy · 1 restarting · 2 stopped
> 🐳 27.1.1 · `/var/run/docker.sock`
>
> **Containers (6)**
> 🔴 **radarr** `linuxserver/radarr:5.11.0` · exited (137)
> 🟡 **immich-ml** `ghcr.io/immich-app/immich-machine…` · restarting · last exit 1
> ⚪ **pgbackup** `prodrigestivill/postgres-backup-l…` · stopped
> 🟡 **sonarr** `linuxserver/sonarr:4.0.9` · up 3h · unhealthy · cpu 12% · 366.0 MB
> 🟢 **jellyfin** `linuxserver/jellyfin:10.9.11` · up 12d · cpu 24% · 1.1 GB
> 🟢 **traefik** `traefik:v3.1` · up 12d · cpu 1% · 88.0 MB
>
> **⬆ Images with updates (2)**
> `linuxserver/sonarr:4.0.9` · `traefik:v3.1`

Containers that need attention sort to the top. The embed goes red when something crashed, yellow when a health check fails or a container is looping, green when the host is quiet. CPU and memory are sampled for up to 12 running containers per poll — a stats sample costs the daemon about a second each, so a busier host simply shows the state and uptime.

The last row only appears when `DOCKER_CHECK_UPDATES` is on.

**Alerts** posted to `ALERT_CHANNEL_ID` (CRITICAL ones ping `ALERT_ROLE_ID`; resolved alerts are edited green in place):

| Alert | Severity | Fingerprint | Resolves |
|---|---|---|---|
| Container exited with a non-zero code | CRITICAL | `docker:container:<name>:exited` | when it is running again |
| Container stopped cleanly (only with `DOCKER_ALERT_ON_STOP`) | WARNING | `docker:container:<name>:exited` | when it is running again |
| Health check reports `unhealthy` | WARNING | `docker:container:<name>:unhealthy` | when it is healthy again |
| `DOCKER_RESTART_LOOP_N` restarts inside an hour | WARNING | `docker:container:<name>:restart_loop` | when an hour passes without one |
| Daemon unreachable (3 failed polls) | CRITICAL | `docker:unreachable` | on the next successful poll |
| Images have a newer digest (only with `DOCKER_CHECK_UPDATES`) | INFO | `docker:updates` | when every image is current |

A container you stopped yourself is **not** an alert by default — that is what `DOCKER_ALERT_ON_STOP` is for — but a non-zero exit code always is. A container that is removed from the host has its open alerts closed rather than left hanging.

The image-update notice is one alert listing every image the registry serves a newer digest for. It is re-posted when that list changes, and once a week while it stands.

## Slash commands

All commands live under one group so several bots can coexist in a server. Every reply is ephemeral (only you see it).

| Command | What it does | Admin |
|---|---|---|
| `/docker ps [name] [running_only]` | Paginated list of the watched containers; `name` accepts globs (`*arr`) | |
| `/docker restart <container>` | Restart a container — asks for confirmation | ✅ |
| `/docker start <container>` | Start a stopped container — asks for confirmation | ✅ |
| `/docker stop <container>` | Stop a running container — asks for confirmation | ✅ |
| `/docker logs <container> [lines]` | The last N lines it logged (default 50, at most 200) in a code block | |
| `/docker stats <container>` | CPU, memory, network, block I/O and restart count of one container | |
| `/docker updates` | Which images the registry has a newer digest for, and what runs them | |

Container names autocomplete from the last poll. Admin = a role in `ADMIN_ROLE_IDS`, or the server Administrator permission if that is unset.

## Setup (≈10 minutes)

### 1. Create the Discord bot

1. Go to <https://discord.com/developers/applications> → **New Application** → name it (e.g. `docker · my-lab`).
2. **Bot** tab → **Reset Token** → copy it into `DISCORD_TOKEN`. No privileged intents are needed.
3. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; bot permissions **Send Messages**, **Embed Links**, **Manage Messages** (to pin the board), **Read Message History**. Open the generated URL and add the bot to your server.
4. Turn on Developer Mode in Discord (Settings → Advanced), then right-click → **Copy ID** for the server (`GUILD_ID`), the alert channel, the status channel, and optionally an alert role and admin roles.

### 2. Let periscope reach the daemon

Pick **one** of these. `DOCKER_HOST` is tried first, so leave it alone if you want Portainer.

**a. The unix socket (simplest).** Nothing to configure — `DOCKER_HOST` already points at `/var/run/docker.sock`. The account periscope runs as has to be allowed to read it:

```bash
sudo usermod -aG docker periscope   # then restart the service so the new group takes effect
```

That group is root-equivalent: anything that can talk to the socket can start a privileged container. If that is too much for you, put a **socket proxy** in front (for example [`tecnativa/docker-socket-proxy`](https://github.com/Tecnativa/docker-socket-proxy)) with only the endpoints below allowed, and point `DOCKER_HOST` at it:

```yaml
environment:
  CONTAINERS: 1        # /containers/json, /containers/{id}/json, stats, logs
  IMAGES: 1            # /images/json (only needed for DOCKER_CHECK_UPDATES)
  DISTRIBUTION: 1      # /distribution/{name}/json (same)
  POST: 1              # start / stop / restart — leave at 0 for a read-only bot
```

In a container, mount the socket and add its group: `-v /var/run/docker.sock:/var/run/docker.sock:ro --group-add "$(stat -c '%g' /var/run/docker.sock)"`. Read-only is enough unless you want the power commands.

**b. A TCP endpoint.** `DOCKER_HOST=tcp://10.0.0.5:2375` for a plain one (only ever on a trusted network — it is unauthenticated), or `DOCKER_HOST=tcp://docker.lan:2376` with `DOCKER_TLS_VERIFY=true` and the certificates. `DOCKER_CERT_PATH` may name the directory the docker CLI uses, in which case `ca.pem`, `cert.pem` and `key.pem` are read from it.

**c. Portainer.** Leave `DOCKER_HOST` at its default and set `PORTAINER_URL`, `PORTAINER_API_KEY` (Portainer → *My account* → *Access tokens* → *Add access token*) and `PORTAINER_ENDPOINT_ID` — the number in the URL when you open the environment, usually `1`. Every call goes to `/api/endpoints/<id>/docker/...`, so Portainer's own access control decides what the bot may do. Portainer's certificate is not checked unless you set `DOCKER_TLS_VERIFY=true`, since a self-signed one is the norm.

### 3. Choose what to watch

By default every container on the host is watched. `DOCKER_INCLUDE` narrows that to a list of name globs, and `DOCKER_IGNORE` takes names back out of it — the usual pair is `DOCKER_IGNORE=buildx_*,*-test`.

### 4. Configure

Open the web UI (`periscope web`) → **docker** → fill in the values from the steps above → **Test** → **Save** → enable. From a terminal: `periscope config docker KEY=VALUE …` then `periscope enable docker`; `periscope check docker` runs the same test, which connects and reads the daemon's version.

## Environment variables

Common variables come from the shared core; Docker ones are specific to this bot. Everything is listed with comments in [`.env.example`](.env.example).

| Var | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | yes | | Bot token |
| `LAB_NAME` | | `lab` | Footer on every embed; identifies your lab |
| `LAB_COLOR` | | `5865F2` | Hex accent color |
| `GUILD_ID` | | | Server id — set it so commands sync instantly |
| `ALERT_CHANNEL_ID` | | | Channel for alerts |
| `STATUS_CHANNEL_ID` | | | Channel for the pinned board (unset = alerts only) |
| `ALERT_ROLE_ID` | | | Role pinged on CRITICAL |
| `ADMIN_ROLE_IDS` | | | Comma-separated role ids allowed to start/stop/restart |
| `STATUS_INTERVAL_S` | | `60` | How often the board message may be edited |
| `DATA_DIR` | | `data` | Persistent state (alert message ids, board message id, restart history) |
| `WEBHOOK_PORT` | | `8085` | Port of the `/health` endpoint |
| `LOG_LEVEL` | | `INFO` | |
| `DOCKER_HOST` | | `/var/run/docker.sock` | Socket path, `tcp://host:2375` or `https://host:2376` |
| `DOCKER_TLS_VERIFY` | | `false` | Verify the certificate of whatever it connects to (the daemon, or Portainer); also turns `tcp://` into `https://` |
| `DOCKER_CA_PATH` | | | CA certificate (PEM) |
| `DOCKER_CERT_PATH` | | | Client certificate (PEM), or the directory holding `ca.pem`/`cert.pem`/`key.pem` |
| `DOCKER_KEY_PATH` | | | Client key (PEM) |
| `PORTAINER_URL` | | | Reach Docker through Portainer instead of the daemon |
| `PORTAINER_API_KEY` | | | Portainer access token |
| `PORTAINER_ENDPOINT_ID` | | `1` | Which Portainer environment |
| `DOCKER_INCLUDE` | | | Name globs to watch (empty = every container) |
| `DOCKER_IGNORE` | | | Name globs to leave out, applied after `DOCKER_INCLUDE` |
| `DOCKER_POLL_S` | | `60` | How often the daemon is polled (at least 10) |
| `DOCKER_RESTART_LOOP_N` | | `3` | Restarts inside an hour before the restart-loop alert |
| `DOCKER_ALERT_ON_STOP` | | `false` | Alert on a clean stop as well as a crash |
| `DOCKER_CHECK_UPDATES` | | `false` | Ask the registry whether the images in use moved on |
| `DOCKER_UPDATE_CHECK_H` | | `12` | How often that check runs |

Nothing is required: on a host whose socket periscope can read, the defaults are enough. Bad values abort startup with a message that names the setting.

## Health check and ports

The bot receives no webhooks, but it still starts the `periscope` webhook server with **no routes** so that `GET /health` exists (`{"ok": true}` once connected to Discord, `503` otherwise). The Dockerfile's `HEALTHCHECK` curls it from inside the container, so no ports need publishing.

## How it talks to Docker

- Plain HTTP against the Engine API — no `docker` package, only `aiohttp`, which dials a unix socket as happily as a host and port.
- Endpoints used: `GET /version`, `/containers/json?all=1`, `/containers/{id}/json`, `/containers/{id}/stats?stream=false`, `/containers/{id}/logs`, `/images/json`, `/distribution/{name}/json`, and `POST /containers/{id}/start|stop|restart`.
- Through Portainer the same paths hang off `/api/endpoints/<id>/docker`, with the token in an `X-API-Key` header.
- Log output is un-framed before it is shown: without a TTY the daemon prefixes every write with eight bytes saying which stream it came from and how long it is.
- Update checks compare the digest the registry serves for a tag with the one the local image was pulled from. An image built locally (no digest) or a registry that will not answer is skipped rather than reported.
- Only outbound connections: to Discord and to your daemon. Nothing needs exposing from your lab.

## State

`DATA_DIR/state.json` holds the board message id, active alert message ids (so restarts resolve alerts in place), the last state seen for each container, the restart timestamps inside the current hour, and the last image-update result. Delete it to start fresh.

## Development

```bash
python -m venv venv && . venv/bin/activate
pip install -e core -e bots/docker pytest pytest-asyncio
pytest bots/docker
```

Conventions: [docs/CONTRIBUTING.md](../../docs/CONTRIBUTING.md).

## License

MIT
