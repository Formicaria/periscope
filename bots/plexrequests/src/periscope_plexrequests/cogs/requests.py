"""Movie & TV requests: the Search & Request button + modal, titles typed into the requests channel,
`/requests request`, private result menus, announcement cards routed per media type, the availability
watcher that flips cards green (and pings the requester), and `/requests mystatus`."""

from __future__ import annotations

import logging
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..common import (
    AVAILABLE_COLOUR,
    BLURPLE,
    STATUS_AVAILABLE,
    STATUS_PARTIAL,
    STATUS_PENDING,
    STATUS_PROCESSING,
    TYPE_EMOJI,
    build_media_embed,
    build_options,
    build_request_embed,
    check_cooldown,
    parse_typed_title,
    requests_role_denial,
    requests_role_ok,
    result_key,
    title_label,
    validate_query,
)
from ..context import WATCH_MAX_AGE, PlexRequests, slash

log = logging.getLogger(__name__)

REQUEST_CUSTOM_ID = "plexrequests:request"
LEGACY_REQUEST_CUSTOM_ID = "ztplex:request"    # buttons on embeds posted by the standalone bot keep working
REQUEST_MESSAGE_KEY = "request_message_id"


class RequestModal(discord.ui.Modal, title="Request a movie or show"):
    query = discord.ui.TextInput(label="Title", placeholder="e.g. Dune Part Two", max_length=100)

    def __init__(self, cog: "RequestsCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.search_and_offer(interaction, str(self.query.value))


class RequestButtonView(discord.ui.View):
    """Persistent 'Search & Request' button."""

    def __init__(self, cog: "RequestsCog", custom_id: str = REQUEST_CUSTOM_ID):
        super().__init__(timeout=None)
        self.cog = cog
        button: discord.ui.Button = discord.ui.Button(label="Search & Request", style=discord.ButtonStyle.primary,
                                                      emoji="🔎", custom_id=custom_id)
        button.callback = self.on_click  # type: ignore[method-assign]
        self.add_item(button)

    async def on_click(self, interaction: discord.Interaction) -> None:
        self.cog.ctx.stats.bump("request_button", interaction.user)
        if not requests_role_ok(interaction.user, self.cog.cfg):
            await interaction.response.send_message(requests_role_denial(self.cog.cfg), ephemeral=True)
            return
        await interaction.response.send_modal(RequestModal(self.cog))


class ResultsView(discord.ui.View):
    """Select menu of search results, locked to the requester.

    Everything stays hidden until the request is actually sent: menus are ephemeral (button/slash) or
    self-destruct (typed flow), results go privately to the requester, and the only public trace is the
    announcement posted after the backend accepts the request.
    """

    def __init__(self, cog: "RequestsCog", requester_id: int, results: list[dict[str, Any]], public: bool = False):
        super().__init__(timeout=180)
        self.cog = cog
        self.requester_id = requester_id
        self.public = public                 # True = menu is a normal channel message
        self.menu_message: Any = None        # set by the caller after sending the menu
        self.results = {result_key(r, i): r for i, r in enumerate(results)}
        select: discord.ui.Select = discord.ui.Select(placeholder="Pick the title you want…", options=build_options(results))
        select.callback = self.on_pick  # type: ignore[method-assign]
        self.select = select
        self.add_item(select)

    async def on_timeout(self) -> None:
        if self.menu_message is None:
            return
        try:
            if self.public:
                await self.menu_message.delete()   # leave no trace in the channel
            else:
                await self.menu_message.edit(content="⌛ Search expired — start a new search.", view=None)
        except discord.HTTPException:
            pass

    async def on_pick(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("That menu belongs to someone else — start your own search.",
                                                    ephemeral=True)
            return
        pick = self.results[self.select.values[0]]
        self.cog.ctx.stats.bump("pick", interaction.user)
        self.stop()

        if self.public:
            # Typed flow: acknowledge privately, then remove the menu from the channel.
            await interaction.response.defer(ephemeral=True, thinking=True)
            menu = self.menu_message or interaction.message
            if menu is not None:
                try:
                    await menu.delete()
                except discord.HTTPException:
                    pass
        else:
            # Ephemeral menu: swap it for a progress note. This UPDATE_MESSAGE response also makes the menu the
            # interaction's original response, so the edit below reliably lands on it.
            await interaction.response.edit_message(content=f"⏳ Requesting **{pick['title']}**…", view=None)

        text, card = await self.cog.submit_request(interaction.user, pick, interaction.channel)
        try:
            await interaction.edit_original_response(content=text, embed=card, view=None)
        except discord.HTTPException:
            try:
                await interaction.followup.send(text, embed=card or discord.utils.MISSING, ephemeral=True)
            except discord.HTTPException:
                log.warning("could not deliver the request result to %s (%s)", interaction.user, interaction.user.id)


class RequestsCog(commands.Cog):
    def __init__(self, bot: Any):
        self.bot = bot
        self.ctx: PlexRequests = bot.plexreq
        self.cfg = self.ctx.cfg
        self._ready_once = False

    async def cog_load(self) -> None:
        self.ctx.add_persistent_view(RequestButtonView(self))
        self.ctx.add_persistent_view(RequestButtonView(self, custom_id=LEGACY_REQUEST_CUSTOM_ID))
        self.ctx.register(
            slash("request", "Request a movie or TV show for Plex", self.request),
            slash("mystatus", "Your recent requests and where they are", self.mystatus),
        )
        if self.ctx.backend.active:
            self.watch_available.start()
        else:
            log.warning("[%s] no request backend configured (Seerr or Radarr/Sonarr) — media requests are disabled",
                        self.bot.name)

    async def cog_unload(self) -> None:
        self.watch_available.cancel()
        self.ctx.unregister("request", "mystatus")
        await self.ctx.close()                # the backend's HTTP sessions (Bot.close() unloads cogs)

    def view(self) -> RequestButtonView:
        return RequestButtonView(self)

    # ----- search -----

    async def start_request_search(self, member: Any, query: str) -> tuple[str | None, list[dict[str, Any]] | None]:
        """Returns (error_text, results). error_text set when the flow should stop."""
        if not self.ctx.backend.active:
            return ("❌ Requests aren't configured yet (no Seerr or Radarr/Sonarr settings). Tell the admin.", None)
        if not requests_role_ok(member, self.cfg):
            log.info("search denied (missing %r role): %s (%s)", self.cfg.requests_role_name, member, member.id)
            return (requests_role_denial(self.cfg), None)
        if not check_cooldown(self.ctx.request_cd, member.id, limit=5):
            log.info("search rate-limited: %s (%s)", member, member.id)
            return ("⏳ Too many searches — give it a few minutes.", None)
        query = (query or "").strip()
        err = validate_query(query)
        if err:
            return (err, None)
        try:
            results = await self.ctx.backend.search(query)
        except Exception as e:  # noqa: BLE001
            log.exception("search failed for %r", query)
            return (f"❌ Search failed: {str(e)[:150]}", None)
        log.info("search: %s (%s) %r -> %d results", member, member.id, query, len(results))
        self.ctx.stats.bump("search", member)
        if not results:
            return (f"🔍 No movies or shows found for **{query}** — check the spelling?", None)
        return (None, results)

    async def search_and_offer(self, interaction: discord.Interaction, query: str) -> None:
        """Button/slash flow: the interaction is already deferred ephemerally."""
        err, results = await self.start_request_search(interaction.user, query)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return
        view = ResultsView(self, interaction.user.id, results or [])
        view.menu_message = await interaction.followup.send(f"🔍 Results for **{query.strip()}** — pick one:",
                                                            view=view, ephemeral=True)

    # ----- submit -----

    def announce_channel_for(self, media_type: str, member: Any, fallback_channel: Any) -> Any:
        """Route the announcement card: #movies / #tv if configured, else the requests channel."""
        conf = self.cfg.announce_channel.get(media_type, "")
        ch = None
        if conf:
            if conf.isdigit():
                ch = self.bot.get_channel(int(conf))
            else:
                guild = getattr(member, "guild", None)
                if guild:
                    ch = discord.utils.get(guild.text_channels, name=conf)
            if ch is None:
                log.warning("announce channel %r for %s not found — using the requests channel", conf, media_type)
        return ch or self.ctx.requests_channel() or fallback_channel

    async def submit_request(self, member: Any, pick: dict[str, Any], channel: Any) -> tuple[str, discord.Embed | None]:
        """Returns (result text, info card embed or None)."""
        label = title_label(pick)
        emoji = TYPE_EMOJI[pick["media_type"]]
        card = build_media_embed(pick)

        if pick["status"] == STATUS_AVAILABLE:
            self.ctx.stats.bump("already_on_plex", member)
            return (f"✅ {label} is already on Plex — go watch it!", card)
        if pick["status"] in (STATUS_PENDING, STATUS_PROCESSING):
            self.ctx.stats.bump("already_requested", member)
            return (f"⏳ {label} was already requested — it's in the queue.", card)

        try:
            ok, msg, watch_info = await self.ctx.backend.request(pick)
        except Exception as e:  # noqa: BLE001
            log.exception("request errored for %s %s", pick["media_type"], pick["tmdb_id"])
            return (f"❌ Couldn't reach the request backend: {str(e)[:150] or type(e).__name__}", None)
        log.info("request[%s]: discord=%s (%s) %s %s -> %s", pick.get("backend", "seerr"), member, member.id,
                 pick["media_type"], pick["title"], "ok" if ok else msg)
        self.ctx.stats.bump("request_ok" if ok else "request_fail", member)
        if ok:
            announce_channel = self.announce_channel_for(pick["media_type"], member, channel)
            try:
                ann = await announce_channel.send(embed=build_media_embed(
                    pick, footer=f"Requested by {member.display_name} • added to the download queue"))
                self.ctx.records.track_request(member.id, pick, ann.id if ann else 0)
                if watch_info and ann:
                    self.ctx.records.add_watch(watch_info, getattr(announce_channel, "id", 0), ann.id,
                                               member.display_name, member.id, pick["title"])
                    log.info("watching %s for availability (msg %s)", watch_info, ann.id)
            except discord.Forbidden:
                pass
            if getattr(announce_channel, "id", None) == self.cfg.requests_channel_id:
                await self.ctx.sticky.restick(announce_channel, REQUEST_MESSAGE_KEY, build_request_embed(self.cfg), self.view())
            via = "Seerr" if pick.get("backend") == "seerr" else ("Radarr" if pick["media_type"] == "movie" else "Sonarr")
            return (f"{emoji} {label} requested! {via} has it now — it'll appear on Plex once it's downloaded.", card)
        low = (msg or "").lower()
        if "already exists" in low or "already" in low:
            return (f"⏳ {label} was already requested — it's in the queue.", card)
        return (f"❌ Couldn't request {label}: {msg}", card)

    # ----- commands -----

    @app_commands.describe(title="What do you want added?")
    async def request(self, interaction: discord.Interaction, title: str) -> None:
        self.ctx.stats.bump("cmd_request", interaction.user)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.search_and_offer(interaction, title)

    async def mystatus(self, interaction: discord.Interaction) -> None:
        self.ctx.stats.bump("cmd_mystatus", interaction.user)
        hist = self.ctx.records.history(interaction.user.id)
        if not hist:
            await interaction.response.send_message(
                "You haven't requested anything yet — hit **Search & Request** or use `/requests request`!",
                ephemeral=True)
            return
        icon = {"queued": "⏳", "available": "🟢"}
        lines = []
        for h in reversed(hist):
            year = f" ({h['year']})" if h.get("year") else ""
            when = time.strftime("%b %d", time.localtime(h.get("ts", 0)))
            lines.append(f"{icon.get(h.get('status'), '⏳')} {TYPE_EMOJI.get(h.get('type'), '')} "
                         f"**{h['title']}{year}** — {h.get('status', 'queued')} · {when}")
        e = discord.Embed(title="📈 Your requests", description="\n".join(lines[:15]),
                          colour=discord.Colour.from_str(BLURPLE))
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ----- typed titles -----

    async def handle_request_message(self, message: Any) -> None:
        content = parse_typed_title(message.content or "")
        if content is None:
            return
        self.ctx.stats.bump("typed_request", message.author)
        # Keep the channel clean: the typed title disappears, the menu self-destructs, and only the
        # post-request announcement is ever left behind.
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        err, results = await self.start_request_search(message.author, content)
        if err:
            try:
                await message.channel.send(f"{message.author.mention} {err}", delete_after=20)
            except discord.HTTPException:
                pass
        else:
            view = ResultsView(self, message.author.id, results or [], public=True)
            try:
                view.menu_message = await message.channel.send(
                    f"🔍 {message.author.mention} — results for **{content}**, pick one "
                    f"(this menu vanishes once the request is sent):", view=view)
            except discord.HTTPException:
                log.error("cannot post the results menu in #%s", getattr(message.channel, "name", message.channel))
        await self.ctx.sticky.restick(message.channel, REQUEST_MESSAGE_KEY, build_request_embed(self.cfg), self.view())

    # ----- availability watcher -----

    async def mark_available(self, watch: dict[str, Any]) -> bool:
        """Green card + footer flip, 🎉 ping the requester. True = stop watching."""
        channel = self.bot.get_channel(watch["channel_id"])
        if channel is None:
            return True                       # channel gone — stop watching
        try:
            msg = await channel.fetch_message(watch["message_id"])
        except discord.HTTPException:
            return True                       # message deleted — stop watching
        if msg.embeds:
            e = msg.embeds[0]
            e.colour = discord.Colour.from_str(AVAILABLE_COLOUR)
            e.set_footer(text=f"Requested by {watch['requester']} • {self.cfg.available_text}")
            try:
                await msg.edit(embed=e)
                log.info("marked available: msg %s (%s)", msg.id, e.title)
                self.ctx.stats.bump("became_available")
            except discord.HTTPException:
                pass
        if watch.get("requester_id"):
            try:
                await channel.send(f"🎉 <@{watch['requester_id']}> — **{watch.get('title', 'your request')}** "
                                   f"is ready! {self.cfg.available_text}.")
            except discord.HTTPException:
                pass
        return True

    async def check_watches(self) -> None:
        watches = self.ctx.records.watches()
        if not watches:
            return
        drop: set[int] = set()
        available: list[dict[str, Any]] = []
        for w in watches:
            try:
                status = await self.ctx.backend.watch_status(w)
            except Exception:  # noqa: BLE001
                continue                      # backend unreachable — try again next cycle
            if status in (STATUS_PARTIAL, STATUS_AVAILABLE):
                await self.mark_available(w)
                drop.add(w["message_id"])
                available.append(w)
            elif time.time() - w.get("added", 0) > WATCH_MAX_AGE:
                drop.add(w["message_id"])     # stale — give up quietly
        if drop:
            self.ctx.records.drop_watches(drop)
            for w in available:
                self.ctx.records.mark_history_available(w.get("requester_id", 0), w["message_id"])

    @tasks.loop(minutes=5)
    async def watch_available(self) -> None:
        try:
            await self.check_watches()
        except Exception:  # noqa: BLE001
            log.exception("availability watcher failed")

    @watch_available.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    # ----- events -----

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if self.ctx.is_invite_channel(message.channel):
            return                            # the invites cog owns that channel
        if self.ctx.is_requests_channel(message.channel):
            await self.handle_request_message(message)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._ready_once:
            return
        self._ready_once = True
        log.info("[%s] request backend: %s", self.bot.name, self.ctx.backend.describe())
        if not self.cfg.requests_channel_id:
            log.warning("[%s] REQUESTS_CHANNEL_ID is empty — typed requests and the requests embed are off", self.bot.name)
            return
        channel = self.ctx.requests_channel()
        if channel is None:
            log.error("[%s] could not find the requests channel (%s)", self.bot.name, self.cfg.requests_channel_id)
            return
        await self.ctx.sticky.ensure(channel, REQUEST_MESSAGE_KEY, build_request_embed(self.cfg), self.view())


async def setup(bot: Any) -> None:
    await bot.add_cog(RequestsCog(bot))
