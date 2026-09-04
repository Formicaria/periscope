"""Shared event pipeline used by both the webhook receiver and the polling fallback."""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections import deque
from typing import Any

import discord
from periscope import Alert, LabBot, Severity
from periscope.hooks import NullHistory

from .config import GithubSettings
from .messages import feed_kind
from .render import ci_transition, event_ctx, is_bot_sender, one_liner, render, render_event, repo_name

log = logging.getLogger(__name__)
# a bot assembled by hand (a test, a bare install) has no event log; recording is never worth a crash
NO_LOG = NullHistory()

RECENT_MAX = 10
ACTIVITY_WINDOW_S = 24 * 3600


class Dispatcher:
    """Dedupes, filters, renders and posts a GitHub event; keeps counters + CI state in JsonState."""

    def __init__(self, bot: LabBot, cfg: GithubSettings):
        self.bot = bot
        self.history = getattr(bot, "history", NO_LOG)   # a no-op when this bot has none
        self.cfg = cfg
        self.state = bot.state.namespace("gh")
        self._seen: deque[str] = deque(maxlen=2000)
        self._seen_set: set[str] = set()

    # ----- dedupe -------------------------------------------------------------

    def seen(self, delivery_id: str | None) -> bool:
        if not delivery_id:
            return False
        if delivery_id in self._seen_set:
            return True
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(delivery_id)
        self._seen_set.add(delivery_id)
        return False

    # ----- bookkeeping ----------------------------------------------------------

    def bump(self, event: str) -> None:
        now = int(time.time())
        bucket = str(now - now % 3600)
        activity: dict[str, dict[str, int]] = self.state.get("activity", {}) or {}
        activity = {k: v for k, v in activity.items() if int(k) >= now - ACTIVITY_WINDOW_S - 3600}
        activity.setdefault(bucket, {})
        activity[bucket][event] = activity[bucket].get(event, 0) + 1
        self.state.set("activity", activity)

    def activity_summary(self) -> dict[str, int]:
        now = int(time.time())
        totals: dict[str, int] = {}
        for bucket, counts in (self.state.get("activity", {}) or {}).items():
            if int(bucket) >= now - ACTIVITY_WINDOW_S:
                for ev, n in counts.items():
                    totals[ev] = totals.get(ev, 0) + n
        return dict(sorted(totals.items(), key=lambda kv: -kv[1]))

    def remember(self, line: str) -> None:
        recent: list[str] = self.state.get("recent", []) or []
        recent.insert(0, line)
        self.state.set("recent", recent[:RECENT_MAX])

    def recent(self) -> list[str]:
        return self.state.get("recent", []) or []

    def ci_status(self) -> dict[str, dict[str, Any]]:
        return self.state.get("ci", {}) or {}

    def _set_ci(self, repo: str, info: dict[str, Any]) -> None:
        ci = self.ci_status()
        ci[repo] = {"ok": info["conclusion"] == "success", "name": info["name"], "url": info["url"],
                    "ts": int(time.time())}
        self.state.set("ci", ci)

    # ----- main entry -----------------------------------------------------------

    async def dispatch(self, event: str, payload: dict[str, Any], *, delivery_id: str | None = None,
                       source: str = "webhook", when: dt.datetime | None = None) -> bool:
        """Returns True if an embed was posted."""
        event = event.lower()
        if self.seen(delivery_id):
            log.debug("duplicate delivery %s ignored", delivery_id)
            return False
        if not self.cfg.wants_event(event):
            log.debug("event %s filtered by GITHUB_EVENTS", event)
            return False
        if self.cfg.ignore_bots and is_bot_sender(payload):
            log.debug("event %s from bot sender ignored", event)
            return False

        kind, embed = render_event(event, payload, self.bot.lab_name, verbose=self.cfg.verbose)
        if event == "workflow_run":
            await self._handle_ci(payload)
        if embed is None:
            log.debug("event %s/%s produced no embed (%s)", event, payload.get("action"), source)
            return False
        # the user's template for this kind of card (Messages page); None = they switched it off
        post = self.bot.messages.apply(feed_kind(kind), embed, event_ctx(kind, event, payload))
        if post is None:
            log.debug("event %s/%s not posted: %s is switched off", event, payload.get("action"), feed_kind(kind))
            return False

        self.bump(event)
        self.remember(one_liner(event, embed, when))
        self.history.record(service="github", kind="feed", key=repo_name(payload) or event, detail=event,
                            title=embed.title or event, server=self.bot.lab_name,
                            payload={"action": payload.get("action") or "", "via": source})
        targets = self.cfg.channels_for(repo_name(payload), event, self.bot.settings.alert_channel_id)
        if not targets:
            log.warning("no feed channel configured (GITHUB_FEED_CHANNEL_ID / ALERT_CHANNEL_ID); dropping %s", event)
            return False
        posted = False
        for channel_id in targets:
            ch = await self.bot.get_channel_safe(channel_id)
            if ch is None:
                continue
            try:
                await ch.send(embed=post)
                posted = True
            except discord.HTTPException as e:
                log.error("failed to post %s to channel %s: %s", event, channel_id, e)
        return posted

    async def note_completed_run(self, payload: dict[str, Any]) -> None:
        """A run whose live train card already shows the result: update alerts/activity, post nothing."""
        embed = render("workflow_run", payload, self.bot.lab_name, verbose=self.cfg.verbose)
        await self._handle_ci(payload)
        if embed is not None:
            self.bump("workflow_run")
            self.remember(one_liner("workflow_run", embed))

    async def _handle_ci(self, payload: dict[str, Any]) -> None:
        tr = ci_transition(payload)
        if not tr:
            return
        kind, fp, info = tr
        self._set_ci(info["repo"], info)
        self.history.record(service="github", kind="ci", key=info["repo"], server=self.bot.lab_name,
                            severity="ok" if kind == "resolve" else "critical",
                            title=f"{info['repo']} / {info['name']} {info['conclusion']} on {info['branch']}",
                            detail=info.get("url") or "", payload={"sha": info.get("sha")})
        if kind == "resolve":
            await self.bot.alerts.resolve(fp, note=f"`{info['sha']}` succeeded")
            return
        alert = Alert(
            fingerprint=fp,
            title=f"CI failing: {info['repo']} / {info['name']} on {info['branch']}",
            description=f"Workflow **{info['name']}** concluded `{info['conclusion']}` "
                        f"at `{info['sha']}` (triggered by {info['actor']}).",
            severity=Severity.CRITICAL,
            fields={"Repo": info["repo"], "Branch": info["branch"], "Run": info["url"] or "—"},
            url=info["url"],
            mention=True,
        )
        msg = await self.bot.alerts.fire(alert)
        role = self.cfg.ci_failure_role_id
        if msg is not None and role:
            # AlertRouter only pings ALERT_ROLE_ID; ping the dedicated CI role as a reply (embeds can't ping).
            try:
                await msg.channel.send(f"<@&{role}> `{info['repo']}` / {info['name']} is failing on `{info['branch']}`",
                                       reference=msg, allowed_mentions=discord.AllowedMentions(roles=True))
            except discord.HTTPException as e:
                log.warning("could not ping CI role: %s", e)


def get_dispatcher(bot: LabBot) -> Dispatcher:
    """One shared Dispatcher per bot, created on first use (works regardless of cog load order)."""
    d = getattr(bot, "gh_dispatcher", None)
    if d is None:
        d = Dispatcher(bot, bot.gh_settings)  # type: ignore[attr-defined]
        bot.gh_dispatcher = d  # type: ignore[attr-defined]
    return d
