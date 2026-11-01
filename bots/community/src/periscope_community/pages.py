"""Pages: Markdown files that live in a channel as clean embeds (#welcome, #rules, #faq, #guides…).

Drop `welcome.md` in PAGES_DIR with the channel id in its front matter and the bot posts it on the next pass.
Edit the file and the same message is edited in place: no repost, no ping, no "new message" badge. Every `# `
heading starts a new embed, so a FAQ or a set of guides reads as separate cards.

    ---
    channel: 1566514202431193169
    color: 5A189A            (optional, hex; default is the announcement blue)
    ---
    # Welcome to the server
    Body text, Discord markdown, <#channel> mentions…

    # Second card
    …

Deleting the file leaves the post where it is; the bot only ever touches messages it posted for a page.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import discord
from periscope import Severity, lab_embed

log = logging.getLogger(__name__)

FRONT = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)
HEADING = re.compile(r"^# (?!#)(.+?)\s*$", re.M)
MAX_TITLE = 256
MAX_DESC = 4096
MAX_EMBEDS = 10
MAX_TOTAL = 5800            # Discord caps a message's embeds at 6000 characters combined; keep some headroom


@dataclass
class Page:
    name: str
    channel_id: int
    sections: list[tuple[str, str]] = field(default_factory=list)   # (title, body)
    color: int | None = None
    digest: str = ""


def parse(name: str, text: str) -> Page:
    """A page file → Page. Raises ValueError with a readable reason when the file can't be used."""
    meta: dict[str, str] = {}
    body = text
    m = FRONT.match(text.lstrip("﻿"))
    if m:
        for line in m.group(1).splitlines():
            key, _, value = line.partition(":")
            if key.strip():
                meta[key.strip().lower()] = value.strip()
        body = text.lstrip("﻿")[m.end():]
    raw = meta.get("channel", "").lstrip("<#").rstrip(">")
    if not raw.isdigit():
        raise ValueError(f"{name}: front matter needs 'channel: <channel id>'")
    color = None
    if meta.get("color"):
        try:
            color = int(meta["color"].lstrip("#"), 16)
        except ValueError as e:
            raise ValueError(f"{name}: color {meta['color']!r} isn't hex") from e
    sections: list[tuple[str, str]] = []
    marks = list(HEADING.finditer(body))
    lead = body[:marks[0].start()] if marks else body
    if lead.strip():
        sections.append(("", lead.strip()))
    for i, mk in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        sections.append((mk.group(1).strip(), body[mk.end():end].strip()))
    if not sections:
        raise ValueError(f"{name}: nothing to post")
    for title, desc in sections:
        if len(title) > MAX_TITLE:
            raise ValueError(f"{name}: heading longer than {MAX_TITLE} characters: {title[:40]}…")
        if len(desc) > MAX_DESC:
            raise ValueError(f"{name}: section {title or '(intro)'!r} is over {MAX_DESC} characters, split it")
    return Page(name=name, channel_id=int(raw), sections=sections, color=color,
                digest=hashlib.sha256(text.encode("utf-8")).hexdigest())


def embeds(page: Page, *, lab: str | None) -> list[list[discord.Embed]]:
    """Page → messages (each a list of embeds) within Discord's per-message limits. The lab footer and the
    "updated" timestamp go on the very last card only, so a page reads as one piece."""
    cards: list[discord.Embed] = []
    for i, (title, desc) in enumerate(page.sections):
        last = i == len(page.sections) - 1
        e = lab_embed(title or "", desc or None, severity=Severity.INFO, lab_name=lab if last else None,
                      color=page.color, timestamp=last)
        if not title:
            e.title = None
        cards.append(e)
    out: list[list[discord.Embed]] = [[]]
    size = 0
    for e in cards:
        n = len(e.title or "") + len(e.description or "") + len(e.footer.text or "")
        if out[-1] and (len(out[-1]) >= MAX_EMBEDS or size + n > MAX_TOTAL):
            out.append([])
            size = 0
        out[-1].append(e)
        size += n
    return out


async def sync(bot: Any, folder: Path | None, state: Any) -> None:
    """Post new pages and edit changed ones. Cheap when nothing changed: one stat + hash per file."""
    if not folder or not folder.is_dir():
        return
    known: dict[str, dict[str, Any]] = dict(state.get("pages") or {})
    changed = False
    for f in sorted(p for p in folder.glob("*.md") if p.is_file()):
        try:
            page = parse(f.stem, f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("community: page skipped — %s", e)
            continue
        rec = known.get(page.name) or {}
        if rec.get("digest") == page.digest and rec.get("channel") == page.channel_id:
            continue
        ch = await bot.get_channel_safe(page.channel_id)
        if ch is None:
            continue
        old = list(rec.get("mids") or []) if rec.get("channel") == page.channel_id else []
        mids: list[int] = []
        try:
            for i, batch in enumerate(embeds(page, lab=bot.lab_name)):
                msg = None
                if i < len(old):
                    try:
                        msg = await ch.fetch_message(old[i])
                        await msg.edit(content=None, embeds=batch)
                    except discord.NotFound:
                        msg = None
                if msg is None:
                    msg = await ch.send(embeds=batch, allowed_mentions=discord.AllowedMentions.none())
                mids.append(msg.id)
            for mid in old[len(mids):]:                  # the page got shorter: drop our own leftover posts
                try:
                    await (await ch.fetch_message(mid)).delete()
                except discord.HTTPException:
                    pass
        except discord.HTTPException as e:
            log.warning("community: page %s could not be posted: %s", page.name, e)
            continue
        known[page.name] = {"channel": page.channel_id, "mids": mids, "digest": page.digest}
        changed = True
        log.info("community: page %s %s in #%s", page.name, "updated" if old else "posted", getattr(ch, "name", "?"))
    if changed:
        state.set("pages", known)
