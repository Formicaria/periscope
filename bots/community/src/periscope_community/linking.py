"""Account linking without Discord in the way: one-time codes, reading what a Minecraft chat bridge relays, the
book of who is who, and the plex.tv sign-in.

Minecraft: nothing on the game side has to change. Every server already relays in-game chat into a Discord
channel through its own bridge bot, so a code typed in game shows up in Discord *as said by that player* — which
is the proof. The member asks for a code with /link minecraft, types `link CODE` in game, and the bridge message
that carries it ties the two accounts together.

Plex: the member signs in on plex.tv's own page (the PIN flow every Plex companion app uses). periscope learns the
account's id and name, checks it against the server's share list, and throws the sign-in token away.
"""

from __future__ import annotations

import re
import secrets
import time
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"        # no 0/O, 1/I/L — people read these off a screen
CODE_LEN = 6
CODE_TTL_S = 15 * 60
MC_NAME = re.compile(r"^[A-Za-z0-9_]{3,16}$")
CODE_RE = re.compile(r"(?i)(?<![A-Za-z0-9])link\W{0,3}([A-Za-z0-9]{%d})(?![A-Za-z0-9])" % CODE_LEN)
_DECOR = re.compile(r"[*~`]+")                              # markdown bold/italic/code a bridge may wrap names in
_ESCAPED = re.compile(r"\\([_*~`|>\\])")                     # bridges escape markdown in names: Steve\_99
_BRACKETED = re.compile(r"^\s*[<\[(]\s*([A-Za-z0-9_]{3,16})\s*[>\])]\s*:?\s*(.*)$", re.S)
_SEPARATED = re.compile(r"^\s*([A-Za-z0-9_]{3,16})\s*(?::|»|›|>|\||-)\s*(.*)$", re.S)
_FIRST = re.compile(r"^\s*[<\[(]?\s*([A-Za-z0-9_]{3,16})\b")


# ----- reading the bridges -----------------------------------------------------------------------------------
def find_code(text: str) -> str | None:
    m = CODE_RE.search(text or "")
    return m.group(1).upper() if m else None


def _plain(text: str) -> str:
    return _DECOR.sub("", _ESCAPED.sub(lambda m: m.group(1) if m.group(1) == "_" else "", text or ""))


def chat_line(text: str) -> tuple[str, str] | None:
    """'<Steve> hi', '**Steve**: hi', 'Steve » hi', '[Steve] hi' → ('Steve', 'hi'); anything else → None."""
    clean = _plain(text)
    for rx in (_BRACKETED, _SEPARATED):
        m = rx.match(clean)
        if m and m.group(2).strip():
            return m.group(1), m.group(2).strip()
    return None


def speaker(text: str, *, author: str = "", webhook: bool = False) -> str:
    """Who said this bridge message. A webhook bridge posts under the player's name; a bot bridge puts the name
    in the text."""
    if webhook:
        return next((t for t in re.split(r"[^A-Za-z0-9_]+", author or "") if MC_NAME.match(t)), "")
    line = chat_line(text)
    return line[0] if line else ""


def first_word(text: str) -> str:
    """The player a status line is about — 'Steve joined the game', 'Steve left', 'Steve disconnected.'"""
    m = _FIRST.match(_plain(text))
    return m.group(1) if m else ""


def short(resource: str) -> str:
    """'Minecraft: Horror' → 'Horror' (for messages that already say 'server')."""
    head, _, tail = resource.partition(":")
    return tail.strip() if tail.strip() and head.strip().lower() in ("minecraft", "mc") else resource


# ----- the book ----------------------------------------------------------------------------------------------
class LinkBook:
    """Who is who, kept in the service's state:

        links:  {discord id: {"mc": {name lower: {"name", "servers": [...], "since"}}, "plex": {"id", "name", "since"}}}
        codes:  {code: {"uid", "exp"}}
    """

    def __init__(self, state: Any):
        self.state = state

    # links
    def all(self) -> dict[str, dict[str, Any]]:
        return dict(self.state.get("links") or {})

    def get(self, uid: int) -> dict[str, Any]:
        return dict(self.all().get(str(uid)) or {})

    def _put(self, uid: int, rec: dict[str, Any]) -> None:
        links = self.all()
        if rec.get("mc") or rec.get("plex"):
            links[str(uid)] = rec
        else:
            links.pop(str(uid), None)
        self.state.set("links", links)

    def mc_owner(self, name: str) -> int | None:
        low = (name or "").lower()
        if not low:
            return None
        for uid, rec in self.all().items():
            if low in (rec.get("mc") or {}):
                return int(uid)
        return None

    def link_mc(self, uid: int, name: str, server: str, now: float | None = None) -> int | None:
        """Tie `name` to `uid` (on `server`). Returns the member it was taken from, if it belonged to someone else."""
        low = name.lower()
        prev = self.mc_owner(name)
        if prev is not None and prev != uid:
            old = self.get(prev)
            (old.get("mc") or {}).pop(low, None)
            self._put(prev, old)
        rec = self.get(uid)
        mc = dict(rec.get("mc") or {})
        entry = dict(mc.get(low) or {"name": name, "servers": [], "since": now or time.time()})
        entry["name"] = name
        if server not in entry["servers"]:
            entry["servers"] = list(entry["servers"]) + [server]
        mc[low] = entry
        rec["mc"] = mc
        self._put(uid, rec)
        return prev if prev not in (None, uid) else None

    def add_server(self, uid: int, name: str, server: str) -> bool:
        rec = self.get(uid)
        entry = (rec.get("mc") or {}).get(name.lower())
        if entry is None or server in entry.get("servers", []):
            return False
        entry["servers"] = list(entry.get("servers", [])) + [server]
        self._put(uid, rec)
        return True

    def servers(self, uid: int) -> list[str]:
        out: list[str] = []
        for entry in (self.get(uid).get("mc") or {}).values():
            out += [s for s in entry.get("servers", []) if s not in out]
        return out

    def unlink_mc(self, uid: int) -> list[dict[str, Any]]:
        rec = self.get(uid)
        gone = list((rec.pop("mc", None) or {}).values())
        self._put(uid, rec)
        return gone

    def link_plex(self, uid: int, account_id: int, name: str, now: float | None = None) -> int | None:
        prev = self.plex_owner(account_id)
        if prev is not None and prev != uid:
            old = self.get(prev)
            old.pop("plex", None)
            self._put(prev, old)
        rec = self.get(uid)
        rec["plex"] = {"id": int(account_id), "name": name, "since": now or time.time()}
        self._put(uid, rec)
        return prev if prev not in (None, uid) else None

    def plex_owner(self, account_id: int) -> int | None:
        for uid, rec in self.all().items():
            if int((rec.get("plex") or {}).get("id") or 0) == int(account_id):
                return int(uid)
        return None

    def unlink_plex(self, uid: int) -> dict[str, Any] | None:
        rec = self.get(uid)
        gone = rec.pop("plex", None)
        self._put(uid, rec)
        return gone

    # codes
    def issue(self, uid: int, now: float | None = None) -> str:
        """A fresh code for `uid` (any older one of theirs stops working)."""
        now = now or time.time()
        codes = {c: v for c, v in (self.state.get("codes") or {}).items()
                 if float(v.get("exp", 0)) > now and int(v.get("uid", 0)) != uid}
        code = ""
        while not code or code in codes:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        codes[code] = {"uid": uid, "exp": now + CODE_TTL_S}
        self.state.set("codes", codes)
        return code

    def redeem(self, code: str, now: float | None = None) -> int | None:
        """The member a live code belongs to (the code is used up), else None."""
        now = now or time.time()
        codes = dict(self.state.get("codes") or {})
        hit = codes.pop((code or "").upper(), None)
        codes = {c: v for c, v in codes.items() if float(v.get("exp", 0)) > now}
        self.state.set("codes", codes)
        if hit is None or float(hit.get("exp", 0)) <= now:
            return None
        return int(hit["uid"])


# ----- plex.tv -----------------------------------------------------------------------------------------------
PLEX_TV = "https://plex.tv"
PRODUCT = "periscope"


def plex_headers(client_id: str, token: str | None = None) -> dict[str, str]:
    h = {"Accept": "application/json", "X-Plex-Product": PRODUCT, "X-Plex-Client-Identifier": client_id}
    if token:
        h["X-Plex-Token"] = token
    return h


def auth_url(client_id: str, code: str) -> str:
    return (f"https://app.plex.tv/auth#?clientID={quote(client_id)}&code={quote(code)}"
            f"&context%5Bdevice%5D%5Bproduct%5D={quote(PRODUCT)}")


def parse_users(xml_text: str, machine_id: str = "") -> dict[int, dict[str, Any]]:
    """plex.tv /api/users (the owner's share list) → {account id: {"name", "pending"}} for people who have this
    server (any server, when the machine id isn't known). `pending` = invited but not accepted yet."""
    out: dict[int, dict[str, Any]] = {}
    root = ET.fromstring(xml_text)
    for u in root.iter("User"):
        try:
            uid = int(u.get("id") or 0)
        except ValueError:
            continue
        servers = [s for s in u.iter("Server") if not machine_id or s.get("machineIdentifier") == machine_id]
        if not uid or (machine_id and not servers and u.get("home") != "1"):
            continue
        pending = bool(servers) and all(s.get("pending") == "1" for s in servers)
        out[uid] = {"name": u.get("username") or u.get("title") or str(uid), "pending": pending}
    return out


async def plex_pin(session: Any, client_id: str) -> tuple[int, str]:
    async with session.post(f"{PLEX_TV}/api/v2/pins?strong=true", headers=plex_headers(client_id)) as r:
        r.raise_for_status()
        data = await r.json()
    return int(data["id"]), str(data["code"])


async def plex_pin_token(session: Any, client_id: str, pin_id: int) -> str | None:
    async with session.get(f"{PLEX_TV}/api/v2/pins/{pin_id}", headers=plex_headers(client_id)) as r:
        if r.status == 404:
            return None
        r.raise_for_status()
        data = await r.json()
    return data.get("authToken") or None


async def plex_account(session: Any, client_id: str, token: str) -> dict[str, Any]:
    async with session.get(f"{PLEX_TV}/api/v2/user", headers=plex_headers(client_id, token)) as r:
        r.raise_for_status()
        data = await r.json()
    return {"id": int(data["id"]), "name": data.get("username") or data.get("title") or str(data["id"])}


async def plex_access(session: Any, client_id: str, owner_token: str, plex_url: str,
                      account_id: int) -> tuple[bool, str]:
    """Does this Plex account have this server? (True, "") / (False, "pending" | "no")."""
    owner = await plex_account(session, client_id, owner_token)
    if owner["id"] == account_id:
        return True, ""
    machine = ""
    if plex_url:
        try:
            async with session.get(f"{plex_url.rstrip('/')}/identity",
                                   headers=plex_headers(client_id, owner_token)) as r:
                machine = str(((await r.json()).get("MediaContainer") or {}).get("machineIdentifier") or "")
        except Exception:  # noqa: BLE001 — fall back to "shared any server of the owner's"
            machine = ""
    async with session.get(f"{PLEX_TV}/api/users", headers={**plex_headers(client_id, owner_token),
                                                           "Accept": "application/xml"}) as r:
        r.raise_for_status()
        users = parse_users(await r.text(), machine)
    hit = users.get(account_id)
    if hit is None:
        return False, "no"
    return (False, "pending") if hit["pending"] else (True, "")
