"""CI train: one live message per Actions run, edited in place as its jobs move.

    🟡 ci · run #42 on main
    ✅ lint            12s
    🟡 test (core)     step 4/7 · pytest
    ⚪ test (arr)
    ⚪ docker

The run is discovered by the poller (or a workflow_run webhook), a card is posted to the CI channel
immediately, and `tick()` refreshes every tracked run until it completes. Finished runs get a final
green/red edit with the total duration; the alert state machine still fires for default-branch failures.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import discord
from periscope import LabBot, human_duration, lab_embed, truncate

from .render import GREEN, GREY, RED, YELLOW, parse_ts

log = logging.getLogger(__name__)

JOB_ICON = {
    "queued": "⚪", "waiting": "⚪", "pending": "⚪", "requested": "⚪",
    "in_progress": "🟡",
    "success": "✅", "failure": "❌", "cancelled": "⏹️", "skipped": "⏭️", "timed_out": "⏰",
    "neutral": "⚪", "action_required": "⚠️", "stale": "⚪",
}
RUN_COLOR = {"success": GREEN, "failure": RED, "timed_out": RED, "cancelled": GREY, "skipped": GREY}
MAX_JOB_LINES = 25


def job_state(job: dict[str, Any]) -> str:
    return (job.get("conclusion") if job.get("status") == "completed" else job.get("status")) or "queued"


def job_line(job: dict[str, Any]) -> str:
    state = job_state(job)
    icon = JOB_ICON.get(state, "⚪")
    name = truncate(job.get("name") or "job", 40)
    steps = job.get("steps") or []
    if state == "in_progress":
        done = sum(1 for s in steps if s.get("status") == "completed")
        current = next((s.get("name") for s in steps if s.get("status") == "in_progress"), None)
        detail = f"step {min(done + 1, len(steps))}/{len(steps)}" if steps else "running"
        if current:
            detail += f" · {truncate(current, 30)}"
    elif job.get("status") == "completed":
        started, ended = parse_ts(job.get("started_at")), parse_ts(job.get("completed_at"))
        detail = human_duration((ended - started).total_seconds()) if started and ended else state.replace("_", " ")
    else:
        detail = state.replace("_", " ")
    return f"{icon} **{name}**  {detail}"


def render_train(repo: str, run: dict[str, Any], jobs: list[dict[str, Any]], lab: str) -> discord.Embed:
    status = run.get("status") or "queued"
    conclusion = run.get("conclusion")
    done = status == "completed"
    head_icon = JOB_ICON.get(conclusion or status, "🟡") if done else ("🟡" if status == "in_progress" else "⚪")
    color = RUN_COLOR.get(conclusion or "", GREY) if done else (YELLOW if status == "in_progress" else GREY)
    title = f"[{repo}] {head_icon} {run.get('name', 'workflow')} · run #{run.get('run_number', '?')} on {run.get('head_branch', '?')}"
    started = parse_ts(run.get("run_started_at") or run.get("created_at"))
    ended = parse_ts(run.get("updated_at")) if done else dt.datetime.now(dt.timezone.utc)
    elapsed = human_duration((ended - started).total_seconds()) if started and ended else "—"
    who = (run.get("triggering_actor") or run.get("actor") or {}).get("login") or "?"
    head = f"`{(run.get('head_sha') or '')[:7]}` {truncate(run.get('display_title') or '', 70)} — {who}"
    tail = f"{(conclusion or status).replace('_', ' ')} in {elapsed}" if done else f"{status.replace('_', ' ')} · {elapsed}"
    lines = [job_line(j) for j in jobs[:MAX_JOB_LINES]]
    if len(jobs) > MAX_JOB_LINES:
        lines.append(f"… {len(jobs) - MAX_JOB_LINES} more jobs")
    if not lines:
        lines.append("⚪ waiting for jobs…")
    n_done = sum(1 for j in jobs if j.get("status") == "completed")
    desc = f"{head}\n**{tail}** · jobs {n_done}/{len(jobs)}\n\n" + "\n".join(lines)
    e = lab_embed(truncate(title, 256), truncate(desc, 4096), lab_name=lab, color=color, url=run.get("html_url"))
    if who != "?":
        actor = run.get("triggering_actor") or run.get("actor") or {}
        e.set_author(name=who, url=actor.get("html_url"), icon_url=actor.get("avatar_url"))
    return e


class CiTrains:
    """Tracks live runs: state is {run_id: {repo, channel, message, started}} in bot state ("gh:trains")."""

    def __init__(self, bot: LabBot, client, cfg, on_complete=None):
        self.bot = bot
        self.client = client
        self.cfg = cfg
        self.on_complete = on_complete  # async callable(repo, run) after the final edit
        self.state = bot.state.namespace("gh:trains")

    # -- persistence -------------------------------------------------------------------------
    def tracked(self) -> dict[str, dict[str, Any]]:
        return dict(self.state.get("runs", {}) or {})

    def _save(self, runs: dict[str, dict[str, Any]]) -> None:
        self.state.set("runs", runs)

    def is_tracked(self, run_id: int | str) -> bool:
        return str(run_id) in self.tracked()

    # -- lifecycle ---------------------------------------------------------------------------
    async def start(self, repo: str, run: dict[str, Any]) -> bool:
        """Post the initial card for a run that is not finished yet. Returns True if a card was posted."""
        rid = str(run["id"])
        runs = self.tracked()
        if rid in runs:
            return False
        channel_id = self.cfg.ci_channel_id or self.cfg.feed_channel_id or self.bot.settings.alert_channel_id
        if not channel_id:
            return False
        ch = await self.bot.get_channel_safe(channel_id)
        if ch is None:
            return False
        jobs = await self._jobs(repo, run["id"])
        try:
            msg = await ch.send(embed=render_train(repo, run, jobs, self.bot.lab_name))
        except discord.HTTPException as e:
            log.error("could not post CI train for %s#%s: %s", repo, rid, e)
            return False
        runs[rid] = {"repo": repo, "channel": channel_id, "message": msg.id,
                     "started": dt.datetime.now(dt.timezone.utc).isoformat()}
        self._save(runs)
        return True

    async def tick(self) -> None:
        """Refresh every tracked run; finalize the ones that completed."""
        runs = self.tracked()
        if not runs:
            return
        for rid, info in list(runs.items()):
            repo = info["repo"]
            try:
                run = await self.client.workflow_run(repo, int(rid))
                jobs = await self._jobs(repo, int(rid))
            except Exception:  # noqa: BLE001
                log.debug("train %s/%s: refresh failed", repo, rid, exc_info=True)
                continue
            await self._edit(info, render_train(repo, run, jobs, self.bot.lab_name))
            if run.get("status") == "completed":
                runs.pop(rid, None)
                self._save(runs)
                if self.on_complete:
                    try:
                        await self.on_complete(repo, run)
                    except Exception:  # noqa: BLE001
                        log.exception("on_complete failed for %s#%s", repo, rid)
            else:
                # give up on trains older than a day (deleted runs, lost messages)
                started = dt.datetime.fromisoformat(info["started"])
                if dt.datetime.now(dt.timezone.utc) - started > dt.timedelta(days=1):
                    runs.pop(rid, None)
                    self._save(runs)

    # -- helpers -----------------------------------------------------------------------------
    async def _jobs(self, repo: str, run_id: int) -> list[dict[str, Any]]:
        try:
            return await self.client.workflow_run_jobs(repo, run_id)
        except Exception:  # noqa: BLE001
            log.debug("jobs unavailable for %s#%s", repo, run_id, exc_info=True)
            return []

    async def _edit(self, info: dict[str, Any], embed: discord.Embed) -> None:
        ch = await self.bot.get_channel_safe(info["channel"])
        if ch is None:
            return
        try:
            msg = await ch.fetch_message(info["message"])
            await msg.edit(embed=embed)
        except discord.NotFound:
            log.info("CI train message %s vanished; reposting", info["message"])
            try:
                msg = await ch.send(embed=embed)
                info["message"] = msg.id
                runs = self.tracked()
                for rid, i in runs.items():
                    if i is info or i.get("message") == info["message"]:
                        runs[rid] = info
                self._save(runs)
            except discord.HTTPException:
                pass
        except discord.HTTPException as e:
            log.warning("CI train edit failed: %s", e)
