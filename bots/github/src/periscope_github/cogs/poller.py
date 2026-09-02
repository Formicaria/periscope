"""Polling fallback: GET /orgs/{org}/events -> webhook-shaped payloads -> shared Dispatcher."""

from __future__ import annotations

import logging
from typing import Any

from discord.ext import commands, tasks
from periscope import LabBot

from ..client import GithubClient, Reachability
from ..dispatch import get_dispatcher
from ..render import parse_ts

log = logging.getLogger(__name__)

GITHUB = "https://github.com"


# ----- adapters: Events-API shape -> webhook shape (pure) -----------------------

def _common(ev: dict[str, Any]) -> dict[str, Any]:
    actor = ev.get("actor") or {}
    full = (ev.get("repo") or {}).get("name") or "?/?"
    owner, _, name = full.partition("/")
    login = actor.get("display_login") or actor.get("login") or "?"
    return {
        "sender": {"login": login, "avatar_url": actor.get("avatar_url"), "html_url": f"{GITHUB}/{login}",
                   "type": "Bot" if login.endswith("[bot]") else "User"},
        "repository": {"name": name, "full_name": full, "html_url": f"{GITHUB}/{full}",
                       "owner": {"login": owner}, "default_branch": "main"},
        "organization": {"login": owner},
    }


def adapt_push(ev: dict[str, Any]) -> dict[str, Any]:
    p, full = ev.get("payload") or {}, (ev.get("repo") or {}).get("name", "")
    before, head = p.get("before") or "", p.get("head") or ""
    commits = [{
        "id": c.get("sha", ""),
        "message": c.get("message", ""),
        "author": {"name": (c.get("author") or {}).get("name", "?")},
        "url": f"{GITHUB}/{full}/commit/{c.get('sha', '')}",
    } for c in p.get("commits") or []]
    out = _common(ev)
    out.update({"ref": p.get("ref"), "before": before, "after": head, "commits": commits, "forced": False,
                "deleted": False, "created": False,
                "compare": f"{GITHUB}/{full}/compare/{before[:12]}...{head[:12]}" if before and head else None})
    return out


def adapt_pull_request(ev: dict[str, Any]) -> dict[str, Any]:
    p = ev.get("payload") or {}
    out = _common(ev)
    out.update({"action": p.get("action"), "number": p.get("number"), "pull_request": p.get("pull_request") or {},
                "requested_reviewer": p.get("requested_reviewer"), "requested_team": p.get("requested_team")})
    return out


def adapt_issues(ev: dict[str, Any]) -> dict[str, Any]:
    p = ev.get("payload") or {}
    out = _common(ev)
    out.update({"action": p.get("action"), "issue": p.get("issue") or {}, "label": p.get("label"),
                "assignee": p.get("assignee")})
    return out


def adapt_release(ev: dict[str, Any]) -> dict[str, Any]:
    p = ev.get("payload") or {}
    out = _common(ev)
    out.update({"action": p.get("action"), "release": p.get("release") or {}})
    return out


def adapt_create(ev: dict[str, Any]) -> dict[str, Any]:
    p = ev.get("payload") or {}
    out = _common(ev)
    if p.get("ref_type") == "repository":
        out.update({"action": "created"})  # rendered via "repository"
    else:
        out.update({"ref": p.get("ref"), "ref_type": p.get("ref_type"), "description": p.get("description")})
    if p.get("master_branch"):
        out["repository"]["default_branch"] = p["master_branch"]
    return out


def adapt_delete(ev: dict[str, Any]) -> dict[str, Any]:
    p = ev.get("payload") or {}
    out = _common(ev)
    out.update({"ref": p.get("ref"), "ref_type": p.get("ref_type")})
    return out


def adapt_fork(ev: dict[str, Any]) -> dict[str, Any]:
    p = ev.get("payload") or {}
    out = _common(ev)
    out.update({"forkee": p.get("forkee") or {}})
    return out


def adapt_watch(ev: dict[str, Any]) -> dict[str, Any]:
    out = _common(ev)
    out.update({"action": "created"})
    return out


ADAPTERS: dict[str, tuple[str, Any]] = {
    "PushEvent": ("push", adapt_push),
    "PullRequestEvent": ("pull_request", adapt_pull_request),
    "IssuesEvent": ("issues", adapt_issues),
    "ReleaseEvent": ("release", adapt_release),
    "CreateEvent": ("create", adapt_create),
    "DeleteEvent": ("delete", adapt_delete),
    "ForkEvent": ("fork", adapt_fork),
    "WatchEvent": ("star", adapt_watch),
}


def adapt(ev: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Map an Events-API item to (webhook_event_name, webhook_payload) or None if unsupported."""
    entry = ADAPTERS.get(ev.get("type", ""))
    if not entry:
        return None
    name, fn = entry
    payload = fn(ev)
    if name == "create" and (ev.get("payload") or {}).get("ref_type") == "repository":
        return "repository", payload
    return name, payload


# ----- cog --------------------------------------------------------------------------

class GithubPoller(commands.Cog):
    def __init__(self, bot: LabBot):
        self.bot = bot
        self.cfg = bot.gh_settings  # type: ignore[attr-defined]
        self.client: GithubClient = bot.gh_client  # type: ignore[attr-defined]
        self.dispatcher = get_dispatcher(bot)
        self.state = bot.state.namespace("gh:poll")
        self.reach = Reachability(bot, "GitHub API (poll)")
        self.etag: str | None = self.state.get("etag")
        if self.cfg.poll_enabled:
            self.poll.change_interval(seconds=self.cfg.poll_interval_s)
            self.poll.start()
            log.info("polling /orgs/%s/events every %ss", self.cfg.org, self.cfg.poll_interval_s)
        else:
            log.info("polling disabled (GITHUB_POLL_ENABLED=false)")

    async def cog_unload(self) -> None:
        self.poll.cancel()

    @tasks.loop(seconds=120)
    async def poll(self) -> None:
        try:
            status, events, etag, interval = await self.client.org_events(self.etag)
        except Exception as e:  # noqa: BLE001 - never crash the loop
            await self.reach.failure(e)
            return
        await self.reach.success()
        if interval > self.poll.seconds:
            log.info("GitHub asked for X-Poll-Interval=%ss; adjusting", interval)
            self.poll.change_interval(seconds=interval)
        if status == 304 or not events:
            return
        if etag and etag != self.etag:
            self.etag = etag
            self.state.set("etag", etag)

        last_id = int(self.state.get("last_event_id", 0) or 0)
        if last_id == 0:
            # First run: don't replay history, just remember where we are.
            self.state.set("last_event_id", int(events[0]["id"]))
            return
        fresh = [ev for ev in events if int(ev.get("id", 0)) > last_id]
        for ev in reversed(fresh):  # chronological
            mapped = adapt(ev)
            if mapped is None:
                continue
            name, payload = mapped
            try:
                await self.dispatcher.dispatch(name, payload, delivery_id=f"poll:{ev['id']}", source="poll",
                                               when=parse_ts(ev.get("created_at")))
            except Exception:  # noqa: BLE001
                log.exception("failed to dispatch polled event %s", ev.get("id"))
        if fresh:
            self.state.set("last_event_id", max(int(ev["id"]) for ev in fresh))

    @poll.before_loop
    async def _wait(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: LabBot) -> None:
    await bot.add_cog(GithubPoller(bot))
