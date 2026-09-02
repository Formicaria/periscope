# periscope · proxmox

A Discord bot for **Proxmox VE**. It keeps a live, self-updating cluster dashboard pinned in a channel, raises (and auto-resolves) alerts when something goes wrong, watches your nightly backups, and lets admins start/stop guests from Discord with a confirmation step.

Part of the [periscope](../../README.md) pack, built on its shared core, so it looks and behaves like every other periscope bot. Each member runs their own instance pointed at their own cluster; `LAB_NAME` tells them apart in a shared channel.

## What it looks like

**Status board** (one pinned message, edited in place every `STATUS_INTERVAL_S`, with a 🔄 Refresh button):

```
🟢 Proxmox · homelab
2/2 nodes online · 11/14 guests running (7/9 VM, 4/5 CT)

🟢 pve1
CPU ███░░░░░░░░░  23.4%  (16 cores)
MEM ████████░░░░  68.1%  (43.6 GB / 64.0 GB)
⏱ up 41d 3h · 8/10 guests running

🟢 pve2
CPU ██░░░░░░░░░░  12.0%  (8 cores)
MEM █████░░░░░░░  44.9%  (14.4 GB / 32.0 GB)
⏱ up 12d 7h · 3/4 guests running

Storage
🟢 nfs-backups   ████░░░░░░  41.2%  824.0 GB / 2.0 TB
🟡 pve1/local-lvm ████████░░  87.5%  437.5 GB / 500.0 GB
🔴 pve2/local    ██████████  96.9%  96.9 GB / 100.0 GB
                                              🧪 my-lab
```

**Alerts** land in `ALERT_CHANNEL_ID` and are edited green with "RESOLVED" when the condition clears:

| Condition | Severity | Fingerprint |
|---|---|---|
| Node not online | CRITICAL (pings `ALERT_ROLE_ID`) | `pve:node:<node>:down` |
| Node CPU > `PVE_CPU_WARN` for 3 consecutive polls | WARNING | `pve:node:<node>:cpu` |
| Node memory > `PVE_MEM_WARN` | WARNING | `pve:node:<node>:mem` |
| Storage ≥ `PVE_STORAGE_WARN` / `PVE_STORAGE_CRIT` | WARNING / CRITICAL | `pve:storage:<name>:full` |
| A VM/CT that was running is now stopped | WARNING | `pve:vm:<vmid>:stopped` |
| vzdump backup task failed | WARNING | `pve:backup:<upid>` |
| API unreachable 3 polls in a row | CRITICAL | `pve:unreachable` |

Successful backups post a blue summary (per-guest duration and archive size, pulled from the task log).
Guest states survive restarts (kept in `DATA_DIR/state.json`), so a VM that died while the bot was down still alerts.

## Slash commands

All commands live under `/pve`. Guest ids autocomplete by **name or vmid** (e.g. type `pihole`).

| Command | What it does |
|---|---|
| `/pve nodes` | Every node: CPU, memory, uptime, running/total guests |
| `/pve vms [node] [running_only]` | Paged list (10 per page) of VMs and containers: vmid, name, type, status, cpu %, memory |
| `/pve vm <vmid>` | Detail embed for one guest with **Start / Shutdown / Reboot / Stop** buttons (admin only; Reboot and Stop ask for confirmation) |
| `/pve start <vmid>` | Start a guest *(admin, confirm)* |
| `/pve shutdown <vmid>` | Graceful ACPI / init shutdown *(admin, confirm)* |
| `/pve reboot <vmid>` | Reboot *(admin, confirm)* |
| `/pve stop <vmid>` | Hard power-off *(admin, confirm)* |
| `/pve tasks [node] [limit]` | Recent tasks (backups, migrations, power ops) with status, user and duration |
| `/pve storage [node]` | Storage detail per node: type, content, used/free/total with a usage bar |

"Admin" means a member with one of `ADMIN_ROLE_IDS`, or a server administrator if that variable is unset. Power actions are acknowledged ephemerally and logged with the Proxmox task id (UPID).

## Environment variables

Copy `.env.example` to `.env` and fill it in. Secrets never appear in logs.

### Discord / lab (shared by every lab bot)

| Var | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | yes | | Bot token |
| `LAB_NAME` | | `lab` | Footer on every embed; distinguishes labs sharing a channel |
| `LAB_COLOR` | | `5865F2` | Hex accent color |
| `GUILD_ID` | recommended | | Server id; commands sync instantly when set (otherwise up to an hour) |
| `ALERT_CHANNEL_ID` | recommended | | Where alerts and backup summaries go |
| `STATUS_CHANNEL_ID` | recommended | | Where the live board is pinned. Unset = no dashboard, alerts only |
| `ALERT_ROLE_ID` | | | Role pinged on CRITICAL alerts |
| `ADMIN_ROLE_IDS` | | | Comma-separated role ids allowed to control guests |
| `STATUS_INTERVAL_S` | | `60` | Poll + board refresh interval (minimum 15) |
| `DATA_DIR` | | `data` | Persistent state (board message id, open alerts, guest states) |
| `WEBHOOK_PORT` | | `8080` | Port of the internal `/health` endpoint (Docker HEALTHCHECK) |
| `LOG_LEVEL` | | `INFO` | |

### Proxmox

| Var | Required | Default | Description |
|---|---|---|---|
| `PVE_URL` | yes | | `https://host:8006` — any node of the cluster |
| `PVE_TOKEN_ID` | yes | | `user@realm!tokenname` |
| `PVE_TOKEN_SECRET` | yes | | Secret shown once at token creation |
| `PVE_VERIFY_SSL` | | `false` | Verify the TLS certificate (false for the default self-signed one) |
| `PVE_CPU_WARN` | | `85` | Node CPU % warning threshold (3 consecutive polls) |
| `PVE_MEM_WARN` | | `90` | Node memory % warning threshold |
| `PVE_STORAGE_WARN` | | `85` | Storage % warning threshold |
| `PVE_STORAGE_CRIT` | | `95` | Storage % critical threshold |
| `PVE_WATCH_BACKUPS` | | `true` | Poll vzdump tasks every 5 minutes and report them |

The bot fails fast at startup with a clear message if a required variable is missing or malformed.

## Setup (about 10 minutes)

### 1. Create the Discord application

1. Go to <https://discord.com/developers/applications> → **New Application** → name it (e.g. `pve · my-lab`).
2. **Bot** tab → **Reset Token** → copy it into `DISCORD_TOKEN`. No privileged intents are needed.
3. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; bot permissions **Send Messages**, **Embed Links**, **Manage Messages** (to pin the board), **Read Message History**. Open the generated URL and add the bot to your server.
4. In Discord, enable *Settings → Advanced → Developer Mode*, then right-click the server / channels / roles → **Copy ID** for `GUILD_ID`, `ALERT_CHANNEL_ID`, `STATUS_CHANNEL_ID`, `ALERT_ROLE_ID`, `ADMIN_ROLE_IDS`.

### 2. Create a Proxmox API token

In the Proxmox web UI (or shell on any node). A dedicated user with a read-mostly role is recommended:

```bash
# user + token (realm "pve" keeps it out of Linux PAM)
pveum user add periscope@pve --comment "periscope Discord bot"
pveum role add Periscope -privs "VM.Audit VM.PowerMgmt Datastore.Audit Sys.Audit"
pveum acl modify / -user periscope@pve -role Periscope
pveum user token add periscope@pve discord --privsep 0
```

The last command prints a table — the secret is shown **once**:

```
│ full-tokenid │ periscope@pve!discord                │   → PVE_TOKEN_ID  (the wizard's default)
│ value        │ xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx │   → PVE_TOKEN_SECRET
```

Lost it? `pveum user token remove periscope@pve discord`, then re-run the last command. `periscope init proxmox` prints these same commands and checks the token against `/api2/json/version` before saving it.

- `VM.Audit`, `Datastore.Audit`, `Sys.Audit` are enough for monitoring, task history and backup logs.
- `VM.PowerMgmt` is only needed for `/pve start|stop|shutdown|reboot`; drop it for a read-only bot (the commands will then fail with a permission error from PVE).
- `--privsep 0` makes the token inherit the user's permissions; with `--privsep 1` you must grant the ACL to the token itself (`-token periscope@pve!discord`).

### 3. Run it

From the periscope checkout (see the [pack README](../../README.md) for install):

```bash
periscope init proxmox          # creates bots/proxmox/.env
nano bots/proxmox/.env          # paste the values from steps 1–2
periscope enable proxmox
periscope logs proxmox          # look for "ready as ... (lab=my-lab)" and "synced N app commands"
```

Docker instead: `docker compose up -d proxmox` from the repo root uses the same `bots/proxmox/.env`.

## How it works

- `client.py` — async PVE client on `periscope.HttpClient` using `Authorization: PVEAPIToken=<id>=<secret>`. `GET /cluster/resources` is parsed into `Node` / `Guest` / `Storage` models and cached for 30 s; every other command resolves a vmid to its node and type (qemu vs lxc) from that cache, so nothing needs to be configured per guest.
- `cogs/status.py` — the poll loop, board rendering and all threshold / state-change alerting.
- `cogs/vms.py` — listing, detail with control buttons, and power commands (`ConfirmView` for anything destructive).
- `cogs/tasks.py` — `/pve tasks` and the vzdump watcher (finished tasks since the last check, deduplicated by UPID across restarts).
- `cogs/storage.py` — `/pve storage` using `GET /nodes/{node}/storage`.

All four cogs register their commands into the single `/pve` group on the bot.

## Development

```bash
python -m venv venv && . venv/bin/activate
pip install -e core -e bots/proxmox pytest pytest-asyncio
pytest bots/proxmox
```

Conventions: [docs/CONTRIBUTING.md](../../docs/CONTRIBUTING.md).

## License

MIT
