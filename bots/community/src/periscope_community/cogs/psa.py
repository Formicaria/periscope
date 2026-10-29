"""/psa — what admins type, and the maintenance lifecycle that runs on its own once they have.

    /psa post                                   a one-off announcement (opens a form for the long text)
    /psa maint schedule when duration reason [affects]
    /psa maint now      duration reason [affects]
    /psa maint extend   id duration
    /psa maint done     id
    /psa maint cancel   id
    /psa maint list

Every maintenance gets ONE announcement post that is edited through its life (scheduled → in progress → done),
plus a short reply at each step so people actually get notified: "starts in an hour", "starting now", "done".
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import messages as M
from .. import roles
from ..timeparse import human_span, parse_duration, parse_when

log = logging.getLogger(__name__)
EVERYTHING = ("", "all", "everything", "every service", "*")
REMIND_BEFORE_S = 3600
OVERRUN_GIVE_UP_S = 3600


def load_tz(name: str) -> dt.tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("community: unknown TIMEZONE %r — using UTC", name)
        return dt.timezone.utc


class PostModal(discord.ui.Modal, title="Post an announcement"):
    headline = discord.ui.TextInput(label="Title", max_length=250)
    body = discord.ui.TextInput(label="Message", style=discord.TextStyle.paragraph, max_length=4000)

    def __init__(self, cog: "Psa"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        ch = await self.cog.channel()
        if ch is None:
            await interaction.response.send_message("No PSA channel is set (PSA_CHANNEL_ID).", ephemeral=True)
            return
        embeds = M.psa_embeds(str(self.headline), str(self.body), lab=self.cog.bot.lab_name,
                              author=getattr(interaction.user, "display_name", ""))
        content, allowed = roles.announcement(self.cog.bot, self.cog.cfg.announce_ping)
        msg = await ch.send(content=content, embeds=embeds, allowed_mentions=allowed)
        self.cog.bot.history.record(service="community", kind="psa", key=str(msg.id), title=str(self.headline))
        await interaction.response.send_message(f"Posted: {msg.jump_url}", ephemeral=True)


class Psa(commands.Cog):
    group = app_commands.Group(name="psa", description="Announcements and maintenance for this server")
    maint = app_commands.Group(name="maint", description="Schedule and run maintenance windows", parent=group)

    def __init__(self, bot: Any):
        self.bot = bot
        self.cfg = bot.community_cfg
        self.book = bot.community_book
        self.tz = bot.community_tz
        self.lifecycle.start()

    async def cog_unload(self) -> None:
        self.lifecycle.cancel()

    # ----- shared ---------------------------------------------------------------------------------------------
    async def channel(self) -> Any:
        return await self.bot.get_channel_safe(self.cfg.psa_channel_id) if self.cfg.psa_channel_id else None

    def ping(self, affects: list[str]) -> tuple[str | None, discord.AllowedMentions]:
        """Maintenance pings the roles of what it affects (everything = every resource's roles). Announcements
        don't come through here — they ping ANNOUNCE_PING (everyone)."""
        return roles.mention(self.bot, roles.tokens_for(self.cfg.resources, affects), fallback=self.cfg.psa_role_id)

    async def admin(self, interaction: discord.Interaction) -> bool:
        if self.bot.is_admin(interaction.user):
            return True
        await interaction.response.send_message("Only server admins can do that.", ephemeral=True)
        return False

    def parse_affects(self, raw: str | None) -> tuple[list[str], list[str]]:
        text = (raw or "").strip()
        if text.lower() in EVERYTHING:
            return [], []
        names, unknown = [], []
        for part in [p.strip() for p in text.split(",") if p.strip()]:
            res = self.cfg.by_name(part)
            if res is None:
                unknown.append(part)
            elif res.name not in names:
                names.append(res.name)
        return names, unknown

    async def affects_complete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        typed = [p.strip() for p in current.split(",")]
        head, last = ", ".join(p for p in typed[:-1] if p), typed[-1].lower()
        opts = ["everything"] + [r.name for r in self.cfg.resources]
        out = []
        for o in opts:
            if last and last not in o.lower():
                continue
            value = f"{head}, {o}" if head and o != "everything" else o
            out.append(app_commands.Choice(name=value[:100], value=value[:100]))
        return out[:25]

    async def id_complete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [app_commands.Choice(name=f"{e['id']} · {(e.get('reason') or 'maintenance')[:60]}", value=e["id"])
                for e in self.book.open() if current.lower() in e["id"]][:25]

    async def announce(self, entry: dict[str, Any], reply: str | None, *, ping: bool = True) -> dict[str, Any]:
        """Post (or edit) the entry's announcement, and send `reply` under it so people get a notification."""
        ch = await self.channel()
        if ch is None:
            return entry
        embed = self.bot.messages.apply("community.maintenance", M.maint_embed(entry, lab=self.bot.lab_name))
        if embed is None:
            return entry
        content, allowed = self.ping(entry.get("affects") or []) if ping else (None, discord.AllowedMentions.none())
        mid = entry.get("mid")
        msg = None
        if mid:
            try:
                msg = await ch.fetch_message(mid)
                await msg.edit(embed=embed)
            except discord.HTTPException:
                msg = None
        if msg is None:
            msg = await ch.send(content=content, embed=embed, allowed_mentions=allowed)
            entry = self.book.set_phase(entry, entry["phase"], mid=msg.id)
            return entry                       # the fresh post IS the notification
        if reply:
            ref = discord.MessageReference(message_id=msg.id, channel_id=ch.id, fail_if_not_exists=False)
            await ch.send(content=f"{content + ' ' if content else ''}{reply}", reference=ref, allowed_mentions=allowed)
        return entry

    def when_words(self, epoch: float) -> str:
        return f"{M.ts(epoch)} ({M.ts(epoch, 'R')})"

    # ----- /psa post -----------------------------------------------------------------------------------------
    @group.command(name="post", description="Post an announcement or changelog in the PSA channel (pings everyone)")
    async def post(self, interaction: discord.Interaction) -> None:
        if not await self.admin(interaction):
            return
        await interaction.response.send_modal(PostModal(self))

    # ----- /psa maint … --------------------------------------------------------------------------------------
    async def _create(self, interaction: discord.Interaction, start: float, duration: str, reason: str,
                      affects: str | None) -> None:
        try:
            seconds = parse_duration(duration)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        names, unknown = self.parse_affects(affects)
        if unknown:
            await interaction.response.send_message(
                f"Don't know {', '.join(repr(u) for u in unknown)} — pick from: everything, "
                + ", ".join(r.name for r in self.cfg.resources), ephemeral=True)
            return
        try:
            entry = self.book.create(start=start, end=start + seconds, reason=reason, affects=names,
                                     by=getattr(interaction.user, "display_name", str(interaction.user)))
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        now = time.time()
        if start <= now + 30:
            entry = self.book.set_phase(entry, "live")
        entry = await self.announce(entry, None)
        log.info("community: maintenance %s scheduled by %s: %s", entry["id"], entry["by"], entry["reason"])
        self.bot.history.record(service="community", kind="maintenance", key=entry["id"], title=entry["reason"],
                                detail=f"{entry['phase']} by {entry['by']}")
        await interaction.followup.send(
            f"**{entry['id']}** {'is live' if entry['phase'] == 'live' else 'is scheduled'} — "
            f"{self.when_words(entry['start'])} for {human_span(seconds)}. Alerts for "
            f"{M.affects_words(entry['affects'])} stay quiet until it's over. "
            f"`/psa maint done {entry['id']}` when you finish early.", ephemeral=True)

    @maint.command(name="schedule", description="Announce maintenance ahead of time")
    @app_commands.describe(when="e.g. 'fri 10pm', 'tomorrow 9am', '2026-10-30 22:00', 'in 2h'",
                           duration="How long, e.g. '2h', '90m'", reason="What's happening, in a sentence",
                           affects="What goes down (comma separated) — default everything")
    async def schedule(self, interaction: discord.Interaction, when: str, duration: str, reason: str,
                       affects: str | None = None) -> None:
        if not await self.admin(interaction):
            return
        try:
            start = parse_when(when, self.tz)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        await self._create(interaction, start, duration, reason, affects)

    schedule.autocomplete("affects")(affects_complete)

    @maint.command(name="now", description="Start maintenance right now (announces it immediately)")
    @app_commands.describe(duration="How long, e.g. '30m', '2h'", reason="What's happening",
                           affects="What goes down (comma separated) — default everything")
    async def now(self, interaction: discord.Interaction, duration: str, reason: str, affects: str | None = None) -> None:
        if not await self.admin(interaction):
            return
        await self._create(interaction, time.time(), duration, reason, affects)

    now.autocomplete("affects")(affects_complete)

    @maint.command(name="extend", description="Need longer? Push the end out")
    @app_commands.describe(id="Which maintenance", duration="How much longer, e.g. '30m'")
    async def extend(self, interaction: discord.Interaction, id: str, duration: str) -> None:
        if not await self.admin(interaction):
            return
        entry = self.book.get(id)
        if entry is None or entry.get("phase") not in ("scheduled", "live", "overrun"):
            await interaction.response.send_message(f"No open maintenance called {id!r}.", ephemeral=True)
            return
        try:
            seconds = parse_duration(duration)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        entry = self.book.extend(entry, seconds)
        entry = await self.announce(entry, f"⏳ Running longer than planned — now expected back {self.when_words(entry['end'])}.")
        await interaction.followup.send(f"{entry['id']} now ends {self.when_words(entry['end'])}.", ephemeral=True)

    extend.autocomplete("id")(id_complete)

    @maint.command(name="done", description="Finished early (or on time) — announce it")
    @app_commands.describe(id="Which maintenance")
    async def done(self, interaction: discord.Interaction, id: str) -> None:
        if not await self.admin(interaction):
            return
        entry = self.book.get(id)
        if entry is None or entry.get("phase") not in ("live", "overrun", "scheduled"):
            await interaction.response.send_message(f"No open maintenance called {id!r}.", ephemeral=True)
            return
        if entry["phase"] == "scheduled":
            await interaction.response.send_message("That one hasn't started — use `/psa maint cancel`.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.finish(entry)
        await interaction.followup.send(f"{entry['id']} marked done and announced.", ephemeral=True)

    done.autocomplete("id")(id_complete)

    @maint.command(name="cancel", description="Call off a scheduled maintenance")
    @app_commands.describe(id="Which maintenance")
    async def cancel(self, interaction: discord.Interaction, id: str) -> None:
        if not await self.admin(interaction):
            return
        entry = self.book.get(id)
        if entry is None or entry.get("phase") not in ("scheduled", "live", "overrun"):
            await interaction.response.send_message(f"No open maintenance called {id!r}.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        entry = self.book.set_phase(entry, "cancelled")
        await self.announce(entry, "❌ Called off — nothing's going down.", ping=False)
        await interaction.followup.send(f"{entry['id']} cancelled.", ephemeral=True)

    cancel.autocomplete("id")(id_complete)

    @maint.command(name="list", description="What's scheduled or running")
    async def list_(self, interaction: discord.Interaction) -> None:
        if not await self.admin(interaction):
            return
        rows = self.book.open()
        if not rows:
            await interaction.response.send_message("Nothing scheduled.", ephemeral=True)
            return
        lines = [f"**{e['id']}** · {e['phase']} · {self.when_words(float(e['start']))} → {M.ts(float(e['end']), 't')}"
                 f" · {e.get('reason') or 'maintenance'} · {M.affects_words(e.get('affects') or [])}" for e in rows]
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

    # ----- the clock ------------------------------------------------------------------------------------------
    def all_up(self, entry: dict[str, Any]) -> bool:
        """Is everything this maintenance touched answering again? (The watcher keeps down_since current even
        while a maintenance covers it; things that were dead before anyone looked don't hold it open.)"""
        down = set((self.bot.state.get("down_since") or {}).keys())
        quiet = set(self.bot.state.get("quiet_down") or [])
        for name in entry.get("affects") or [r.name for r in self.cfg.resources]:
            res = self.cfg.by_name(name)
            if res is not None and res.key in down and res.key not in quiet:
                return False
        return True

    async def finish(self, entry: dict[str, Any], note: str = "") -> None:
        entry = self.book.set_phase(entry, "done", finished=time.time(), note=note or entry.get("note"))
        await self.announce(entry, "✅ All done — everything should be back. Shout here if something isn't.")

    @tasks.loop(seconds=30)
    async def lifecycle(self) -> None:
        try:
            now = time.time()
            for entry in self.book.open():
                start, end, phase = float(entry["start"]), float(entry["end"]), entry["phase"]
                if phase == "scheduled":
                    if now >= start:
                        entry = self.book.set_phase(entry, "live")
                        await self.announce(entry, f"🛠️ Starting now: {entry.get('reason') or 'maintenance'}. "
                                                   f"Back {M.ts(end, 'R')}.")
                    elif start - now <= REMIND_BEFORE_S and not entry.get("reminded") \
                            and start - float(entry.get("created") or 0) > 2 * REMIND_BEFORE_S:
                        entry = self.book.set_phase(entry, "scheduled", reminded=True)
                        await self.announce(entry, f"⏰ Heads up — maintenance starts {M.ts(start, 'R')}.")
                elif phase == "live" and now >= end:
                    if self.all_up(entry):
                        await self.finish(entry)
                    else:
                        entry = self.book.set_phase(entry, "overrun")
                        await self.announce(entry, "⏳ Running a little long — some things are still coming back up.",
                                            ping=False)
                elif phase == "overrun":
                    if self.all_up(entry):
                        await self.finish(entry)
                    elif now >= end + OVERRUN_GIVE_UP_S:
                        await self.finish(entry, note="Some things are still coming back — they'll get their own "
                                                      "outage post if they stay down.")
        except Exception:  # noqa: BLE001
            log.exception("community: maintenance lifecycle pass failed")

    @lifecycle.before_loop
    async def _ready(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: Any) -> None:
    await bot.add_cog(Psa(bot))
