# Contributing / writing a new bot

## Rules of the road
- One bot per integration under `bots/`. Related services that are always deployed together (the *arr stack, Prometheus+Alertmanager+Grafana) share a bot.
- Build on `core/`. Don't fork its helpers into your bot; PR the core instead.
- Python 3.10+, `discord.py` 2.x, async everywhere. No blocking calls in the event loop.
- One top-level slash group per bot (`/pve`, `/prom`, …). Subcommands, not a flat pile.
- Every embed goes through `lab_embed(..., lab_name=bot.lab_name)` so labs are distinguishable.
- A live `StatusBoard` in `STATUS_CHANNEL_ID`, refreshed every `STATUS_INTERVAL_S`, with a `RefreshView`.
- Alerts use `bot.alerts.fire(Alert(fingerprint=...))` / `resolve(fingerprint)`. Fingerprints are stable strings: `<bot>:<object>:<id>:<condition>`.
- Anything destructive: `admin_only()` + `ConfirmView` + ephemeral reply.
- Three consecutive API failures → CRITICAL `<service> unreachable`, resolved on recovery. Loops never crash.
- Fail fast on missing config with a message that says which var.
- No secrets in logs. No hard-coded ids.

## Repo skeleton
```
periscope/
  core/src/periscope/            shared library (bot base, embeds, alerts, boards, views, webhook, state)
  bots/<name>/
    src/periscope_<name>/{__main__.py, config.py, client.py, cogs/}
    tests/  .env.example  pyproject.toml  Dockerfile  README.md
  setup.sh  update.sh  periscope.cli  periscope@.service     # install + CLI + systemd template unit
  deploy/lxc-create.sh                                        # PVE one-liner
  docker-compose.yml                                          # secondary deploy path
```
Copy any existing `bots/<name>/`, replace `client.py` + `cogs/`, pick an unused default `WEBHOOK_PORT` in `.env.example`, and add the name to the matrix in both workflows. The systemd template (`periscope@<name>`) and the CLI discover bots from `bots/`, so nothing else needs registering.

## Deploy conventions (same as [displexia](https://github.com/xchronusx/displexia))
- The repo *is* the install: `git clone` to `/opt/periscope`; each bot's `.env` + `data/` live in `bots/<name>/` (gitignored).
- `setup.sh` is idempotent: apt deps → one venv → `pip install -e core -e bots/*` → CLI → template unit → enable every bot with a token.
- `update.sh` = `git pull --ff-only` + reinstall + refresh CLI/unit + restart enabled bots. Never anything destructive.
- `periscope.cli` / `periscope@.service` use `__DIR__` placeholders; `setup.sh` substitutes the checkout path.

## Release
Tag `vX.Y.Z` on `main`; `docker.yml` pushes `ghcr.io/formicaria/periscope-<name>:X.Y.Z` and `:latest` for amd64+arm64.

## Ideas queue
- `docker` — container status/restarts/image updates (Portainer or socket)
- `uptime` — Uptime Kuma webhook receiver
- `truenas` — pools, scrubs, SMART
- `pihole` — blocked %, top clients
- `power` — UPS via NUT
- cross-lab `/lab status` roll-up bot that reads every board and posts one summary
