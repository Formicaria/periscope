# periscope · github

GitHub organization activity feed for Discord. One org-level webhook on GitHub → rich embeds in your
Formicaria channels (`#formicaria`, `#op-anthill`, `#op-sovrgn`, …), CI-failure alerts that resolve themselves,
a live org overview board, and `/gh` slash commands to query repos, PRs, issues, runs and commits.

Part of the [periscope](../../README.md) pack, built on its shared core, so it shares the same env vars,
embed style, alert routing and status-board plumbing as every other lab bot.

## What it looks like

- **Push** — `[anthill:main] 3 new commits` with up to five `abc1234` links, first-line messages and authors, linked to the compare view. Force-pushes are flagged ⚠️ in yellow.
- **Pull request** — `[sovrgn] PR #42 merged: Add pheromone router` — `main ← alice:feat/router • +120/-14 • 6 files`. Green when merged, red when closed unmerged, grey for drafts. Reviews show ✅ approved / 🔁 changes requested / 💬 commented.
- **Issues / comments** — opened, closed, reopened, labeled, assigned; comments truncated to 300 chars with a link.
- **Release** — 🚀 name, tag, first 500 chars of notes, asset count.
- **Workflow run** — `❌ CI failure on main • Run #91 • 4m 12s • abc1234 fix tests`. A failure on the repo's **default branch** fires a CRITICAL alert (optionally pinging a CI role) that is edited to 🟢 RESOLVED on the next success.
- **Also**: branch/tag create & delete, stars, forks, repository created/deleted/archived/renamed/publicized/privatized, members joining/leaving the org or teams, deployment statuses, discussions, failed check runs.
- **Status board** (pinned in `STATUS_CHANNEL_ID`, refreshed every `STATUS_INTERVAL_S`, 🔄 button): repo count, open PRs, open issues, per-repo CI status 🟢/🔴, last 10 events as relative-time one-liners.

Every embed carries the sender's login + avatar, links back to GitHub, and the `🧪 <LAB_NAME>` footer.

## Slash commands (`/gh …`)

| Command | What it does |
|---|---|
| `/gh repos` | All org repos: stars, open issues, last push (paginated) |
| `/gh repo <name>` | Description, default branch, open PRs/issues, stars/forks, size, languages, latest release, CI status |
| `/gh prs [repo]` | Open pull requests across the org or in one repo (search API) |
| `/gh issues [repo]` | Open issues across the org or in one repo |
| `/gh runs [repo]` | Latest workflow runs (one repo, or the 5 most recently pushed repos) |
| `/gh commits <repo> [branch] [n]` | Last *n* (1–30) commits on a branch |
| `/gh activity` | Event counts received in the last 24 h, by type, plus the latest events |
| `/gh watch` | Force a re-render of the live org overview board in `STATUS_CHANNEL_ID` |

Repository names autocomplete from a cached org repo list (refreshed every 10 min).

## Setup (≈10 minutes)

### 1. Discord application

1. <https://discord.com/developers/applications> → **New Application** → name it (e.g. `LAB GitHub`).
2. **Bot** tab → **Reset Token** → copy it into `DISCORD_TOKEN`. No privileged intents are needed.
3. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; bot permissions **Send Messages**, **Embed Links**, **Read Message History**, **Manage Messages** (to pin the status board), **Mention Everyone** (only if you want role pings). Open the generated URL and invite the bot to THE LAB.
4. Enable *Developer Mode* in Discord (User Settings → Advanced), then right-click → **Copy ID** for your server (`GUILD_ID`), the feed channel(s), the alert channel and the status channel.

### 2. GitHub fine-grained PAT (for `/gh` commands and polling)

GitHub → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.

- **Resource owner**: the `formicaria` organization (an org owner may need to approve it).
- **Repository access**: *All repositories* (or the ones you care about).
- **Permissions** (all *Read-only*):
  - Repository: **Contents** (commits, branches), **Metadata** (always), **Pull requests**, **Issues**, **Actions** (workflow runs).
  - Organization: **Members** (only needed for `/orgs/{org}/events` polling of a private org).
- Put it in `GITHUB_TOKEN`. The bot never writes to GitHub. Without a token, `/gh` still works for public repos but with GitHub's 60 req/h unauthenticated limit; polling requires a token.

### 3. Make the bot reachable from GitHub

GitHub must be able to `POST https://<public-host>/github`. The bot listens on `WEBHOOK_PORT` (8084 by default in the pack). Pick one:

- **Cloudflare Tunnel** (no open ports, free): `cloudflared tunnel --url http://localhost:8084` — prints a public `https://…trycloudflare.com` URL (for a permanent hostname create a named tunnel in Zero Trust and route `github.yourlab.example` → `http://localhost:8084`).
- **Tailscale Funnel**: `tailscale funnel 8084` → `https://<machine>.<tailnet>.ts.net/github`.
- **Reverse proxy** (Caddy/Traefik/nginx) already terminating TLS on your domain → proxy `/github` and `/health` to port 8084.

Only `/github` (HMAC-verified) and `/health` (unauthenticated JSON `{"ok": true}`) are exposed; everything else 404s.

### 4. Create the org webhook (one for the whole organization)

1. Generate a secret: `openssl rand -hex 32` → put it in `WEBHOOK_SECRET`.
2. GitHub → organization **formicaria** → **Settings → Webhooks → Add webhook**.
3. **Payload URL**: `https://<public-host>/github`
4. **Content type**: `application/json`
5. **Secret**: the exact `WEBHOOK_SECRET` value.
6. **SSL verification**: enabled.
7. **Which events?** → **Send me everything** (the bot filters; use `GITHUB_EVENTS` to narrow what gets posted).
8. **Active** ✓ → **Add webhook**. GitHub immediately sends a `ping`; the bot logs `webhook ping from https://<public-host>/github` and the delivery shows a green ✓ under *Recent Deliveries*. A red ✗ with `401` means the secret does not match; a timeout means step 3 is not working.

Signatures are verified with `X-Hub-Signature-256` over the raw body; deliveries are de-duplicated by `X-GitHub-Delivery`.

### 5. Run it

From the periscope checkout (see the [pack README](../../README.md) for install):

Open the web UI (`periscope web`) → **github** → fill in the values from the steps above → **Test** → **Save** → enable. From a terminal: `periscope config github KEY=VALUE …` then `periscope enable github`; `periscope check github` runs the same test.

## Configuration

Common variables (from `periscope`):

| Variable | Default | Meaning |
|---|---|---|
| `DISCORD_TOKEN` | — | **Required.** Bot token |
| `LAB_NAME` | `lab` | Footer on every embed; run one instance per lab |
| `LAB_COLOR` | `5865F2` | Hex color for neutral embeds |
| `GUILD_ID` | — | Server id; slash commands sync instantly to this guild |
| `ALERT_CHANNEL_ID` | — | CI-failure and "GitHub API unreachable" alerts; also the feed fallback |
| `STATUS_CHANNEL_ID` | — | Where the live org board is pinned |
| `ALERT_ROLE_ID` | — | Role pinged on CRITICAL alerts |
| `ADMIN_ROLE_IDS` | — | Comma list of admin role ids |
| `STATUS_INTERVAL_S` | `60` | Board refresh period |
| `DATA_DIR` | `data` | Persistent JSON state |
| `LOG_LEVEL` | `INFO` | `DEBUG` logs every ignored/unknown event |
| `WEBHOOK_HOST` / `WEBHOOK_PORT` | `0.0.0.0` / `8084` | Listener for `/github` and `/health` |
| `WEBHOOK_SECRET` | — | **Set it.** Must equal the GitHub webhook secret (HMAC-SHA256) |

GitHub variables:

| Variable | Default | Meaning |
|---|---|---|
| `GITHUB_ORG` | `formicaria` | Organization login |
| `GITHUB_TOKEN` | — | Fine-grained read-only PAT (see §2) |
| `GITHUB_CI_CHANNEL_ID` | feed channel | Where workflow / check results are posted |
| `GITHUB_FEED_CHANNEL_ID` | `ALERT_CHANNEL_ID` | Default channel for the feed (e.g. `#formicaria`) |
| `GITHUB_REPO_CHANNEL_MAP` | — | `anthill=<#op-anthill id>,sovrgn=<#op-sovrgn id>,microround*=<id>` — per-repo routing; exact names win, then glob patterns (`*`, `?`), then the default |
| `GITHUB_EVENTS` | all | Comma list of `X-GitHub-Event` names to post, e.g. `push,pull_request,release,workflow_run` |
| `GITHUB_VERBOSE` | `true` | Post every event and action (PR syncs/labels, review edits, in-progress runs, unknown event types get a generic card). `false` = curated highlights only |
| `GITHUB_IGNORE_BOTS` | `false` | `true` drops events whose sender ends in `[bot]` (dependabot, github-actions…) — off by default so Actions-made commits and releases show |
| `GITHUB_CI_FAILURE_ROLE_ID` | — | Role pinged (as a reply to the alert) when a workflow fails on a default branch |
| `GITHUB_POLL_ENABLED` | `true` | Poll the org feed + workflow runs with `GITHUB_TOKEN` (see below) |
| `GITHUB_POLL_INTERVAL_S` | `120` | Poll period (≥ 30; GitHub's `X-Poll-Interval` is honoured) |

Example routing for the Formicaria category:

```
GITHUB_FEED_CHANNEL_ID=<#formicaria>
GITHUB_REPO_CHANNEL_MAP=anthill=<#op-anthill>,microround=<#op-microround>,sovrgn=<#op-sovrgn>,pherosphere*=<#op-pherosphere>
```

## How events reach the bot

Two sources, both feeding the same pipeline (dedupe by delivery id, so running both is safe):

- **Polling** (default on when `GITHUB_TOKEN` is set): every `GITHUB_POLL_INTERVAL_S` the bot reads the org event feed (push, PR, issues, releases, forks, stars, branches, members…) **and** each repo's workflow runs — every Actions run (CI, release builds, anything with a workflow) gets a **live train card** in the CI channel the moment it's spotted, refreshed every 15 s with one line per job (⚪ queued · 🟡 running with `step n/m · name` · ✅ ❌ ⏭️ with duration) and finalized green/red with the total time. The event feed alone never carries runs. Nothing inbound needed. First run baselines silently — no history replay. Latency ≈ the poll interval.
- **Org webhook** (optional, instant): github.com/organizations/&lt;org&gt;/settings/hooks → Add webhook → `https://<public-host>/github`, `application/json`, secret = `WEBHOOK_SECRET`, "Send me everything". Needs the bot's port reachable from GitHub (reverse proxy / Cloudflare Tunnel / Tailscale Funnel).

Routing: `GITHUB_REPO_CHANNEL_MAP` gives each project its own channel that receives **everything** for those repos — commits, PRs, issues, releases and the live CI trains (`Anthill=123,micromound=123,SOVRGNnet.cc=456,periscope=789`, globs allowed). Repos not in the map go to `GITHUB_FEED_CHANNEL_ID` (CI to `GITHUB_CI_CHANNEL_ID`); leave both blank to drop unmapped repos. `GITHUB_MIRROR_TO_FEED=true` makes mapped repos also post to the catch-alls. `periscope layout` sets channel permissions (`#git-*` humans read-only / bots post, `#op-*` bots muted) and prints the map for you. A failing workflow on a default branch also raises an alert in `ALERT_CHANNEL_ID` pinging `GITHUB_CI_FAILURE_ROLE_ID`, resolved in place when it goes green.

## Alerts & robustness

- `gh:<repo>:workflow:<name>:<branch>` — CRITICAL on a failed/timed-out run on the default branch, resolved by the next successful run of the same workflow on that branch.
- `gh:api:unreachable` — CRITICAL after 3 consecutive GitHub API failures (commands, board or poller), resolved on the next success. The board keeps showing cached data meanwhile.
- Webhook handler errors return 500 (GitHub will show them under *Recent Deliveries* and you can *Redeliver*); unknown events return 200 and are logged at DEBUG.

## Development

```bash
python -m venv venv && . venv/bin/activate
pip install -e core -e bots/github pytest pytest-asyncio
pytest bots/github
```

Conventions: [docs/CONTRIBUTING.md](../../docs/CONTRIBUTING.md).

## License

MIT
