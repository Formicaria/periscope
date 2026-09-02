"""Interactive first-run wizard: `periscope init <bot>`.

Asks only for what is still empty, verifies every credential against the real service before
writing it, creates the Discord channels/roles it needs (with your permission), and remembers the
shared answers (server, channels, roles, lab name) in periscope.json so the second bot asks nothing
it already knows.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DISCORD_API = "https://discord.com/api/v10"
# View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Manage Messages,
# Mention Everyone (224256) + Manage Channels (16) + Manage Roles (268435456) so the wizard can create the layout.
INVITE_PERMS = 224256 + 16 + 268435456

LAYOUT = {
    "categories": [
        ("🧪 LAB STATUS", ["lab-status", "lab-alerts", "media", "network", "backups"]),
        ("🕹️ LAB CONTROL", ["lab-cmd"]),
    ],
    "roles": [("lab-admin", 0xE67E22, False), ("lab-oncall", 0xE74C3C, True), ("bots", 0x5865F2, False)],
}
GITHUB_LAYOUT = {"category": "Formicaria", "channels": ["formicaria-git", "formicaria-ci"],
                 "roles": [("formicaria-dev", 0x2ECC71, True)]}

# Which shared channel/role feeds which env var, per bot.
SHARED = {
    "STATUS_CHANNEL_ID": "lab-status",
    "ALERT_CHANNEL_ID": "lab-alerts",
    "ALERT_ROLE_ID": "@lab-oncall",
    "ADMIN_ROLE_IDS": "@lab-admin",
}
BOT_SHARED_OVERRIDES = {
    "arr": {"MEDIA_CHANNEL_ID": "media"},
    "unifi": {"ALERT_CHANNEL_ID": "network"},
    "github": {"GITHUB_FEED_CHANNEL_ID": "formicaria-git", "GITHUB_CI_CHANNEL_ID": "formicaria-ci", "ALERT_CHANNEL_ID": "formicaria-ci",
               "ALERT_ROLE_ID": "@formicaria-dev", "GITHUB_CI_FAILURE_ROLE_ID": "@formicaria-dev"},
}


# ----------------------------------------------------------------------------- tiny helpers
class Abort(Exception):
    pass


def say(msg: str = "") -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    say(f"  ✔ {msg}")


def warn(msg: str) -> None:
    say(f"  !! {msg}")


def ask(prompt: str, default: str = "", secret: bool = False, required: bool = True) -> str:
    while True:
        shown = f" [{default}]" if default and not secret else (" [set]" if default and secret else "")
        try:
            raw = (getpass.getpass if secret else input)(f"  {prompt}{shown}: ")
        except (EOFError, KeyboardInterrupt):
            raise Abort from None
        raw = raw.strip()
        if raw:
            return raw
        if default:
            return default
        if not required:
            return ""
        say("    (required)")


def yes(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    try:
        raw = input(f"  {prompt} [{d}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        raise Abort from None
    return default if not raw else raw.startswith("y")


def http(method: str, url: str, headers: dict | None = None, body: dict | None = None,
         verify: bool = True, timeout: int = 15) -> tuple[int, dict | list | str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": "periscope-wizard", **(headers or {})})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    ctx = None
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            text = r.read().decode(errors="replace")
            status = r.status
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        status = e.code
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, ConnectionError, OSError) as e:
        return 0, str(e)
    try:
        return status, json.loads(text) if text else {}
    except json.JSONDecodeError:
        return status, text


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            m = re.match(r"^\s*([A-Z0-9_]+)=(.*)$", line)
            if m:
                out[m.group(1)] = m.group(2).split("  #", 1)[0].strip()
    return out


def write_env(example: Path, target: Path, values: dict[str, str]) -> None:
    """Rewrite the example file with values filled in, keeping its comments as documentation."""
    lines, seen = [], set()
    for line in example.read_text().splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if m and m.group(1) in values:
            # systemd EnvironmentFile keeps inline "# comments" as part of the value, so drop them
            k = m.group(1)
            lines.append(f"{k}={values[k]}")
            seen.add(k)
        else:
            lines.append(line)
    extra = [k for k in values if k not in seen and values[k]]
    if extra:
        lines.append("")
        for k in extra:
            lines.append(f"{k}={values[k]}")
    target.write_text("\n".join(lines) + "\n")
    os.chmod(target, 0o600)


# ----------------------------------------------------------------------------- discord
class Discord:
    def __init__(self, token: str):
        self.h = {"Authorization": f"Bot {token}"}

    def get(self, path: str):
        return http("GET", DISCORD_API + path, self.h)

    def post(self, path: str, body: dict):
        return http("POST", DISCORD_API + path, self.h, body)

    def me(self) -> dict | None:
        st, d = self.get("/users/@me")
        return d if st == 200 and isinstance(d, dict) else None

    def guilds(self) -> list[dict]:
        st, d = self.get("/users/@me/guilds")
        return d if st == 200 and isinstance(d, list) else []

    def channels(self, gid: str) -> list[dict]:
        st, d = self.get(f"/guilds/{gid}/channels")
        return d if st == 200 and isinstance(d, list) else []

    def roles(self, gid: str) -> list[dict]:
        st, d = self.get(f"/guilds/{gid}/roles")
        return d if st == 200 and isinstance(d, list) else []


def norm(s: str) -> str:
    return s.strip().lower()


def step_discord(env: dict, shared: dict, bot: str) -> Discord:
    say("\n── Discord")
    while True:
        token = env.get("DISCORD_TOKEN") or ask("Bot token (developer portal → your app → Bot → Reset Token)", secret=True)
        d = Discord(token)
        me = d.me()
        if me:
            env["DISCORD_TOKEN"] = token
            ok(f"token works — logged in as {me['username']} (app id {me['id']})")
            break
        warn("Discord rejected that token")
        env["DISCORD_TOKEN"] = ""

    invite = (f"https://discord.com/oauth2/authorize?client_id={me['id']}"
              f"&scope=bot%20applications.commands&permissions={INVITE_PERMS}")
    while True:
        gl = d.guilds()
        want = shared.get("GUILD_ID") or env.get("GUILD_ID")
        if want and any(g["id"] == want for g in gl):
            g = next(g for g in gl if g["id"] == want)
        elif len(gl) == 1:
            g = gl[0]
        elif len(gl) > 1:
            say("  The bot is in several servers:")
            for i, g in enumerate(gl, 1):
                say(f"    {i}. {g['name']} ({g['id']})")
            g = gl[int(ask("Which one", "1")) - 1]
        else:
            say(f"  The bot is not in a server yet. Invite it with:\n\n    {invite}\n")
            input("  Press Enter once it has joined…")
            continue
        break
    env["GUILD_ID"] = shared["GUILD_ID"] = g["id"]
    ok(f"server: {g['name']} ({g['id']})")
    return d


def step_layout(d: Discord, env: dict, shared: dict, bot: str) -> None:
    say("\n── Channels and roles")
    gid = env["GUILD_ID"]
    chans = {norm(c["name"]): c for c in d.channels(gid)}
    roles = {norm(r["name"]): r for r in d.roles(gid)}
    needed_chan = list(dict.fromkeys(v for v in {**SHARED, **BOT_SHARED_OVERRIDES.get(bot, {})}.values() if not v.startswith("@")))
    needed_role = list(dict.fromkeys(v[1:] for v in {**SHARED, **BOT_SHARED_OVERRIDES.get(bot, {})}.values() if v.startswith("@")))
    missing_c = [c for c in needed_chan if c not in chans]
    missing_r = [r for r in needed_role if r not in roles]

    if missing_c or missing_r:
        say(f"  Missing: {' '.join('#' + c for c in missing_c)} {' '.join('@' + r for r in missing_r)}".rstrip())
        if yes("Create them now (needs Manage Channels + Manage Roles on the bot)?"):
            cats = {norm(c["name"]): c for c in chans.values() if c["type"] == 4}
            layouts = [(cat, names) for cat, names in LAYOUT["categories"]]
            role_specs = list(LAYOUT["roles"])
            if bot == "github":
                layouts.append((GITHUB_LAYOUT["category"], GITHUB_LAYOUT["channels"]))
                role_specs += GITHUB_LAYOUT["roles"]
            for name, color, mentionable in role_specs:
                if name in missing_r:
                    st, r = d.post(f"/guilds/{gid}/roles", {"name": name, "color": color, "mentionable": mentionable})
                    if st in (200, 201):
                        roles[name] = r
                        ok(f"@{name}")
                    else:
                        warn(f"could not create @{name}: {st} {r}")
            for cat_name, names in layouts:
                wanted = [n for n in names if n in missing_c]
                if not wanted:
                    continue
                cat = cats.get(norm(cat_name))
                if cat is None:
                    st, cat = d.post(f"/guilds/{gid}/channels", {"name": cat_name, "type": 4})
                    if st not in (200, 201):
                        warn(f"could not create category {cat_name}: {st} {cat}")
                        cat = None
                    else:
                        cats[norm(cat_name)] = cat
                for n in wanted:
                    body = {"name": n, "type": 0}
                    if cat:
                        body["parent_id"] = cat["id"]
                    st, c = d.post(f"/guilds/{gid}/channels", body)
                    if st in (200, 201):
                        chans[n] = c
                        ok(f"#{n}")
                    else:
                        warn(f"could not create #{n}: {st} {c}")
        else:
            say("  Fine — create them yourself, then re-run `periscope init` (or paste IDs below).")

    mapping = {**SHARED, **BOT_SHARED_OVERRIDES.get(bot, {})}
    overrides = BOT_SHARED_OVERRIDES.get(bot, {})
    for var, target in mapping.items():
        # a bot-specific target (e.g. unifi alerts → #network) beats a value inherited from the shared defaults
        inherited = var in overrides and env.get(var) and env.get(var) == shared.get(var)
        if env.get(var) and not inherited:
            continue
        if target.startswith("@"):
            r = roles.get(target[1:])
            if r:
                env[var] = r["id"]
                ok(f"{var} ← @{r['name']}")
            else:
                env[var] = ask(f"{var} (role id for {target}, blank to skip)", required=False)
        else:
            c = chans.get(target)
            if c:
                env[var] = c["id"]
                ok(f"{var} ← #{c['name']}")
            else:
                env[var] = ask(f"{var} (channel id for #{target})")
    for k in ("STATUS_CHANNEL_ID", "ALERT_CHANNEL_ID", "ALERT_ROLE_ID", "ADMIN_ROLE_IDS"):
        if env.get(k) and bot != "github":
            shared[k] = env[k]


def step_identity(env: dict, shared: dict) -> None:
    say("\n── This lab")
    default = shared.get("LAB_NAME") or (env.get("LAB_NAME") if env.get("LAB_NAME") not in ("", "my-lab", "lab") else "") \
        or socket.gethostname()
    env["LAB_NAME"] = shared["LAB_NAME"] = ask("Lab name shown in every embed footer", default)
    env["LAB_COLOR"] = shared["LAB_COLOR"] = ask("Accent color (hex, no #)", shared.get("LAB_COLOR") or env.get("LAB_COLOR") or "5865F2")


# ----------------------------------------------------------------------------- per-bot service checks
def check_url_reachable(url: str, verify: bool) -> bool:
    st, _ = http("GET", url, verify=verify, timeout=8)
    return st != 0


def step_proxmox(env: dict) -> None:
    say("\n── Proxmox VE")
    say("  Create a token on any PVE node (prints the secret once):\n"
        "    pveum user add periscope@pve --comment 'periscope Discord bot'\n"
        "    pveum role add Periscope -privs 'VM.Audit VM.PowerMgmt Datastore.Audit Sys.Audit'\n"
        "    pveum acl modify / -user periscope@pve -role Periscope\n"
        "    pveum user token add periscope@pve discord --privsep 0\n")
    while True:
        url = ask("PVE URL", env.get("PVE_URL") if "example" not in env.get("PVE_URL", "") and "PVE_HOST" not in env.get("PVE_URL", "") else "https://192.168.1.10:8006").rstrip("/")
        tid = ask("Token id", env.get("PVE_TOKEN_ID") or "periscope@pve!discord")
        sec = ask("Token secret", env.get("PVE_TOKEN_SECRET"), secret=True)
        verify = env.get("PVE_VERIFY_SSL", "false").lower() == "true"
        st, d = http("GET", f"{url}/api2/json/version", {"Authorization": f"PVEAPIToken={tid}={sec}"}, verify=verify)
        if st == 200 and isinstance(d, dict):
            ok(f"Proxmox VE {d.get('data', {}).get('version', '?')} answered")
            env.update(PVE_URL=url, PVE_TOKEN_ID=tid, PVE_TOKEN_SECRET=sec)
            return
        warn(f"PVE check failed ({st or 'unreachable'}: {str(d)[:120]})")
        if not yes("Try again?"):
            env.update(PVE_URL=url, PVE_TOKEN_ID=tid, PVE_TOKEN_SECRET=sec)
            return


def arr_check(url: str, key: str, verify: bool, api: str = "v3") -> tuple[bool, str]:
    st, d = http("GET", f"{url}/api/{api}/system/status", {"X-Api-Key": key}, verify=verify)
    if st == 200 and isinstance(d, dict):
        return True, f"{d.get('appName', '')} {d.get('version', '')}".strip()
    return False, f"{st or 'unreachable'}"


def step_arr(env: dict) -> None:
    say("\n── *arr stack, download clients, media servers  (blank URL = skip that service)")
    verify = env.get("VERIFY_SSL", "true").lower() != "false"
    for name, api in (("SONARR", "v3"), ("RADARR", "v3"), ("LIDARR", "v1"), ("PROWLARR", "v1")):
        while True:
            url = ask(f"{name.title()} URL", env.get(f"{name}_URL"), required=False).rstrip("/")
            if not url:
                env[f"{name}_URL"] = ""
                break
            key = ask(f"{name.title()} API key (Settings → General → Security)", env.get(f"{name}_API_KEY"), secret=True)
            good, info = arr_check(url, key, verify, api)
            if good:
                ok(f"{info} answered")
                env[f"{name}_URL"], env[f"{name}_API_KEY"] = url, key
                break
            warn(f"{name.title()} check failed ({info})")
            if not yes("Try again?"):
                env[f"{name}_URL"], env[f"{name}_API_KEY"] = url, key
                break
    disp = load_env(Path("/opt/displexia/.env"))
    if disp.get("PLEX_TOKEN") and not env.get("PLEX_TOKEN"):
        env["PLEX_TOKEN"] = disp["PLEX_TOKEN"]
        env.setdefault("PLEX_URL", disp.get("PLEX_URL", ""))
        if not env["PLEX_URL"]:
            env["PLEX_URL"] = disp.get("PLEX_URL", "")
        ok("reusing the Plex token from /opt/displexia/.env")
    url = ask("Plex URL", env.get("PLEX_URL"), required=False).rstrip("/")
    env["PLEX_URL"] = url
    if url:
        tok = ask("Plex token (X-Plex-Token)", env.get("PLEX_TOKEN"), secret=True, required=False)
        env["PLEX_TOKEN"] = tok
        if tok:
            st, _ = http("GET", f"{url}/identity?X-Plex-Token={urllib.parse.quote(tok)}", {"Accept": "application/json"}, verify=verify)
            ok("Plex answered") if st == 200 else warn(f"Plex check failed ({st or 'unreachable'}) — saved anyway")
    for name, label in (("QBIT", "qBittorrent Web UI"), ("SABNZBD", "SABnzbd")):
        url = ask(f"{label} URL", env.get(f"{name}_URL"), required=False).rstrip("/")
        env[f"{name}_URL"] = url
        if url and name == "QBIT":
            key = ask("qBittorrent API key (≥5.2: Options → Web UI → API keys; blank = use user/password)",
                      env.get("QBIT_API_KEY"), secret=True, required=False)
            env["QBIT_API_KEY"] = key
            if key:
                st, _ = http("GET", f"{url}/api/v2/app/version", {"Authorization": f"Bearer {key}"}, verify=verify)
                ok("qBittorrent answered") if st == 200 else warn(f"qBittorrent rejected the key ({st or 'unreachable'}) — saved anyway")
                env["QBIT_USER"], env["QBIT_PASS"] = "", ""
            else:
                env["QBIT_USER"] = ask("qBittorrent user", env.get("QBIT_USER"), required=False)
                env["QBIT_PASS"] = ask("qBittorrent password", env.get("QBIT_PASS"), secret=True, required=False)
        elif url:
            env["SABNZBD_API_KEY"] = ask("SABnzbd API key", env.get("SABNZBD_API_KEY"), secret=True)
    if not env.get("WEBHOOK_SECRET"):
        env["WEBHOOK_SECRET"] = secrets.token_hex(24)
        ok("generated WEBHOOK_SECRET")
    say(f"\n  Point Sonarr/Radarr webhooks (Settings → Connect → Webhook) at:\n"
        f"    http://<this-host>:{env.get('WEBHOOK_PORT', '8082')}/<sonarr|radarr|lidarr|prowlarr>?token={env['WEBHOOK_SECRET']}")


def step_unifi(env: dict) -> None:
    say("\n── UniFi  (make a local-only, read-only admin: Settings → Admins → Add → 'Restrict to local access')")
    while True:
        url = ask("Controller URL (UDM/UCG: https://<ip>  ·  self-hosted: https://<host>:8443)",
                  env.get("UNIFI_URL") if "192.168.1.1" not in env.get("UNIFI_URL", "") and "CONTROLLER_IP" not in env.get("UNIFI_URL", "") else "").rstrip("/")
        is_os = yes("Is this a UniFi OS console (UDM / UDR / UCG / Cloud Key Gen2+)?", env.get("UNIFI_IS_UNIFI_OS", "true") == "true")
        user = ask("Username", env.get("UNIFI_USER") or "periscope")
        pw = ask("Password", env.get("UNIFI_PASS"), secret=True)
        verify = env.get("VERIFY_SSL", "false").lower() == "true"
        path = "/api/auth/login" if is_os else "/api/login"
        st, d = http("POST", url + path, body={"username": user, "password": pw}, verify=verify)
        env.update(UNIFI_URL=url, UNIFI_USER=user, UNIFI_PASS=pw, UNIFI_IS_UNIFI_OS="true" if is_os else "false")
        if st == 200:
            ok("UniFi login works")
            return
        warn(f"UniFi login failed ({st or 'unreachable'})")
        if not yes("Try again?"):
            return


def step_github(env: dict) -> None:
    say("\n── GitHub")
    env["GITHUB_ORG"] = ask("Organization", env.get("GITHUB_ORG") or "Formicaria")
    while True:
        tok = ask("Fine-grained PAT, read-only (Settings → Developer settings → Fine-grained tokens)",
                  env.get("GITHUB_TOKEN"), secret=True, required=False)
        if not tok:
            warn("no token: /gh commands and polling stay off; webhooks still work")
            env["GITHUB_TOKEN"] = ""
            break
        st, d = http("GET", "https://api.github.com/user", {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
        if st == 200 and isinstance(d, dict):
            ok(f"GitHub token works — {d.get('login')}")
            env["GITHUB_TOKEN"] = tok
            break
        warn(f"GitHub rejected the token ({st})")
        if not yes("Try again?"):
            env["GITHUB_TOKEN"] = tok
            break
    if not env.get("WEBHOOK_SECRET"):
        env["WEBHOOK_SECRET"] = secrets.token_hex(24)
        ok("generated WEBHOOK_SECRET")
    env["GITHUB_POLL_ENABLED"] = "true" if env.get("GITHUB_TOKEN") else "false"
    if env.get("GITHUB_TOKEN"):
        ok("polling on — the feed works with no inbound port; the webhook below is optional (instant delivery)")
    say(f"\n  Optional org webhook: github.com/organizations/{env['GITHUB_ORG']}/settings/hooks → Add webhook\n"
        f"    Payload URL   https://<public-host>/github   (this bot listens on port {env.get('WEBHOOK_PORT', '8084')})\n"
        f"    Content type  application/json\n"
        f"    Secret        {env['WEBHOOK_SECRET']}\n"
        f"    Events        Send me everything")


def step_prometheus(env: dict) -> None:
    say("\n── Prometheus / Alertmanager / Grafana")
    for var, label, path in (("PROM_URL", "Prometheus URL", "/-/ready"), ("ALERTMANAGER_URL", "Alertmanager URL", "/-/ready")):
        while True:
            url = ask(label, env.get(var) if "192.168" not in env.get(var, "") else "").rstrip("/")
            st, _ = http("GET", url + path)
            env[var] = url
            if st == 200:
                ok(f"{label.split()[0]} ready")
                break
            warn(f"{label.split()[0]} check failed ({st or 'unreachable'})")
            if not yes("Try again?"):
                break
    env["GRAFANA_URL"] = ask("Grafana URL (blank = no panel screenshots)", env.get("GRAFANA_URL") if "192.168" not in env.get("GRAFANA_URL", "") else "", required=False).rstrip("/")
    if env["GRAFANA_URL"]:
        env["GRAFANA_TOKEN"] = ask("Grafana service-account token", env.get("GRAFANA_TOKEN"), secret=True, required=False)
    if not env.get("WEBHOOK_SECRET"):
        env["WEBHOOK_SECRET"] = secrets.token_hex(24)
        ok("generated WEBHOOK_SECRET")
    say(f"\n  Alertmanager receiver URL:  http://<this-host>:{env.get('WEBHOOK_PORT', '8081')}/alertmanager?token={env['WEBHOOK_SECRET']}")


SERVICE_STEPS = {"proxmox": step_proxmox, "arr": step_arr, "unifi": step_unifi, "github": step_github, "prometheus": step_prometheus}


# ----------------------------------------------------------------------------- main
def main() -> int:
    if len(sys.argv) != 3:
        say("usage: python -m periscope.wizard <repo-dir> <bot>")
        return 2
    root, bot = Path(sys.argv[1]), sys.argv[2]
    bot_dir = root / "bots" / bot
    example, target = bot_dir / ".env.example", bot_dir / ".env"
    if not example.exists():
        say(f"unknown bot '{bot}'")
        return 2
    if not sys.stdin.isatty():
        say("periscope init is interactive — run it from a terminal (or copy .env.example to .env and edit it by hand)")
        return 2

    shared_path = root / "periscope.json"
    shared = json.loads(shared_path.read_text()) if shared_path.exists() else {}
    env = load_env(example)
    env.update({k: v for k, v in load_env(target).items() if v})
    for k in ("GUILD_ID", "STATUS_CHANNEL_ID", "ALERT_CHANNEL_ID", "ALERT_ROLE_ID", "ADMIN_ROLE_IDS", "LAB_NAME", "LAB_COLOR"):
        if not env.get(k) and shared.get(k) and not (bot == "github" and k in ("ALERT_CHANNEL_ID", "ALERT_ROLE_ID", "LAB_NAME")):
            env[k] = shared[k]

    say(f"\nperiscope · {bot} — first-run setup. Enter keeps a [default]; Ctrl-C aborts without writing.")
    try:
        d = step_discord(env, shared, bot)
        step_layout(d, env, shared, bot)
        if bot != "github":
            step_identity(env, shared)
        elif not env.get("LAB_NAME") or env["LAB_NAME"] in ("THE LAB", "my-lab", "lab"):
            env["LAB_NAME"] = env.get("GITHUB_ORG") or "Formicaria"
        SERVICE_STEPS.get(bot, lambda e: None)(env)
    except Abort:
        say("\n  aborted — nothing written")
        return 1

    write_env(example, target, env)
    shared_path.write_text(json.dumps(shared, indent=2) + "\n")
    ok(f"wrote {target}")

    if yes(f"\nEnable and start periscope@{bot} now?"):
        (bot_dir / "data").mkdir(exist_ok=True)
        r = subprocess.run(["systemctl", "enable", "--now", f"periscope@{bot}"], capture_output=True, text=True)
        if r.returncode == 0:
            subprocess.run(["systemctl", "restart", f"periscope@{bot}"])
            ok(f"periscope@{bot} running — follow it with: periscope logs {bot}")
        else:
            warn(f"systemctl failed: {r.stderr.strip()} — run: periscope enable {bot}")
    else:
        say(f"  Later: periscope enable {bot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
