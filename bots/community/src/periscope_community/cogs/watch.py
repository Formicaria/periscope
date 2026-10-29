"""The watcher: probe everything every minute, keep the board current, post outages (with the why) and their
recoveries, and send anything dropped in the outbox.

Rules that keep the channel worth reading:
  * nothing is posted until a resource has been down for OUTAGE_GRACE_S — restarts and blips stay invisible
  * one post per root cause, edited as the picture changes, flipped to "Resolved" when it's over
  * a resource that was already down the first time periscope ever looked at it is board-only until it has been
    up once — no PSA about a server somebody switched off on purpose last year
  * maintenance covers everything it affects: the board says 🛠️, no outage post goes out
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Any

import aiohttp
import discord
from discord.ext import commands, tasks
from periscope import Alert, RefreshView, Severity, StatusBoard

from .. import messages as M
from .. import roles
from ..diagnose import Cause, group, is_down
from ..probes import Probe, probe
from ..pve import PveView

log = logging.getLogger(__name__)
MIN_INTERVAL_S = 30


class Watch(commands.Cog):
    def __init__(self, bot: Any):
        self.bot = bot
        self.cfg = bot.community_cfg
        self.book = bot.community_book
        self.state = bot.state
        self.board = StatusBoard(bot, "community", channel_id=self.cfg.psa_channel_id)
        self.session: aiohttp.ClientSession | None = None
        self.loop.change_interval(seconds=max(MIN_INTERVAL_S, int(bot.settings.status_interval_s or 60)))
        self.loop.start()

    async def cog_unload(self) -> None:
        self.loop.cancel()
        if self.session and not self.session.closed:
            await self.session.close()

    # ----- helpers ------------------------------------------------------------------------------------------
    async def channel(self) -> discord.abc.Messageable | None:
        if not self.cfg.psa_channel_id:
            return None
        return await self.bot.get_channel_safe(self.cfg.psa_channel_id)

    def ping(self, names: list[str]) -> tuple[str | None, discord.AllowedMentions]:
        """Outages ping the roles of the resources hit (each resource's own ping roles), else PSA_ROLE_ID, else
        nobody. Announcements from the outbox ping ANNOUNCE_PING (everyone) instead."""
        return roles.mention(self.bot, roles.tokens_for(self.cfg.resources, names), fallback=self.cfg.psa_role_id)

    # ----- one pass -------------------------------------------------------------------------------------------
    @tasks.loop(seconds=60)
    async def loop(self) -> None:
        try:
            await self.tick()
        except Exception:  # noqa: BLE001 — loops never crash
            log.exception("community: watch pass failed")

    @loop.before_loop
    async def _ready(self) -> None:
        await self.bot.wait_until_ready()

    async def snapshot(self) -> tuple[dict[str, Probe], PveView]:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
        res = self.cfg.resources
        results = await asyncio.gather(*(probe(self.session, r) for r in res))
        probes = {r.key: p for r, p in zip(res, results)}
        pve = await self.bot.community_pve.view() if any(r.vmid is not None for r in res) else PveView(error="unused")
        return probes, pve

    async def tick(self) -> None:
        now = time.time()
        probes, pve = await self.snapshot()
        seen: list[str] = list(self.state.get("seen") or [])
        quiet: set[str] = set(self.state.get("quiet_down") or [])
        down_since: dict[str, float] = dict(self.state.get("down_since") or {})
        rows, down_keys = [], set()
        for r in self.cfg.resources:
            p = probes.get(r.key)
            down = is_down(r, p, pve)
            if r.key not in seen:
                seen.append(r.key)
                if down:
                    quiet.add(r.key)            # down the first time we ever looked: board-only for now
            if not down:
                quiet.discard(r.key)
                down_since.pop(r.key, None)
            else:
                down_since.setdefault(r.key, now)
            maint = self.book.covering(r.name, now)
            row: dict[str, Any] = {"name": r.name, "key": r.key, "ms": p.ms if p else None,
                                   "detail": p.detail if p else "", "since": down_since.get(r.key)}
            if maint is not None:
                row.update(state="maint", until=float(maint["end"]))
            elif down:
                row["state"] = "down"
                down_keys.add(r.key)
            elif p is not None and p.ok is None and not pve.ok:
                row["state"] = "unknown"
            else:
                row["state"] = "up"
            rows.append(row)
        self.state.set("seen", seen)
        self.state.set("quiet_down", sorted(quiet))
        self.state.set("down_since", down_since)

        # why, for the board (every down thing) and for posts (only past the grace period, not board-only)
        causes = group(self.cfg.resources, down_keys, pve, probes)
        why = {name: c.title for c in causes for name in c.affected}
        for row in rows:
            if row["state"] == "down":
                row["why"] = why.get(row["name"], "")
        postable_keys = {k for k in down_keys if k not in quiet and now - down_since.get(k, now) >= self.cfg.grace_s}
        postable = group(self.cfg.resources, postable_keys, pve, probes) if postable_keys else []
        await self.reconcile(postable, down_keys, down_since, now)
        self.bot.community_rows = rows
        if self.cfg.board:
            await self.render(rows, now)
        await self.drain_outbox()

    # ----- outages -------------------------------------------------------------------------------------------
    async def reconcile(self, causes: list[Cause], down_keys: set[str], down_since: dict[str, float],
                        now: float) -> None:
        open_: dict[str, dict[str, Any]] = dict(self.state.get("outages") or {})
        names_down = {r.name for r in self.cfg.resources if r.key in down_keys}
        ch = await self.channel()
        for c in causes:
            since = min((down_since.get(r.key, now) for r in self.cfg.resources if r.name in c.affected), default=now)
            # same incident under a better explanation? keep the post, re-key it
            match = open_.get(c.key) or next((v for v in open_.values() if set(v["affected"]) & set(c.affected)), None)
            if match is not None and match["key"] != c.key:
                open_.pop(match["key"], None)
            if match is None:
                mid = None
                if ch is not None:
                    content, allowed = self.ping(c.affected)
                    embed = self.bot.messages.apply("community.outage", M.outage_embed(c, since, lab=self.bot.lab_name))
                    if embed is not None:
                        msg = await ch.send(content=content, embed=embed, allowed_mentions=allowed)
                        mid = msg.id
                log.info("community: outage posted — %s (%s)", c.title, ", ".join(c.affected))
                self.bot.history.record(service="community", kind="outage", key=c.key, severity="critical",
                                        title=c.title, detail=", ".join(c.affected))
                await self.bot.alerts.fire(Alert(fingerprint=f"community:{c.key}:down", title=c.title,
                                                 description=c.why, severity=Severity.CRITICAL,
                                                 fields={"Affected": M.affects_words(c.affected)}))
                open_[c.key] = {"key": c.key, "mid": mid, "since": since, "title": c.title, "why": c.why,
                                "affected": list(c.affected)}
            else:
                changed = (match["title"], match["why"], sorted(match["affected"])) != (c.title, c.why, sorted(c.affected))
                rec = {**match, "key": c.key, "title": c.title, "why": c.why, "affected": list(c.affected),
                       "since": min(float(match["since"]), since)}
                open_[c.key] = rec
                if changed and ch is not None and rec.get("mid"):
                    await self._edit(ch, rec["mid"], M.outage_embed(c, rec["since"], lab=self.bot.lab_name))
        # anything open whose resources are all back → resolved
        live_keys = {c.key for c in causes}
        for key, rec in list(open_.items()):
            if key in live_keys:
                continue
            if set(rec["affected"]) & names_down:
                continue                       # still down, just not past grace under this cause any more
            cause = Cause(rec["key"], rec["title"], rec["why"], list(rec["affected"]))
            if ch is not None and rec.get("mid"):
                await self._edit(ch, rec["mid"], M.recovered_embed(cause, float(rec["since"]), now, lab=self.bot.lab_name))
                try:
                    ref = discord.MessageReference(message_id=rec["mid"], channel_id=ch.id, fail_if_not_exists=False)
                    await ch.send(M.back_line(cause, float(rec["since"]), now), reference=ref,
                                  allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException as e:
                    log.warning("community: could not post the back-up line: %s", e)
            log.info("community: resolved — %s", rec["title"])
            self.bot.history.record(service="community", kind="recovered", key=key, severity="ok", title=rec["title"])
            await self.bot.alerts.resolve(f"community:{key}:down")
            open_.pop(key, None)
        self.state.set("outages", open_)

    async def _edit(self, ch: Any, mid: int, embed: discord.Embed) -> None:
        try:
            msg = await ch.fetch_message(mid)
            await msg.edit(embed=embed)
        except discord.HTTPException as e:
            log.warning("community: could not edit post %s: %s", mid, e)

    # ----- board ----------------------------------------------------------------------------------------------
    async def build_board(self, rows: list[dict[str, Any]] | None = None, now: float | None = None) -> discord.Embed:
        rows = rows if rows is not None else getattr(self.bot, "community_rows", [])
        return M.board_embed(rows, self.book.open(), lab=self.bot.lab_name, problems=self.cfg.problems,
                             now=now or time.time())

    async def render(self, rows: list[dict[str, Any]], now: float) -> None:
        try:
            embed = await self.build_board(rows, now)
            await self.board.render(embed, view=RefreshView(self.build_board, custom_id="periscope:community:refresh"))
        except discord.HTTPException as e:
            log.warning("community: board update failed: %s", e)

    # ----- outbox ---------------------------------------------------------------------------------------------
    async def drain_outbox(self) -> None:
        box: Path = self.cfg.outbox
        if not box.is_dir():
            return
        files = sorted(p for p in box.glob("*.md") if p.is_file())
        if not files:
            return
        ch = await self.channel()
        if ch is None:
            return
        sent = box / "sent"
        sent.mkdir(exist_ok=True)
        for f in files:
            try:
                title, body = M.split_markdown(f.read_text(encoding="utf-8"))
                embeds = M.psa_embeds(title, body, lab=self.bot.lab_name)
                content, allowed = roles.announcement(self.bot, self.cfg.announce_ping)   # changelogs ping everyone
                for i in range(0, len(embeds), 10):
                    await ch.send(content=content if i == 0 else None, embeds=embeds[i:i + 10], allowed_mentions=allowed)
                shutil.move(str(f), str(sent / f"{time.strftime('%Y%m%d-%H%M%S')}-{f.name}"))
                log.info("community: posted outbox announcement %s", f.name)
                self.bot.history.record(service="community", kind="psa", key=f.name, severity="info", title=title)
            except Exception:  # noqa: BLE001
                log.exception("community: outbox file %s could not be posted — left in place", f.name)


async def setup(bot: Any) -> None:
    await bot.add_cog(Watch(bot))
