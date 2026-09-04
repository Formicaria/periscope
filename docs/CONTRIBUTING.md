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
  setup.sh  update.sh  periscope.cli  periscope.service      # install + CLI + the one systemd unit
  web/                                                        # admin UI (FastAPI + HTMX), runs inside the runtime
  config/periscope.yaml                                       # the whole install's config (0600), edited by UI/CLI
  deploy/lxc-create.sh                                        # PVE one-liner
  docker-compose.yml                                          # secondary deploy path
```
Copy any existing `bots/<name>/`, replace `client.py` + `cogs/`, write `service.py` with `SERVICES = [ServiceSpec(...)]` (see `bots/proxmox/src/periscope_proxmox/service.py`: settings from `.env.example`, `build()` wiring clients + cogs onto the `ServiceBot` facade, `check()` for the UI's Test button), and add the name to the `ci.yml` matrix. The runtime discovers services from every installed `periscope_*` package; the web UI renders their settings automatically.

## Deploy conventions (same as [displexia](https://github.com/xchronusx/displexia))
- The repo *is* the install: `git clone` to `/opt/periscope`; each bot's `.env` + `data/` live in `bots/<name>/` (gitignored).
- `setup.sh` is idempotent: apt deps → one venv → `pip install -e core -e bots/*` → CLI → template unit → enable every bot with a token.
- `update.sh` = `git pull --ff-only` + reinstall + refresh CLI/unit + restart enabled bots. Never anything destructive.
- `periscope.cli` / `periscope.service` use `__DIR__` placeholders; `setup.sh` substitutes the checkout path.
- Config changes apply on restart (`periscope restart`, or the button in the UI).

## Release
Tag `vX.Y.Z` on `main`; `docker.yml` pushes `ghcr.io/formicaria/periscope-<name>:X.Y.Z` and `:latest` for amd64+arm64.

## Ideas queue
- `docker` — container status/restarts/image updates (Portainer or socket)
- `uptime` — Uptime Kuma webhook receiver
- `truenas` — pools, scrubs, SMART
- `pihole` — blocked %, top clients
- `power` — UPS via NUT
- cross-lab `/lab status` roll-up bot that reads every board and posts one summary

## Releases

Every change ships as a release, the way the other Formicaria repos do it:

1. Bump the version everywhere it appears — `core`, `web` and every `bots/*` package carry the same number, in
   both their `__init__.py` and their `pyproject.toml`:

```bash
V=0.1.2
sed -i "s/^__version__ = \".*\"/__version__ = \"$V\"/" core/src/periscope/__init__.py web/src/periscope_web/__init__.py bots/*/src/*/__init__.py
sed -i "0,/^version = \".*\"/s//version = \"$V\"/" core/pyproject.toml web/pyproject.toml bots/*/pyproject.toml
```
2. Add a `## vX.Y.Z` section at the top of `CHANGELOG.md` saying what changed and why.
3. Commit, then tag and push the tag:

```bash
git commit -am "vX.Y.Z: what changed"
git push
git tag vX.Y.Z && git push origin vX.Y.Z
```

`.github/workflows/release.yml` takes it from there: it refuses a tag that does not match `__version__` or has
no CHANGELOG section, runs the test suite, pushes `ghcr.io/formicaria/periscope:X.Y.Z` and `:latest`, and
publishes the release **PERISCOPE vX.Y.Z** with that CHANGELOG section as its notes and a source archive attached.
