"""Discord channel/role convention as importable coroutines.

Shared by `periscope layout` (scripts/git_layout.py), the web UI (/discord "Create missing" and "Apply git/op
permissions", the first-run flow) and — as data — the terminal wizard. Every coroutine takes a `discord.Guild`;
a REST-only `discord.Client` (``await client.login(token)`` + ``fetch_guild``, no gateway) is enough, so nothing
here needs a connected presence.

Convention (by channel name, any category):
  #lab-status #lab-alerts #media #network #backups   boards + alerts        (category "🧪 LAB STATUS")
  #lab-cmd                                           slash commands          (category "🕹️ LAB CONTROL")
  @lab-admin @lab-oncall @bots                       roles
  #git-<project>   bot feed: @everyone read-only, @bots may post/embed/manage
  #op-<project>    humans only: @bots may not post here; purge removes old bot posts
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import discord

from .wizard import GITHUB_LAYOUT, INVITE_PERMS, LAYOUT

__all__ = ["LAYOUT", "GITHUB_LAYOUT", "INVITE_PERMS", "CONVENTION_CHANNELS", "CONVENTION_ROLES", "LayoutReport",
           "GitLayoutResult", "ensure_layout", "apply_git_layout", "git_env_lines", "layout_status"]

log = logging.getLogger(__name__)
Say = Callable[[str], Any]

CONVENTION_CHANNELS: tuple[str, ...] = tuple(n for _, names in LAYOUT["categories"] for n in names)
CONVENTION_ROLES: tuple[str, ...] = tuple(name for name, _, _ in LAYOUT["roles"])


def _quiet(_: str) -> None:
    return None


def _is_text(ch: Any) -> bool:  # by channel type rather than class so REST payloads and test doubles both work
    return getattr(ch, "type", None) in (discord.ChannelType.text, discord.ChannelType.news)


def _is_category(ch: Any) -> bool:
    return getattr(ch, "type", None) == discord.ChannelType.category


@dataclass
class LayoutReport:
    """What `ensure_layout` did. `lines` is the human log, the rest is for callers that want structure."""

    created_roles: list[str] = field(default_factory=list)
    created_categories: list[str] = field(default_factory=list)
    created_channels: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.created_roles or self.created_categories or self.created_channels)

    @property
    def ok(self) -> bool:
        return not self.errors


def layout_status(channel_names: list[str], role_names: list[str], *, github: bool = False) -> dict[str, Any]:
    """Which convention channels/roles exist, given the guild's channel + role names. Pure, for the UI."""
    chans = {n.lower() for n in channel_names}
    roles = {n.lower() for n in role_names}
    want_c = list(CONVENTION_CHANNELS) + (list(GITHUB_LAYOUT["channels"]) if github else [])
    want_r = list(CONVENTION_ROLES) + ([r[0] for r in GITHUB_LAYOUT["roles"]] if github else [])
    git = sorted(n for n in chans if n.startswith("git-"))
    op = sorted(n for n in chans if n.startswith("op-"))
    return {
        "channels": [{"name": n, "exists": n in chans} for n in want_c],
        "roles": [{"name": n, "exists": n in roles} for n in want_r],
        "git": git,
        "op": op,
        "missing_channels": [n for n in want_c if n not in chans],
        "missing_roles": [n for n in want_r if n not in roles],
    }


async def ensure_layout(guild: discord.Guild, *, github: bool = False, say: Say = _quiet) -> LayoutReport:
    """Create every missing convention role, category and channel (same plan as the wizard's LAYOUT).
    Idempotent: what already exists (by name, case-insensitive) is left alone."""
    rep = LayoutReport()

    def out(line: str) -> None:
        rep.lines.append(line)
        say(line)

    fetched = await guild.fetch_channels()
    chans = {c.name.lower(): c for c in fetched if _is_text(c)}
    cats = {c.name.lower(): c for c in fetched if _is_category(c)}
    roles = {r.name.lower(): r for r in await guild.fetch_roles()}

    role_specs = list(LAYOUT["roles"]) + (list(GITHUB_LAYOUT["roles"]) if github else [])
    for name, color, mentionable in role_specs:
        if name.lower() in roles:
            rep.existing.append(f"@{name}")
            continue
        try:
            r = await guild.create_role(name=name, colour=discord.Colour(color), mentionable=mentionable, reason="periscope layout")
            roles[name.lower()] = r
            rep.created_roles.append(name)
            out(f"created @{name}")
        except discord.HTTPException as e:
            rep.errors.append(f"could not create @{name}: {e}")
            out(f"!! could not create @{name}: {e}")

    layouts = [(cat, list(names)) for cat, names in LAYOUT["categories"]]
    if github:
        layouts.append((GITHUB_LAYOUT["category"], list(GITHUB_LAYOUT["channels"])))
    for cat_name, names in layouts:
        wanted = [n for n in names if n.lower() not in chans]
        rep.existing.extend(f"#{n}" for n in names if n.lower() in chans)
        if not wanted:
            continue
        cat = cats.get(cat_name.lower())
        if cat is None:
            try:
                cat = await guild.create_category(cat_name, reason="periscope layout")
                cats[cat_name.lower()] = cat
                rep.created_categories.append(cat_name)
                out(f"created category {cat_name}")
            except discord.HTTPException as e:
                rep.errors.append(f"could not create category {cat_name}: {e}")
                out(f"!! could not create category {cat_name}: {e}")
                cat = None
        for n in wanted:
            try:
                ch = await guild.create_text_channel(n, category=cat, reason="periscope layout")
                chans[n.lower()] = ch
                rep.created_channels.append(n)
                out(f"created #{n}")
            except discord.HTTPException as e:
                rep.errors.append(f"could not create #{n}: {e}")
                out(f"!! could not create #{n}: {e}")
    if not rep.changed and not rep.errors:
        out("nothing to create — the layout is complete")
    return rep


@dataclass
class GitLayoutResult:
    """What `apply_git_layout` did: the #git-*/#op-* channels it touched (name → id), every text channel seen,
    purge counts and the human log. `aborted` is set when the @bots role is missing (nothing was changed)."""

    channel_ids: dict[str, int] = field(default_factory=dict)
    channels: dict[str, int] = field(default_factory=dict)
    purged: dict[str, int] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    aborted: bool = False


async def apply_git_layout(guild: discord.Guild, *, me_id: int | None = None, maps: dict[str, list[str]] | None = None,
                           purge: list[str] | tuple[str, ...] = (), dry: bool = False, say: Say = _quiet) -> GitLayoutResult:
    """The `periscope layout` logic: #git-* channels become bot feeds (humans read-only, @bots post), #op-* channels
    mute @bots, `purge` channels lose old bot posts. `me_id` is the acting bot's user id (given the @bots role)."""
    res = GitLayoutResult()
    tag = "[dry-run] " if dry else ""

    def out(line: str) -> None:
        res.lines.append(line)
        say(line)

    channels = {c.name.lower(): c for c in await guild.fetch_channels() if _is_text(c)}
    res.channels = {name: ch.id for name, ch in channels.items()}
    roles = {r.name.lower(): r for r in await guild.fetch_roles()}
    bots = roles.get("bots")
    if bots is None:
        msg = "no @bots role — create the channel layout first (it creates it) or create it by hand"
        res.errors.append(msg)
        res.aborted = True
        out(f"!! {msg}")
        return res
    if me_id:
        try:
            me = guild.get_member(me_id) or await guild.fetch_member(me_id)
        except discord.HTTPException:
            me = None
        if me is not None and bots not in me.roles and not dry:
            try:
                await me.add_roles(bots, reason="periscope layout")
                out(f"  gave this bot the @{bots.name} role")
            except discord.Forbidden:
                out("!! cannot assign @bots to myself — give every periscope bot user the @bots role manually")

    everyone = guild.default_role
    for name, ch in sorted(channels.items()):
        if name.startswith("git-"):
            want = {
                everyone: discord.PermissionOverwrite(send_messages=False, create_public_threads=False,
                                                      create_private_threads=False, add_reactions=True),
                bots: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                  attach_files=True, manage_messages=True, read_message_history=True),
            }
            kind = "feed (humans read-only, bots post)"
        elif name.startswith("op-"):
            want = {bots: discord.PermissionOverwrite(send_messages=False)}
            kind = "discussion (bots muted)"
        else:
            continue
        out(f"{tag}#{ch.name}: {kind}")
        if not dry:
            try:
                for target, ow in want.items():
                    await ch.set_permissions(target, overwrite=ow, reason="periscope layout")
                if name.startswith("git-") and getattr(ch, "slowmode_delay", 0):
                    await ch.edit(slowmode_delay=0)
            except discord.HTTPException as e:
                res.errors.append(f"#{ch.name}: {e}")
                out(f"!! #{ch.name}: {e}")
                continue
        res.channel_ids[name] = ch.id

    for name in purge:
        ch = channels.get(name.lower().lstrip("#"))
        if ch is None:
            out(f"!! purge: no channel named #{name}")
            continue
        n = 0
        async for msg in ch.history(limit=None):
            if msg.author.bot and msg.embeds:
                n += 1
                if not dry:
                    try:
                        await msg.delete()
                    except discord.HTTPException:
                        pass
        res.purged[ch.name] = n
        out(f"{tag}#{ch.name}: removed {n} bot post{'s' if n != 1 else ''}")
    if not res.channel_ids and not purge:
        out("no #git-* or #op-* channels found — create some and re-run")
    return res


def git_env_lines(res: GitLayoutResult, maps: dict[str, list[str]] | None = None) -> list[str]:
    """The ready-to-paste GITHUB_* lines `periscope layout` prints, from a result and the --map arguments."""
    pairs: list[str] = []
    lines: list[str] = []
    for chan, repos in (maps or {}).items():
        key = chan.lower().lstrip("#")
        cid = res.channel_ids.get(key) or res.channels.get(key)
        if cid is None:
            lines.append(f"# !! no channel named #{chan}")
            continue
        pairs += [f"{r}={cid}" for r in repos]
    lines.append("GITHUB_REPO_CHANNEL_MAP=" + ",".join(pairs))
    lines.append("GITHUB_FEED_CHANNEL_ID=        # blank = repos not in the map are dropped; set a catch-all if you want one")
    lines.append("GITHUB_CI_CHANNEL_ID=")
    lines.append("GITHUB_MIRROR_TO_FEED=false")
    return lines
