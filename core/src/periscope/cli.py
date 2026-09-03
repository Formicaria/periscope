"""`periscope <verb>` implementation for the verbs that need the config store (the bash wrapper handles systemd)."""

from __future__ import annotations

import asyncio
import getpass
import json
import socket
import sys
import time
from pathlib import Path

from .config import Settings, env_scope
from .registry import discover
from .store import Store, is_secret_key

STATE_ICON = {"running": "●", "connecting": "◐", "pending": "◐", "error": "✖", "skipped": "○", "disabled": "·"}


def _runtime_status(root: Path) -> dict:
    p = root / "data" / "runtime.json"
    try:
        st = json.loads(p.read_text())
        if time.time() - p.stat().st_mtime > 60:
            st["stale"] = True
        return st
    except (OSError, ValueError):
        return {}


def _say(msg: str = "") -> None:
    print(msg, flush=True)


# ----- verbs ---------------------------------------------------------------------------------------
def cmd_list(store: Store, root: Path, args: list[str]) -> int:
    specs = discover()
    rt = _runtime_status(root)
    services = rt.get("services", {})
    if rt and not rt.get("stale"):
        _say(f"  runtime up {rt.get('uptime_s', 0) // 60} min · presences: " +
             ", ".join(f"{n}{'' if p.get('connected') else ' (offline)'}" for n, p in rt.get("presences", {}).items()))
    else:
        _say("  runtime not running (periscope start)")
    names = sorted(set(specs) | set(store.services), key=lambda n: (specs[n].group if n in specs else "zzz", n))
    for name in names:
        cfg = store.service(name) if name in store.services else {"enabled": False, "presence": "-", "env": {}}
        live = services.get(name, {})
        state = live.get("state") if cfg.get("enabled") else "disabled"
        state = state or ("pending" if cfg.get("enabled") else "disabled")
        icon = STATE_ICON.get(state, "?")
        title = specs[name].title if name in specs else name + " (not installed)"
        err = f"  — {live['error']}" if live.get("error") else ""
        _say(f"  {icon} {name:<14} {state:<10} {cfg.get('presence', '-'):<10} {title}{err}")
    return 0


def cmd_status(store: Store, root: Path, args: list[str]) -> int:
    rt = _runtime_status(root)
    if not rt or rt.get("stale"):
        _say("  runtime not running")
        return 1
    _say(json.dumps(rt, indent=2))
    return 0


def cmd_enable(store: Store, root: Path, args: list[str], on: bool = True) -> int:
    specs = discover()
    if not args:
        _say("usage: periscope enable <service...>")
        return 2
    rc = 0
    for name in args:
        if name not in specs:
            _say(f"  {name}: unknown service (installed: {', '.join(sorted(specs))})")
            rc = 1
            continue
        if on:
            env = store.env_for(name)
            missing = specs[name].required_missing(env)
            if missing:
                _say(f"  {name}: missing {', '.join(missing)} — set them in the web UI (periscope web) or `periscope config {name}`")
                rc = 1
                continue
            if not store.token_for(name):
                _say(f"  {name}: presence '{store.presence_for(name)}' has no token — periscope presence token {store.presence_for(name)}")
                rc = 1
                continue
        store.set_enabled(name, on)
        _say(f"  {name} {'enabled' if on else 'disabled'}")
    store.save()
    _say("  restart to apply: periscope restart")
    return rc


def cmd_check(store: Store, root: Path, args: list[str]) -> int:
    specs = discover()
    if len(args) != 1 or args[0] not in specs:
        _say(f"usage: periscope check <service>   ({', '.join(sorted(specs))})")
        return 2
    spec = specs[args[0]]
    if spec.check is None:
        _say(f"  {spec.name}: no check available")
        return 0
    ok, msg = asyncio.run(spec.check(store.env_for(spec.name)))
    _say(f"  {'✔' if ok else '✖'} {msg}")
    return 0 if ok else 1


def cmd_config(store: Store, root: Path, args: list[str]) -> int:
    specs = discover()
    if not args:
        _say("usage: periscope config <service> [KEY=VALUE ...]")
        return 2
    name = args[0]
    if name not in specs:
        _say(f"  unknown service {name}")
        return 2
    updates = dict(a.split("=", 1) for a in args[1:] if "=" in a)
    if updates:
        store.update_service_env(name, updates)
        store.save()
        _say(f"  saved {', '.join(updates)} — restart to apply")
    cfg = store.service(name)
    _say(f"  {name}: enabled={cfg.get('enabled')} presence={cfg.get('presence')}")
    env = store.env_for(name)
    for s in specs[name].settings:
        v = env.get(s.key, "")
        shown = ("********" if v else "") if s.type == "secret" or is_secret_key(s.key) else v
        flag = " (required, MISSING)" if s.required and not v else ""
        _say(f"    {s.key}={shown}{flag}")
    return 0


def cmd_presence(store: Store, root: Path, args: list[str]) -> int:
    if not args:
        for n, p in store.presences.items():
            users = [s for s in store.services if store.presence_for(s) == n]
            _say(f"  {n:<10} token={'set' if p.get('token') else 'MISSING'}  label={p.get('label', n)}  services: {', '.join(users) or '-'}")
        _say("  periscope presence add <name> | token <name> | use <service> <name>")
        return 0
    sub = args[0]
    if sub == "add" and len(args) >= 2:
        store.presences.setdefault(args[1], {"token": "", "label": args[1]})
        store.save()
        _say(f"  added presence {args[1]} — set its token: periscope presence token {args[1]}")
        return 0
    if sub == "token" and len(args) >= 2:
        name = args[1]
        store.presences.setdefault(name, {"token": "", "label": name})
        tok = getpass.getpass(f"  bot token for presence '{name}': ").strip()
        if not tok:
            _say("  no token entered")
            return 1
        ok, who = asyncio.run(_check_token(tok))
        if not ok:
            _say(f"  Discord rejected that token ({who})")
            return 1
        store.presences[name]["token"] = tok
        store.save()
        _say(f"  ✔ {who} — restart to apply")
        return 0
    if sub == "use" and len(args) >= 3:
        svc, name = args[1], args[2]
        if name not in store.presences:
            _say(f"  unknown presence {name}")
            return 1
        store.service(svc)["presence"] = name
        store.save()
        _say(f"  {svc} now posts as presence {name} — restart to apply")
        return 0
    _say("  usage: periscope presence [add <name> | token <name> | use <service> <name>]")
    return 2


async def _check_token(token: str) -> tuple[bool, str]:
    import aiohttp

    async with aiohttp.ClientSession() as s:
        async with s.get("https://discord.com/api/v10/users/@me", headers={"Authorization": f"Bot {token}"}) as r:
            if r.status != 200:
                return False, f"HTTP {r.status}"
            d = await r.json()
            return True, f"token works — {d.get('username')} (app id {d.get('id')})"


def cmd_web(store: Store, root: Path, args: list[str]) -> int:
    web = store.web
    host = str(web.get("host") or "0.0.0.0")
    if host in ("0.0.0.0", "::", ""):
        host = socket.gethostname()
    url = str(web.get("base_url") or "").rstrip("/") or f"http://{host}:{web.get('port', 8090)}"
    _say(f"  web UI: {url}")
    rt = _runtime_status(root)
    if rt and not rt.get("stale"):
        _say(f"  runtime is up (pid {rt.get('pid')}, {int(rt.get('uptime_s', 0)) // 60} min)")
        if not web.get("oauth_client_id"):
            _say("  first sign-in uses the setup token from the log:  journalctl -u periscope | grep 'setup token' | tail -1")
    else:
        _say("  runtime is not running — periscope start")
    return 0


def cmd_init(store: Store, root: Path, args: list[str]) -> int:
    """Terminal first-run: token → presence default, pick guild, then point at the web UI."""
    _say("\nperiscope · first run. The web UI does the same with clicks (periscope web); this is the terminal path.")
    tok = getpass.getpass("  bot token (developer portal → your app → Bot → Reset Token): ").strip()
    if not tok:
        return 1
    ok, who = asyncio.run(_check_token(tok))
    if not ok:
        _say(f"  Discord rejected that token ({who})")
        return 1
    _say(f"  ✔ {who}")
    store.presences.setdefault("default", {"token": "", "label": "periscope"})
    store.presences["default"]["token"] = tok
    import aiohttp

    async def guilds() -> list[dict]:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://discord.com/api/v10/users/@me/guilds", headers={"Authorization": f"Bot {tok}"}) as r:
                return await r.json() if r.status == 200 else []

    gl = asyncio.run(guilds())
    if len(gl) == 1:
        store.lab["guild_id"] = gl[0]["id"]
        _say(f"  ✔ server: {gl[0]['name']}")
    elif gl:
        for i, g in enumerate(gl, 1):
            _say(f"    {i}. {g['name']} ({g['id']})")
        pick = input("  which server: ").strip()
        store.lab["guild_id"] = gl[int(pick) - 1]["id"] if pick.isdigit() else gl[0]["id"]
    else:
        app_id = who.split("app id ")[-1].rstrip(")")
        _say(f"  the bot is not in a server yet — invite it: https://discord.com/oauth2/authorize?client_id={app_id}"
             f"&scope=bot%20applications.commands&permissions=268659728")
    name = input(f"  lab name shown in embeds [{store.lab.get('name') or socket.gethostname()}]: ").strip()
    if name:
        store.lab["name"] = name
    store.save()
    _say("  saved. Start the runtime (periscope start) and finish in the web UI: periscope web")
    return 0


VERBS = {"list": cmd_list, "status": cmd_status, "enable": cmd_enable, "disable": lambda s, r, a: cmd_enable(s, r, a, on=False),
         "check": cmd_check, "config": cmd_config, "presence": cmd_presence, "web": cmd_web, "init": cmd_init}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        _say("usage: python -m periscope.cli <root> <verb> [args]")
        return 2
    root, verb, args = Path(argv[0]), argv[1], argv[2:]
    store = Store.load(root / "config" / "periscope.yaml")
    if not store.exists:
        from .migrate import migrate_v1
        if migrate_v1(store, root):
            store.save()
    fn = VERBS.get(verb)
    if fn is None:
        _say(f"unknown verb {verb}")
        return 2
    return fn(store, root, args)


if __name__ == "__main__":
    raise SystemExit(main())
