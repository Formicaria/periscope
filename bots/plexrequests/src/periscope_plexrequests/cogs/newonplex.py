"""New-on-Plex feed in NEW_CHANNEL: one card per film and per episode that has genuinely just landed.

Every five minutes the cog asks Plex for its newest films and episodes and compares them against what it has
already announced (`records.plex_seen()`). That record accumulates — it is never replaced by whatever happens
to be in the current window — so an item that scrolls out of view stays announced, and Plex re-ordering its
own "recently added" list cannot make old films look new again. Items are identified by their Plex guid
rather than their ratingKey, because a rescan or a rebuilt database hands out fresh ratingKeys for content
that has been on the shelf for years.

Two safety nets sit on top of that, because a Plex restart is exactly when this goes wrong:

  * the first pass on a fresh install (or after an upgrade from the old list-of-ratingKeys record) only takes
    a baseline — nothing is posted;
  * a pass where most of the window looks new is a rescan, not a delivery. It is adopted quietly and logged,
    so the channel never receives a library listing.

Removals are noticed too, as far as the window reaches: an item the feed announced, added no earlier than the
oldest thing Plex is still showing, that has vanished from the list has been deleted. Those go to the event
log (`bot.history`) rather than the channel, and the item is forgotten so a later re-add counts as new.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Iterable

import discord
from discord.ext import commands, tasks
from periscope.hooks import NullHistory

from ..common import AVAILABLE_COLOUR, NEW_ON_PLEX_KIND, resolve_channel
from ..context import PlexRequests

log = logging.getLogger(__name__)
# a bot assembled by hand (a test, a bare install) has no event log; recording is never worth a crash
NO_LOG = NullHistory()

MAX_PER_PASS = 5        # cards a pass may post; the rest wait for the next one instead of being dropped
KEEP_DAYS = 60          # how long an announced item is remembered, so it is never announced twice
MAX_REMEMBERED = 5000   # a ceiling on the record whatever the retention says, so state.json cannot run away
RESCAN_MIN = 15         # a pass has to be at least this odd before it counts as a rescan rather than a drop
RESCAN_SHARE = 0.6      # …and this much of the whole window has to look new


def item_id(it: dict[str, Any]) -> str:
    """What identifies an item for good: its Plex guid, falling back to the ratingKey when there is none."""
    return str(it.get("id") or it.get("key") or "")


def new_label(it: dict[str, Any]) -> str:
    year = f" ({it['year']})" if it.get("year") else ""
    return f"{it['title']}{year}"


def fresh_items(items: list[dict[str, Any]], seen: Any, limit: int | None = MAX_PER_PASS) -> list[dict[str, Any]]:
    """The items nobody has announced yet, oldest first, at most `limit` of them (None for all of them).

    Oldest first matters: when more has landed than one pass may post, the queue drains in the order things
    actually arrived rather than newest-first with the tail silently dropped."""
    known = set(seen or ())
    fresh = list(reversed([i for i in items if item_id(i) not in known]))
    return fresh if limit is None else fresh[:limit]


def looks_like_a_rescan(fresh: int, window: int) -> bool:
    """Is this pass a library rescan rather than a delivery?

    Plex hands out new ratingKeys when a library is repaired or rebuilt, and bumps `addedAt` when it re-reads
    files, so after a restart the whole window can look new at once. A season landing is a handful of items;
    most of the window turning over at once is not something that happens to a shelf that is only being added
    to, so it is treated as a rescan and adopted in silence."""
    return window > 0 and fresh >= RESCAN_MIN and fresh >= RESCAN_SHARE * window


def departed(items: list[dict[str, Any]], seen: dict[str, dict[str, Any]]) -> list[str]:
    """Ids the feed announced that have since gone from Plex — only as far back as the window can see.

    Plex only shows its newest items, so nothing can be said about anything older than the oldest one in the
    list. Inside that reach the reasoning is sound: an item added no earlier than that and no longer listed
    has been removed from the library."""
    stamps = [float(i.get("added_at") or 0) for i in items if i.get("added_at")]
    if not stamps or not seen:
        return []
    horizon, here = min(stamps), {item_id(i) for i in items}
    return sorted(key for key, meta in seen.items()
                  if key not in here and float((meta or {}).get("added") or 0) >= horizon)


def remember(seen: dict[str, dict[str, Any]], items: Iterable[dict[str, Any]], *,
             now: float | None = None) -> dict[str, dict[str, Any]]:
    """Fold items into the record. Already-known ones keep the date they were first seen."""
    stamp = float(now if now is not None else time.time())
    out = {key: dict(meta or {}) for key, meta in (seen or {}).items()}
    for it in items:
        key = item_id(it)
        if not key:
            continue
        entry = out.setdefault(key, {})
        entry.setdefault("first", stamp)
        entry["added"] = float(it.get("added_at") or entry.get("added") or 0)
        entry["title"] = str(it.get("title") or entry.get("title") or "")
    return out


def prune(seen: dict[str, dict[str, Any]], *, now: float | None = None, keep_days: float = KEEP_DAYS,
          cap: int = MAX_REMEMBERED) -> dict[str, dict[str, Any]]:
    """Drop what is too old to be worth remembering, newest kept when the ceiling is hit."""
    stamp = float(now if now is not None else time.time())
    cutoff = stamp - keep_days * 86400
    kept = [(key, meta) for key, meta in (seen or {}).items()
            if float((meta or {}).get("first") or stamp) >= cutoff]
    kept.sort(key=lambda kv: float((kv[1] or {}).get("first") or stamp), reverse=True)
    return dict(kept[:cap])


def new_on_plex_embed(it: dict[str, Any], plex_name: str) -> discord.Embed:
    """The feed card for one item that has just landed (a film or an episode)."""
    emoji = "🎬" if it["kind"] == "movie" else "📺"
    e = discord.Embed(title=f"🆕 {emoji}  {new_label(it)}", description=it["summary"] or None,
                      colour=discord.Colour.from_str(AVAILABLE_COLOUR))
    e.set_footer(text=f"Now on {plex_name}")
    return e


def new_on_plex_ctx(it: dict[str, Any], plex_name: str) -> dict[str, Any]:
    """What a plexrequests.new_on_plex template can use besides the card's own parts."""
    return {"name": str(it["title"]), "year": it.get("year") or "", "kind": it.get("kind") or "",
            "label": new_label(it), "plex_name": plex_name}


class NewOnPlexCog(commands.Cog):
    def __init__(self, bot: Any):
        self.bot = bot
        self.history = getattr(bot, "history", NO_LOG)   # a no-op when this bot has none
        self.ctx: PlexRequests = bot.plexreq
        self.cfg = self.ctx.cfg

    async def cog_load(self) -> None:
        if self.cfg.new_channel and self.cfg.plex_token:
            self.new_on_plex.start()

    async def cog_unload(self) -> None:
        self.new_on_plex.cancel()

    @tasks.loop(minutes=5)
    async def new_on_plex(self) -> None:
        channel = resolve_channel(self.bot, self.cfg.new_channel, self.cfg.guild_id)
        if channel is None:
            return
        try:
            items = await asyncio.to_thread(self.ctx.plex.recently_added)
        except Exception as ex:  # noqa: BLE001
            log.warning("new-on-plex: %s", ex)
            return
        now = time.time()
        seen = self.ctx.records.plex_seen()
        if seen is None:                       # never run, or upgraded from the old record: baseline quietly
            self.ctx.records.set_plex_seen(remember({}, items, now=now))
            log.info("new-on-plex: baselined %d items — nothing announced", len(items))
            return
        waiting = fresh_items(items, seen, limit=None)
        # the rescan check comes first: when Plex re-keys the shelf every old id looks deleted and every new
        # one looks added, and neither reading is worth acting on
        if looks_like_a_rescan(len(waiting), len(items)):
            self.ctx.records.set_plex_seen(prune(remember(seen, items, now=now), now=now))
            log.warning("new-on-plex: %d of %d items look new — that is a Plex rescan, not a delivery; "
                        "adopting them quietly instead of announcing the library", len(waiting), len(items))
            self.history.record(service="plexrequests", kind="rescan", severity="warning",
                                key=self.cfg.plex_name, server=self.bot.lab_name,
                                title=f"Plex re-keyed {len(waiting)} items — the feed stayed quiet")
            return
        seen = self.forget_departed(items, seen)
        self.ctx.records.set_plex_seen(prune(remember(seen, await self.announce(channel, waiting), now=now),
                                             now=now))

    async def announce(self, channel: Any, waiting: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Post up to a pass's worth of cards; returns the items that are now dealt with.

        Whatever is left over is not marked as seen, so the next pass picks it up rather than losing it —
        which is what keeps a season landing whole instead of five episodes deep."""
        done: list[dict[str, Any]] = []
        for it in waiting[:MAX_PER_PASS]:
            e = self.bot.messages.apply(NEW_ON_PLEX_KIND, new_on_plex_embed(it, self.cfg.plex_name),
                                        new_on_plex_ctx(it, self.cfg.plex_name))
            if e is None:                      # switched off: the item counts as dealt with, nothing is posted
                done.append(it)
                continue
            try:
                await channel.send(embed=e)
            except discord.HTTPException:
                break                          # leave the rest queued for the next pass
            self.ctx.stats.bump("new_on_plex")
            self.history.record(service="plexrequests", kind="added", key=item_id(it),
                                title=f"New on {self.cfg.plex_name}: {new_label(it)}",
                                server=self.bot.lab_name, payload={"kind": it.get("kind")})
            done.append(it)
        return done

    def forget_departed(self, items: list[dict[str, Any]], seen: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Write off what has gone from Plex: noted in the event log, dropped from the record, never posted."""
        for key in departed(items, seen):
            gone = seen.pop(key, None) or {}
            self.history.record(service="plexrequests", kind="removed", key=key, server=self.bot.lab_name,
                                title=f"Gone from {self.cfg.plex_name}: {gone.get('title') or key}")
        return seen

    @new_on_plex.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: Any) -> None:
    await bot.add_cog(NewOnPlexCog(bot))
