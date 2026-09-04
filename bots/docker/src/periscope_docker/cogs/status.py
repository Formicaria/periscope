"""Live container board plus the alerts: exited, unhealthy, restart loops, daemon unreachable, image updates.

`DOCKER_POLL_S` drives the loop (and so how quickly an alert fires); the board message is edited no more often
than `STATUS_INTERVAL_S`, so a fast poll does not mean a fast edit rate against Discord.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import discord
from discord.ext import commands, tasks
from periscope import Alert, RefreshView, Severity, StatusBoard, human_duration, lab_embed, truncate
from periscope.hooks import NullHistory

from ..bot import DockerBot
from ..util import (
    RESTARTING,
    RUNNING,
    Container,
    chunk_lines,
    container_line,
    counts,
    images_in_use,
    sort_key,
)
from . import attach

log = logging.getLogger(__name__)
# a bot assembled by hand (a test, a bare install) has no event log; recording is never worth a crash
NO_LOG = NullHistory()

BOARD_KIND = "docker.board"
FP_UNREACHABLE = "docker:unreachable"
FP_UPDATES = "docker:updates"
CONTAINER_FP = "docker:container:"
MAX_FAILURES = 3          # consecutive poll failures before the daemon counts as unreachable
RESTART_WINDOW_S = 3600   # restarts inside this window are what DOCKER_RESTART_LOOP_N counts
UPDATE_REMIND_S = 7 * 86400   # how long before an unchanged "images have updates" notice is posted again
CRITICAL_TROUBLE = ("crashed", "dead")
WARNING_TROUBLE = ("unhealthy", "restarting", "removing")
BOARD_ROWS = 30           # containers listed on the board; the rest are summed up in one line, well inside
                          # Discord's 6000-character limit for one embed


def fingerprint(name: str, condition: str) -> str:
    return f"{CONTAINER_FP}{name}:{condition}"


def name_of(fp: str) -> str:
    """The container name inside a `docker:container:<name>:<condition>` fingerprint."""
    parts = fp.split(":")
    return parts[2] if len(parts) > 3 else ""


def restarted(previous: str, c: Container, poll_s: int) -> bool:
    """Did this container restart between the previous poll and this one?

    Two things give it away: it was not running last time and is running now, or it is running but has been up
    for less time than one poll — which is how a container that crash-loops faster than the poll looks."""
    if not previous:
        return False                       # first sight of it: there is nothing to compare against
    if c.state == RESTARTING:
        return previous != RESTARTING      # count one restart per spell in the daemon's restart loop
    if not c.running:
        return False
    if previous != RUNNING:
        return True
    return c.uptime_s is not None and c.uptime_s < poll_s


# ----- the board ---------------------------------------------------------


def board_ctx(version: str, label: str, containers: list[Container], updates: list[str],
              checking_updates: bool) -> dict[str, Any]:
    """The facts the board is drawn from, as plain values (also the variables a customised board can use)."""
    rows = sorted(containers, key=sort_key)
    return {
        "version": version,
        "endpoint": label,
        "counts": counts(rows),
        "containers": [{"name": c.name, "image": c.tag, "state": c.state, "health": c.health,
                        "trouble": c.trouble, "uptime_s": c.uptime_s, "exit_code": c.exit_code,
                        "cpu": round(c.cpu_pct, 1) if c.cpu_pct is not None else None,
                        "mem": c.mem_used, "line": container_line(c)} for c in rows],
        "down": [c.name for c in rows if not c.running],
        "updates": list(updates),
        "checking_updates": bool(checking_updates),
    }


def board_severity(containers: list[dict[str, Any]]) -> Severity:
    if any(c["trouble"] in CRITICAL_TROUBLE for c in containers):
        return Severity.CRITICAL
    if any(c["trouble"] in WARNING_TROUBLE for c in containers):
        return Severity.WARNING
    return Severity.OK


def board_embed(data: dict[str, Any], lab_name: str | None) -> discord.Embed:
    n = data["counts"]
    head = [f"**{n['running']}/{n['total']}** containers running"]
    if n["unhealthy"]:
        head.append(f"{n['unhealthy']} unhealthy")
    if n["restarting"]:
        head.append(f"{n['restarting']} restarting")
    if n["stopped"]:
        head.append(f"{n['stopped']} stopped")
    desc = " · ".join(head) + f"\n🐳 {data['version'] or '?'} · `{truncate(data['endpoint'], 60)}`"
    e = lab_embed(f"Docker — {truncate(data['endpoint'], 40)}", desc,
                  severity=board_severity(data["containers"]), lab_name=lab_name)
    rows = [c["line"] for c in data["containers"][:BOARD_ROWS]]
    hidden = len(data["containers"]) - len(rows)
    if hidden > 0:
        rows.append(f"…and {hidden} more")
    if rows:
        for i, block in enumerate(chunk_lines(rows)):
            e.add_field(name=f"Containers ({n['total']})" if i == 0 else "\u200b",
                        value=truncate(block, 1024), inline=False)
    else:
        e.add_field(name="Containers", value="none match DOCKER_INCLUDE / DOCKER_IGNORE", inline=False)
    if data["checking_updates"]:
        updates = data["updates"]
        value = truncate(" · ".join(f"`{ref}`" for ref in updates), 1024) if updates else "everything is current"
        e.add_field(name=f"⬆ Images with updates ({len(updates)})", value=value, inline=False)
    return e


# ----- the cog -----------------------------------------------------------


class StatusCog(commands.Cog):
    def __init__(self, bot: DockerBot):
        self.bot = bot
        self.history = getattr(bot, "history", NO_LOG)   # a no-op when this bot has none
        self.cfg = bot.cfg
        self.board = StatusBoard(bot, key="docker", kind=BOARD_KIND)
        self.view = RefreshView(self.build_board, custom_id="docker:refresh")
        self.state = bot.state.namespace("docker")
        self._failures = 0
        self._version = ""
        self._rendered = 0.0
        self._checked_updates = 0.0
        self._updates: list[str] = list(self.state.get("updates", []) or [])
        self._seen: dict[str, str] = dict(self.state.get("seen_state", {}) or {})
        self._restarts: dict[str, list[float]] = {k: list(v) for k, v in (self.state.get("restarts", {}) or {}).items()}
        self._active: set[str] = set()
        self.tick.change_interval(seconds=max(10, self.cfg.poll_s))

    async def cog_load(self) -> None:
        self.bot.add_view(self.view)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    # ----- polling loop -------------------------------------------------

    @tasks.loop(seconds=60)
    async def tick(self) -> None:
        try:
            containers = await self.snapshot()
        except Exception as e:  # never let the loop die
            self._failures += 1
            log.warning("Docker poll failed (%d in a row): %s", self._failures, e)
            if self._failures == MAX_FAILURES:
                await self._safe(self.bot.alerts.fire(Alert(
                    FP_UNREACHABLE, "Docker unreachable",
                    f"{MAX_FAILURES} consecutive polls of {self.cfg.endpoint} failed.\n"
                    f"Last error: `{truncate(str(e), 300)}`", severity=Severity.CRITICAL)))
            return
        if self._failures >= MAX_FAILURES:
            await self._safe(self.bot.alerts.resolve(FP_UNREACHABLE, "The daemon is answering again"))
        self._failures = 0
        try:
            await self.evaluate(containers)
        except Exception:
            log.exception("alert evaluation failed")
        now = time.time()
        if not self.board.channel_id or now - self._rendered < self.bot.settings.status_interval_s - 1:
            return  # no status channel, or the board was edited recently enough
        self._rendered = now
        data = self.data(containers)
        self.history.sample(service="docker", metric="running", value=data["counts"]["running"],
                            server=self.bot.lab_name)
        for c in (x for x in containers if x.cpu_pct is not None):
            self.history.sample(service="docker", metric="cpu", value=c.cpu_pct, key=c.name, server=self.bot.lab_name)
        try:
            await self.board.render(board_embed(data, self.bot.lab_name), view=self.view, ctx=data)
        except Exception:
            log.exception("status board render failed")

    @tick.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def snapshot(self) -> list[Container]:
        """One poll: the watched containers, their cpu/memory when affordable, and the update check when due."""
        containers = [c for c in await self.bot.docker.containers() if self.cfg.watches(c.name)]
        if not self._version:
            try:
                self._version = str((await self.bot.docker.version()).get("Version") or "")
            except Exception as e:  # the version is decoration; a failure here must not fail the poll
                log.debug("version lookup failed: %s", e)
        await self.bot.docker.sample(containers)
        await self.check_updates(containers)
        return containers

    async def check_updates(self, containers: list[Container]) -> None:
        if not self.cfg.check_updates:
            return
        now = time.time()
        if self._checked_updates and now - self._checked_updates < self.cfg.update_check_h * 3600:
            return
        self._checked_updates = now
        found = await self.bot.docker.updates(images_in_use(containers))
        self._updates = [u["ref"] for u in found]
        self.state.set("updates", self._updates)

    def data(self, containers: list[Container]) -> dict[str, Any]:
        return board_ctx(self._version, self.cfg.label, containers, self._updates, self.cfg.check_updates)

    async def build_board(self) -> discord.Embed:
        """Used by the 🔄 RefreshView button."""
        try:
            return board_embed(self.data(await self.snapshot()), self.bot.lab_name)
        except Exception as e:
            return lab_embed("Docker", f"{self.cfg.endpoint} is unreachable: `{truncate(str(e), 300)}`",
                             severity=Severity.CRITICAL, lab_name=self.bot.lab_name)

    @staticmethod
    async def _safe(coro) -> None:
        try:
            await coro
        except Exception:
            log.exception("alert delivery failed")

    async def _resolve(self, fp: str, note: str) -> None:
        """Resolve only what is actually open, so a healthy poll writes no state."""
        if fp in self._active:
            await self._safe(self.bot.alerts.resolve(fp, note))
            self._active.discard(fp)

    # ----- alerts -------------------------------------------------------

    async def evaluate(self, containers: list[Container]) -> None:
        self._active = set(self.bot.alerts.active())
        now = time.time()
        seen: dict[str, str] = {}
        for c in sorted(containers, key=lambda c: c.name):
            seen[c.name] = c.state
            if self._seen.get(c.name) != c.state:      # first sight of it, or it has changed since the last poll
                self.history.record(service="docker", kind="up" if c.running else "down", key=c.name,
                                    severity="ok" if c.running else
                                             ("critical" if c.trouble in CRITICAL_TROUBLE else "warning"),
                                    title=f"{c.name} is {c.state}", detail=c.status or "",
                                    server=self.bot.lab_name, payload={"image": c.tag, "exit_code": c.exit_code})
            await self.check_state(c)
            await self.check_health(c)
            await self.check_restart_loop(c, now)
        await self.forget_removed(seen)
        self._seen = seen
        # containers that are gone take their restart history with them, so it cannot grow without end
        self._restarts = {k: v for k, v in self._restarts.items() if k in seen}
        self.state.set("seen_state", seen)
        self.state.set("restarts", {k: v for k, v in self._restarts.items() if v})
        await self.check_update_alert(now)

    async def check_state(self, c: Container) -> None:
        """A container that is not running: an alert when it crashed, and when DOCKER_ALERT_ON_STOP says so."""
        fp = fingerprint(c.name, "exited")
        if c.running or c.state in (RESTARTING, "created", "paused"):
            await self._resolve(fp, f"`{c.name}` is running again")
            return
        code = c.exit_code
        crashed = bool(code) or c.state == "dead"
        if not crashed and not self.cfg.alert_on_stop:
            return  # someone stopped it on purpose, and that is not news
        await self._safe(self.bot.alerts.fire(Alert(
            fp, f"Container {'crashed' if crashed else 'stopped'}: {c.name}",
            f"`{c.tag}` is `{c.state}`" + (f" with exit code **{code}**." if code is not None else "."),
            severity=Severity.CRITICAL if crashed else Severity.WARNING,
            fields={"Image": f"`{c.tag}`", "Container": f"`{c.short_id}`", "Status": c.status or "—"})))

    async def check_health(self, c: Container) -> None:
        fp = fingerprint(c.name, "unhealthy")
        if c.running and c.unhealthy:
            await self._safe(self.bot.alerts.fire(Alert(
                fp, f"Health check failing: {c.name}",
                f"`{c.tag}` is up but its health check reports **unhealthy**.", severity=Severity.WARNING,
                fields={"Status": c.status or "—", "Up": human_duration(c.uptime_s)})))
        else:
            await self._resolve(fp, f"`{c.name}` is healthy again")

    async def check_restart_loop(self, c: Container, now: float) -> None:
        fp = fingerprint(c.name, "restart_loop")
        history = [t for t in self._restarts.get(c.name, []) if now - t < RESTART_WINDOW_S]
        if restarted(self._seen.get(c.name, ""), c, self.cfg.poll_s):
            history.append(now)
        self._restarts[c.name] = history
        if len(history) >= self.cfg.restart_loop_n:
            await self._safe(self.bot.alerts.fire(Alert(
                fp, f"Restart loop: {c.name}",
                f"`{c.tag}` restarted **{len(history)}** times in the last hour "
                f"(threshold {self.cfg.restart_loop_n}).", severity=Severity.WARNING,
                fields={"State": c.state, "Status": c.status or "—"})))
        elif not history:
            await self._resolve(fp, f"`{c.name}` has settled")

    async def forget_removed(self, seen: dict[str, str]) -> None:
        """Containers that no longer exist cannot recover on their own — close their alerts rather than leave
        them open forever."""
        for fp in sorted(self._active):
            if not fp.startswith(CONTAINER_FP):
                continue
            name = name_of(fp)
            if name and name not in seen:
                await self._resolve(fp, f"`{name}` no longer exists on this host")
                self._restarts.pop(name, None)

    async def check_update_alert(self, now: float) -> None:
        """One INFO alert listing the images with a newer digest, re-posted when the list changes or weekly."""
        if not self.cfg.check_updates:
            return
        if not self._updates:
            await self._resolve(FP_UPDATES, "every image is current")
            if self.state.get("updates_posted"):
                self.state.pop("updates_posted")
            return
        posted = self.state.get("updates_posted") or {}
        changed = list(posted.get("refs") or []) != self._updates
        stale = now - float(posted.get("ts") or 0) > UPDATE_REMIND_S
        if not changed and not stale:
            return
        listed = "\n".join(f"• `{ref}`" for ref in self._updates[:20])
        more = f"\n…and {len(self._updates) - 20} more" if len(self._updates) > 20 else ""
        await self._safe(self.bot.alerts.fire(Alert(
            FP_UPDATES, f"{len(self._updates)} image{'s' if len(self._updates) != 1 else ''} have updates",
            f"The registry serves a newer digest than the one these are running:\n{listed}{more}",
            severity=Severity.INFO, mention=False), force=True))
        self.state.set("updates_posted", {"refs": self._updates, "ts": now})


async def setup(bot: DockerBot) -> None:
    cog = StatusCog(bot)
    await bot.add_cog(cog)
    attach(bot, cog)
