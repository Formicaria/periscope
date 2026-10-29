"""Every embed the community service posts, built from plain data so the wording can be tested without Discord.

Times are Discord timestamps (<t:…:F>, <t:…:R>) so every member reads them in their own time zone and the
"in 2 hours" ticks down by itself.
"""

from __future__ import annotations

import time
from typing import Any

import discord
from periscope import Severity, lab_embed, truncate
from periscope.messages import MessageKind, register

from .diagnose import Cause
from .timeparse import human_span

MAX_DESC = 4096


def ts(epoch: float, style: str = "F") -> str:
    return f"<t:{int(epoch)}:{style}>"


def affects_words(names: list[str]) -> str:
    if not names:
        return "everything"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" and {names[-1]}"


# ----- outages -------------------------------------------------------------------------------------------
def outage_embed(cause: Cause, since: float, *, lab: str) -> discord.Embed:
    e = lab_embed(cause.title, cause.why, severity=Severity.CRITICAL, lab_name=lab)
    e.add_field(name="Affected", value=affects_words(cause.affected), inline=True)
    e.add_field(name="Down since", value=f"{ts(since, 't')} ({ts(since, 'R')})", inline=True)
    e.add_field(name="​", value="We're on it — this post updates itself when it's back.", inline=False)
    return e


def recovered_embed(cause: Cause, since: float, until: float, *, lab: str) -> discord.Embed:
    e = lab_embed(f"Resolved: {cause.title}", cause.why, severity=Severity.OK, lab_name=lab)
    e.add_field(name="Affected", value=affects_words(cause.affected), inline=True)
    e.add_field(name="Down for", value=human_span(until - since), inline=True)
    e.add_field(name="Back since", value=ts(until, "t"), inline=True)
    return e


def back_line(cause: Cause, since: float, until: float) -> str:
    return f"🟢 **{affects_words(cause.affected)}** {'is' if len(cause.affected) == 1 else 'are'} back up " \
           f"(down {human_span(until - since)})."


# ----- maintenance ---------------------------------------------------------------------------------------
PHASES = {
    "scheduled": ("🛠️", "Scheduled maintenance", Severity.WARNING),
    "live": ("🛠️", "Maintenance in progress", Severity.WARNING),
    "done": ("✅", "Maintenance complete", Severity.OK),
    "overrun": ("⏳", "Maintenance is running long", Severity.WARNING),
    "cancelled": ("❌", "Maintenance cancelled", Severity.INFO),
}


def maint_embed(entry: dict[str, Any], *, lab: str, now: float | None = None) -> discord.Embed:
    now = now if now is not None else time.time()
    phase = entry.get("phase", "scheduled")
    icon, head, sev = PHASES.get(phase, PHASES["scheduled"])
    start, end = float(entry["start"]), float(entry["end"])
    lines = [f"**{entry.get('reason') or 'Server work'}**", ""]
    lines.append(f"**Affects:** {affects_words(entry.get('affects') or [])}")
    if phase == "scheduled":
        lines.append(f"**Starts:** {ts(start)} ({ts(start, 'R')})")
        lines.append(f"**Expected back:** {ts(end)} ({ts(end, 'R')}) — about {human_span(end - start)}")
    elif phase in ("live", "overrun"):
        lines.append(f"**Started:** {ts(start, 't')} ({ts(start, 'R')})")
        lines.append(f"**Expected back:** {ts(end)} ({ts(end, 'R')})")
    elif phase == "done":
        took = float(entry.get("finished") or end) - start
        lines.append(f"**Took:** {human_span(took)} — everything should be back. If something isn't, say so here.")
    elif phase == "cancelled":
        lines.append(f"Was planned for {ts(start)} — not happening. Carry on.")
    if entry.get("note"):
        lines += ["", str(entry["note"])]
    e = lab_embed(f"{icon} {head}", truncate("\n".join(lines), MAX_DESC), severity=Severity.INFO, lab_name=lab)
    e.color = sev.color
    e.set_author(name=f"maintenance {entry.get('id', '')}".strip())
    return e


# ----- announcements -------------------------------------------------------------------------------------
def psa_embeds(title: str, body: str, *, lab: str, author: str = "") -> list[discord.Embed]:
    """A long announcement split across as many embeds as it takes (Discord: 4096 per description)."""
    chunks: list[str] = []
    rest = body.strip()
    while rest:
        if len(rest) <= MAX_DESC:
            chunks.append(rest)
            break
        cut = rest.rfind("\n\n", 0, MAX_DESC)
        cut = cut if cut > MAX_DESC // 2 else rest.rfind("\n", 0, MAX_DESC)
        cut = cut if cut > 0 else MAX_DESC
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    chunks = chunks or [""]
    out = []
    for i, chunk in enumerate(chunks):
        e = lab_embed(title if i == 0 else f"{title} (cont.)", chunk or None, severity=Severity.INFO, lab_name=lab)
        if author and i == 0:
            e.set_author(name=author)
        out.append(e)
    return out


def split_markdown(text: str) -> tuple[str, str]:
    """An outbox file → (title, body). The first '# ' line is the title; without one, the first line is."""
    lines = text.strip().splitlines()
    if not lines:
        return "Announcement", ""
    first = lines[0].strip()
    title = first.lstrip("#").strip() or "Announcement"
    return title[:250], "\n".join(lines[1:]).strip()


# ----- the board ---------------------------------------------------------------------------------------
def board_embed(rows: list[dict[str, Any]], upcoming: list[dict[str, Any]], *, lab: str, problems: list[str],
                now: float | None = None) -> discord.Embed:
    """rows: {name, state: up|down|maint|unknown, detail, since, ms}. upcoming: maintenance entries."""
    now = now if now is not None else time.time()
    down = [r for r in rows if r["state"] == "down"]
    maint = [r for r in rows if r["state"] == "maint"]
    if down:
        sev, head = Severity.CRITICAL, f"{len(down)} of {len(rows)} down"
    elif maint:
        sev, head = Severity.WARNING, "maintenance in progress"
    else:
        sev, head = Severity.OK, "everything's up"
    lines = []
    for r in rows:
        dot = {"up": "🟢", "down": "🔴", "maint": "🛠️"}.get(r["state"], "⚪")
        bits = []
        if r["state"] == "up":
            if r.get("detail"):
                bits.append(r["detail"])
            if r.get("ms") is not None:
                bits.append(f"{r['ms']} ms")
        elif r["state"] == "down":
            if r.get("since"):
                bits.append(f"down since {ts(r['since'], 'R')}")
            if r.get("why"):
                bits.append(r["why"])
        elif r["state"] == "maint":
            bits.append(f"maintenance until {ts(r['until'], 't')}" if r.get("until") else "maintenance")
        else:
            bits.append("not checked yet")
        lines.append(f"{dot} **{r['name']}**" + (f" — {' · '.join(bits)}" if bits else ""))
    e = lab_embed("Server status", "\n".join(lines) or "Nothing to watch yet.", severity=sev, lab_name=lab)
    e.title = f"{sev.emoji} Server status — {head}"
    soon = [u for u in upcoming if u.get("phase") in ("scheduled", "live", "overrun")]
    if soon:
        e.add_field(name="Planned maintenance", inline=False, value="\n".join(
            f"🛠️ {ts(float(u['start']), 'f')} ({ts(float(u['start']), 'R')}) · {human_span(float(u['end']) - float(u['start']))}"
            f" · {u.get('reason') or 'server work'} — {affects_words(u.get('affects') or [])}" for u in soon[:5]))
    if problems:
        e.add_field(name="Setup problems", value=truncate("\n".join(f"• {p}" for p in problems), 1000), inline=False)
    e.add_field(name="​", value=f"Checked {ts(now, 'R')} · outages and maintenance get posted in this channel",
                inline=False)
    return e


register(
    MessageKind("community.board", "Status board", "The live board of everything the community uses.",
                where="the PSA channel", where_env="PSA_CHANNEL_ID", group="boards"),
    MessageKind("community.outage", "Outage", "Something stayed down past the grace period — what and why.",
                where="the PSA channel", where_env="PSA_CHANNEL_ID", group="alerts"),
    MessageKind("community.maintenance", "Maintenance notice",
                "Scheduled / started / finished / cancelled maintenance.", where="the PSA channel",
                where_env="PSA_CHANNEL_ID", group="alerts"),
)
