"""/link — members tie their Discord account to what they use here, and get that thing's role (which is also who
gets pinged when it goes down or has maintenance).

    /link minecraft   a one-time code to type in chat on any of our Minecraft servers; the server's chat bridge
                      relays it here, which proves who typed it → that server's player role. After that, playing on
                      another of our servers gives that one's role too (the bridge says who joined or chatted).
    /link plex        sign in with Plex on plex.tv's own page; an account that has this server → the Plex role
    /link status      what you're linked to
    /link remove      forget your Minecraft names (and take their roles back) or your Plex link
    /link sync        (admins) put every linked member's roles back
    /link list        (admins) who is linked to what

Roles are only ever added for Plex, never taken away: taking the Plex role from someone who was invited by
/plexinvite pulls their share (that's plexrequests' auto-revoke), so unlinking leaves it alone.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from .. import roles
from ..config import Bridge
from ..linking import (
    CODE_TTL_S,
    LinkBook,
    auth_url,
    find_code,
    first_word,
    plex_access,
    plex_account,
    plex_pin,
    plex_pin_token,
    short,
    speaker,
)
from ..pve import borrowed_env

log = logging.getLogger(__name__)
PLEX_WAIT_S = 10 * 60
PLEX_POLL_S = 3


class Link(commands.Cog):
    group = app_commands.Group(name="link", description="Link your Discord to your Minecraft or Plex account")

    def __init__(self, bot: Any):
        self.bot = bot
        self.cfg = bot.community_cfg
        self.book = LinkBook(bot.state)
        self.waiting: dict[str, discord.Interaction] = {}     # code → the /link minecraft that asked for it
        self.assured: set[tuple[int, int]] = set()            # (member, bridge channel) whose role is known good
        self.tasks: set[asyncio.Task] = set()
        self.session: aiohttp.ClientSession | None = None

    async def cog_unload(self) -> None:
        for t in self.tasks:
            t.cancel()
        if self.session and not self.session.closed:
            await self.session.close()

    # ----- helpers -------------------------------------------------------------------------------------------
    def http(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self.session

    def plex_client_id(self) -> str:
        cid = self.bot.state.get("plex_client_id")
        if not cid:
            cid = f"periscope-{uuid.uuid4()}"
            self.bot.state.set("plex_client_id", cid)
        return str(cid)

    async def member(self, uid: int) -> discord.Member | None:
        guild = self.bot.get_guild(int(self.bot.guild_id or 0))
        if guild is None:
            return None
        m = guild.get_member(uid)
        if m is None:
            try:
                m = await guild.fetch_member(uid)
            except discord.HTTPException:
                return None
        return m

    async def grant(self, uid: int, tokens: list[str], reason: str) -> list[str]:
        """Give `uid` the roles named; returns the names of the ones that were actually new."""
        m = await self.member(uid)
        if m is None:
            return []
        wanted = [m.guild.get_role(i) for i in roles.resolve(self.bot, tokens)]
        new = [r for r in wanted if r is not None and r not in m.roles]
        if new:
            try:
                await m.add_roles(*new, reason=reason)
            except discord.HTTPException as e:
                log.warning("community: could not give %s %s: %s", m, [r.name for r in new], e)
                return []
            log.info("community: %s → %s (%s)", m, ", ".join(r.name for r in new), reason)
        return [r.name for r in new]

    async def take(self, uid: int, tokens: list[str], reason: str) -> list[str]:
        m = await self.member(uid)
        if m is None:
            return []
        keep = set(roles.resolve(self.bot, [self.cfg.plex_role])) if self.cfg.plex_role else set()
        gone = [r for r in (m.guild.get_role(i) for i in roles.resolve(self.bot, tokens))
                if r is not None and r in m.roles and r.id not in keep]
        if gone:
            try:
                await m.remove_roles(*gone, reason=reason)
            except discord.HTTPException as e:
                log.warning("community: could not take %s from %s: %s", [r.name for r in gone], m, e)
                return []
        return [r.name for r in gone]

    def bridge_named(self, resource: str) -> Bridge | None:
        return next((b for b in self.cfg.bridges if b.resource == resource), None)

    def where(self) -> str:
        names = [short(b.resource) for b in self.cfg.bridges]
        return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]

    # ----- /link minecraft -------------------------------------------------------------------------------------
    @group.command(name="minecraft", description="Link your Minecraft name — get a code to type in game")
    async def minecraft(self, interaction: discord.Interaction) -> None:
        if not self.cfg.bridges:
            await interaction.response.send_message("Minecraft linking isn't set up on this server.", ephemeral=True)
            return
        code = self.book.issue(interaction.user.id)
        self.waiting = {c: i for c, i in self.waiting.items()
                        if i.user.id != interaction.user.id and time.time() - i.created_at.timestamp() < CODE_TTL_S}
        self.waiting[code] = interaction
        await interaction.response.send_message(
            f"Join **{self.where()}** and type this in the game chat within 15 minutes:\n"
            f"# `link {code}`\n"
            "You'll get that server's role as soon as it shows up here — and the role for any of our other "
            "servers the first time you play there.", ephemeral=True)

    async def redeem(self, message: discord.Message, bridge: Bridge, code: str, name: str) -> None:
        uid = self.book.redeem(code)
        if uid is None:
            return                                   # stale or someone's typo — the bridge channel is chat, stay quiet
        took_from = self.book.link_mc(uid, name, bridge.resource) if name else None
        added = await self.grant(uid, bridge.roles, reason=f"linked Minecraft account {name or '(unknown name)'}")
        self.assured.add((uid, bridge.channel_id))
        m = await self.member(uid)
        who = m.display_name if m else "their Discord account"
        tail = f" — {', '.join(added)} role added" if added else ""
        try:
            await message.channel.send(f"✅ {name or 'That player'} is linked to {who} on Discord{tail}.",
                                       allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.warning("community: could not confirm the link in #%s: %s", getattr(message.channel, "name", "?"), e)
        inter = self.waiting.pop(code, None)
        if inter is not None:
            note = "" if name else (" (I couldn't read your player name from the chat line, so the role for our "
                                    "other servers won't follow automatically.)")
            try:
                await inter.edit_original_response(
                    content=f"✅ Linked **{name or 'your Minecraft account'}** on {short(bridge.resource)}"
                            f"{' — ' + ', '.join(added) + ' added' if added else ''}.{note}")
            except discord.HTTPException:
                pass
        if took_from:
            log.info("community: Minecraft name %s moved from %s to %s", name, took_from, uid)
        self.bot.history.record(service="community", kind="link", key=f"mc:{name.lower() or uid}", severity="info",
                                title=f"Minecraft {name or '?'} linked", detail=f"{uid} on {bridge.resource}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.guild.id != int(self.bot.guild_id or 0):
            return
        bridge = self.cfg.bridge_for(message.channel.id)
        if bridge is None:
            return
        me = getattr(self.bot, "user", None)
        if me is not None and message.author.id == me.id:
            return
        if not (message.author.bot or message.webhook_id):
            return                                   # only what the bridge relays proves anything
        text = message.content or ""
        name = speaker(text, author=message.author.name, webhook=bool(message.webhook_id))
        code = find_code(text)
        if code:
            await self.redeem(message, bridge, code, name)
            return
        for cand in {name, first_word(text)} - {""}:
            uid = self.book.mc_owner(cand)
            if uid is None or (uid, bridge.channel_id) in self.assured:
                continue
            self.assured.add((uid, bridge.channel_id))
            if self.book.add_server(uid, cand, bridge.resource):
                log.info("community: %s seen on %s", cand, bridge.resource)
            await self.grant(uid, bridge.roles, reason=f"{cand} plays on {short(bridge.resource)}")

    # ----- /link plex ----------------------------------------------------------------------------------------
    def plex_env(self) -> dict[str, str]:
        return borrowed_env(self.cfg.plex_from) if self.cfg.plex_from else {}

    @group.command(name="plex", description="Link your Plex account — sign in on plex.tv")
    async def plex(self, interaction: discord.Interaction) -> None:
        env = self.plex_env()
        if not self.cfg.plex_role or not env.get("PLEX_TOKEN"):
            await interaction.response.send_message("Plex linking isn't set up on this server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        cid = self.plex_client_id()
        try:
            pin_id, code = await plex_pin(self.http(), cid)
        except Exception as e:  # noqa: BLE001
            log.warning("community: plex.tv PIN failed: %s", e)
            await interaction.followup.send("plex.tv isn't answering right now — try again in a bit.", ephemeral=True)
            return
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Sign in with Plex", url=auth_url(cid, code)))
        await interaction.followup.send(
            "Sign in with the Plex account you watch on. This only tells me *which* account it is — I check it "
            "has this server and keep its name, nothing else. The button works for 10 minutes.",
            view=view, ephemeral=True)
        task = asyncio.create_task(self.finish_plex(interaction, pin_id, env))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def finish_plex(self, interaction: discord.Interaction, pin_id: int, env: dict[str, str]) -> None:
        cid, uid = self.plex_client_id(), interaction.user.id

        async def say(text: str) -> None:
            try:
                await interaction.edit_original_response(content=text, view=None)
            except discord.HTTPException:
                pass

        token = None
        deadline = time.time() + PLEX_WAIT_S
        while time.time() < deadline and token is None:
            await asyncio.sleep(PLEX_POLL_S)
            try:
                token = await plex_pin_token(self.http(), cid, pin_id)
            except Exception:  # noqa: BLE001 — keep polling through a blip
                continue
        if token is None:
            await say("That Plex sign-in timed out — run `/link plex` again when you're ready.")
            return
        try:
            acct = await plex_account(self.http(), cid, token)
            ok, why = await plex_access(self.http(), cid, env["PLEX_TOKEN"], env.get("PLEX_URL", ""), acct["id"])
        except Exception as e:  # noqa: BLE001
            log.warning("community: Plex check failed: %s", e)
            await say("Signed in, but I couldn't check that account against the server — try again in a bit.")
            return
        finally:
            token = None                                 # the member's Plex token is never kept
        if not ok:
            await say(f"You're signed in as **{acct['name']}**, but that account "
                      + ("hasn't accepted its invite to this server yet — check your email, accept it, and run "
                         "`/link plex` again." if why == "pending" else
                         "doesn't have this Plex server. If you think it should, ask in the Plex channel."))
            return
        self.book.link_plex(uid, acct["id"], acct["name"])
        added = await self.grant(uid, [self.cfg.plex_role], reason=f"verified Plex account {acct['name']}")
        await say(f"✅ Linked to Plex account **{acct['name']}**"
                  + (f" — {', '.join(added)} role added." if added else " — you already had the role."))
        self.bot.history.record(service="community", kind="link", key=f"plex:{acct['id']}", severity="info",
                                title=f"Plex {acct['name']} linked", detail=str(uid))

    # ----- /link status | remove ------------------------------------------------------------------------------
    def describe(self, uid: int) -> str:
        rec = self.book.get(uid)
        lines = []
        for entry in (rec.get("mc") or {}).values():
            where = ", ".join(short(s) for s in entry.get("servers", [])) or "—"
            lines.append(f"⛏️ Minecraft **{entry['name']}** · {where}")
        if rec.get("plex"):
            lines.append(f"🎬 Plex **{rec['plex']['name']}**")
        return "\n".join(lines)

    @group.command(name="status", description="What your Discord is linked to")
    async def status(self, interaction: discord.Interaction) -> None:
        text = self.describe(interaction.user.id)
        await interaction.response.send_message(
            text or "Nothing linked yet — `/link minecraft` or `/link plex`.", ephemeral=True)

    @group.command(name="remove", description="Unlink your Minecraft names or your Plex account")
    @app_commands.describe(what="Which link to forget")
    @app_commands.choices(what=[app_commands.Choice(name="Minecraft", value="minecraft"),
                                app_commands.Choice(name="Plex", value="plex")])
    async def remove(self, interaction: discord.Interaction, what: app_commands.Choice[str]) -> None:
        uid = interaction.user.id
        if what.value == "plex":
            gone = self.book.unlink_plex(uid)
            await interaction.response.send_message(
                f"Forgot Plex account **{gone['name']}**. (Your Plex role stays — an admin can change that.)"
                if gone else "You had no Plex link.", ephemeral=True)
            return
        servers = self.book.servers(uid)
        gone = self.book.unlink_mc(uid)
        if not gone:
            await interaction.response.send_message("You had no Minecraft link.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        tokens = [t for s in servers if (b := self.bridge_named(s)) for t in b.roles]
        taken = await self.take(uid, tokens, reason="unlinked Minecraft account")
        self.assured = {a for a in self.assured if a[0] != uid}
        await interaction.followup.send(
            f"Forgot {', '.join('**' + g['name'] + '**' for g in gone)}"
            + (f" and took back {', '.join(taken)}." if taken else "."), ephemeral=True)

    # ----- admins ----------------------------------------------------------------------------------------------
    @group.command(name="list", description="(admins) Who is linked to what")
    async def list_(self, interaction: discord.Interaction) -> None:
        if not self.bot.is_admin(interaction.user):
            await interaction.response.send_message("Only server admins can do that.", ephemeral=True)
            return
        links = self.book.all()
        if not links:
            await interaction.response.send_message("Nobody has linked anything yet.", ephemeral=True)
            return
        lines = [f"<@{uid}>\n{self.describe(int(uid))}" for uid in links]
        await interaction.response.send_message(("\n".join(lines))[:1900], ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="sync", description="(admins) Give every linked member their roles again")
    async def sync(self, interaction: discord.Interaction) -> None:
        if not self.bot.is_admin(interaction.user):
            await interaction.response.send_message("Only server admins can do that.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        given = 0
        for uid_s, rec in self.book.all().items():
            uid = int(uid_s)
            tokens = [t for s in self.book.servers(uid) if (b := self.bridge_named(s)) for t in b.roles]
            if rec.get("plex") and self.cfg.plex_role:
                tokens.append(self.cfg.plex_role)
            given += len(await self.grant(uid, tokens, reason="/link sync"))
        await interaction.followup.send(f"Checked {len(self.book.all())} linked members · {given} roles given back.",
                                        ephemeral=True)


async def setup(bot: Any) -> None:
    await bot.add_cog(Link(bot))
