"""The recap: what happened while you were asleep, read back out of the event log.

`build_digest(history, since, until, servers)` is pure — hand it a `periscope.history.History` (or the no-op
one) and a window and it gives back the embed: how many events each service logged, what alerted and whether it
came back, what was grabbed and imported, which builds failed. Nothing is fetched, nothing is posted; it only
reads what the bots already wrote, so a preview on the Messages page and the real 8 a.m. card come out of the
same code.

`digest_ctx()` is the same facts as plain values, which is what a customised `core.digest` template gets to work
with. The kind is registered on import, so the card is editable on the Messages page like any other.

`DigestSchedule` is the small hook a runtime can hang on a timer:

    schedule = DigestSchedule(hour=8, state=runtime.state.namespace("digest"))
    if schedule.due(now):
        since, until = schedule.window(now)
        embed = build_digest(runtime.history, since, until, servers)
        ...post it...
        schedule.mark(now)

`due()` answers once per day, at the first tick at or after the hour; `window()` is the span since the last
recap (capped, so a runtime that was off for a week does not report a week as one night).
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Iterable, Sequence

import discord

from .embeds import Severity, human_duration, lab_embed, truncate
from .history import ALERT_KINDS, RESOLVE_KINDS
from .messages import MessageKind, register

log = logging.getLogger(__name__)

DIGEST_KIND = "core.digest"
DEFAULT_HOUR = 8
MAX_WINDOW_H = 36          # a recap covers at most this much, however long the runtime was away
MIN_WINDOW_S = 300         # below this there is nothing worth saying
TOP_N = 6                  # rows listed per section before the rest are summed up in one line

GRAB_KINDS = ("grab", "grabbed")
IMPORT_KINDS = ("import", "imported", "upgrade")
CI_KINDS = ("ci", "ci_failed", "build")

SECTION_EMPTY = "nothing"


def _names(servers: Any) -> list[str]:
    """The servers to report on: a list of names, a {key: name} map, or a Store's `servers` — all fine."""
    if not servers:
        return []
    if isinstance(servers, dict):
        out = []
        for key, value in servers.items():
            name = value.get("name") if isinstance(value, dict) else value
            out.append(str(name or key))
        return [n for n in out if n]
    if isinstance(servers, str):
        return [servers]
    return [str(s) for s in servers if str(s)]


def _plural(n: int, one: str, many: str = "") -> str:
    return f"{n} {one}" if n == 1 else f"{n} {many or one + 's'}"


def _label(event: dict[str, Any]) -> str:
    """One line for an event: its title, else enough of the other columns to recognise it by."""
    title = str(event.get("title") or "").strip()
    if title:
        return truncate(title, 120)
    key = str(event.get("key") or "").strip()
    return truncate(f"{event.get('service') or '?'} {event.get('kind') or ''} {key}".strip(), 120)


def _clock(ts: float) -> str:
    return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).strftime("%H:%M")


def alert_story(alerts: list[dict[str, Any]], resolves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair each alert with the resolve that followed it, so the recap can say what is still open.

    An alert and its resolve are the same thing when they share a service and a key; the earliest resolve after
    the alert closes it. Anything with no resolve after it was still open when the window ended."""
    closed: set[int] = set()
    story: list[dict[str, Any]] = []
    for alert in sorted(alerts, key=lambda e: float(e.get("ts") or 0)):
        match = None
        for i, done in enumerate(sorted(resolves, key=lambda e: float(e.get("ts") or 0))):
            same = (done.get("service") == alert.get("service") and done.get("key") == alert.get("key")
                    and float(done.get("ts") or 0) >= float(alert.get("ts") or 0))
            if same and i not in closed:
                closed.add(i)
                match = done
                break
        story.append({
            "service": str(alert.get("service") or ""), "key": str(alert.get("key") or ""),
            "title": _label(alert), "severity": str(alert.get("severity") or "warning"),
            "ts": float(alert.get("ts") or 0), "at": _clock(alert.get("ts") or 0),
            "resolved": match is not None,
            "resolved_ts": float(match.get("ts") or 0) if match else 0.0,
            "down_s": (float(match["ts"]) - float(alert["ts"])) if match else 0.0,
        })
    return story


def digest_ctx(history: Any, since: float, until: float, servers: Any = ()) -> dict[str, Any]:
    """Everything the recap says, as plain values — the variables a `core.digest` template can use."""
    names = _names(servers)
    scope: dict[str, Any] = {"since": since, "until": until}
    events = list(history.events(since=since, until=until, limit=5000, newest_first=False))
    if names:
        # an event that never said which server it belongs to belongs to all of them, so it still counts
        events = [e for e in events if not str(e.get("server") or "") or str(e.get("server")) in names]
    by_service: dict[str, int] = {}
    for event in events:
        service = str(event.get("service") or "")
        by_service[service] = by_service.get(service, 0) + 1
    alerts = [e for e in events if e.get("kind") in ALERT_KINDS]
    resolves = [e for e in events if e.get("kind") in RESOLVE_KINDS]
    grabs = [e for e in events if e.get("kind") in GRAB_KINDS]
    imports = [e for e in events if e.get("kind") in IMPORT_KINDS]
    ci = [e for e in events if e.get("kind") in CI_KINDS
          and str(e.get("severity") or "") in ("warning", "critical")]
    story = alert_story(alerts, resolves)
    return {
        **scope,
        "hours": round(max(0.0, until - since) / 3600, 1),
        "servers": names,
        "total": len(events),
        "quiet": not events,
        "by_service": dict(sorted(by_service.items(), key=lambda kv: (-kv[1], kv[0]))),
        "alerts": story,
        "still_open": [a for a in story if not a["resolved"]],
        "recovered": [a for a in story if a["resolved"]],
        "grabs": [{"title": _label(e), "service": e.get("service"), "at": _clock(e.get("ts") or 0)} for e in grabs],
        "imports": [{"title": _label(e), "service": e.get("service"), "at": _clock(e.get("ts") or 0)}
                    for e in imports],
        "ci": [{"title": _label(e), "key": e.get("key"), "at": _clock(e.get("ts") or 0)} for e in ci],
    }


def _lines(rows: Sequence[dict[str, Any]], render, empty: str = SECTION_EMPTY) -> str:
    """At most TOP_N rows, then a line saying how many were left out."""
    if not rows:
        return empty
    out = [render(row) for row in rows[:TOP_N]]
    if len(rows) > TOP_N:
        out.append(f"…and {len(rows) - TOP_N} more")
    return truncate("\n".join(out), 1024)


def build_digest(history: Any, since: float, until: float, servers: Any = ()) -> discord.Embed:
    """The recap card for a window. Pure: it reads the log and returns an embed, nothing else."""
    data = digest_ctx(history, since, until, servers)
    return digest_embed(data, lab_name=", ".join(data["servers"]) or None)


def digest_embed(data: dict[str, Any], lab_name: str | None = None) -> discord.Embed:
    """Draw the recap from `digest_ctx()` data — the same drawing the Messages page previews."""
    span = human_duration(max(0.0, float(data["until"]) - float(data["since"])))
    open_now, back = data["still_open"], data["recovered"]
    if open_now:
        severity = Severity.CRITICAL if any(a["severity"] == "critical" for a in open_now) else Severity.WARNING
    elif back:
        severity = Severity.OK
    else:
        severity = Severity.INFO

    if data["quiet"]:
        body = f"Nothing was logged in the last {span}. A quiet night."
    else:
        head = [f"**{_plural(data['total'], 'thing')}** happened in the last {span}."]
        if open_now:
            head.append(f"⚠️ {_plural(len(open_now), 'alert')} still open.")
        elif back:
            head.append(f"Everything that alerted came back ({_plural(len(back), 'alert')}).")
        else:
            head.append("Nothing alerted.")
        body = " ".join(head)
    e = lab_embed("While you were asleep", body, severity=severity, lab_name=lab_name)

    if data["by_service"]:
        e.add_field(name="By service", inline=False,
                    value=truncate(" · ".join(f"**{name or 'core'}** {n}" for name, n in data["by_service"].items()),
                                   1024))
    if data["alerts"]:
        e.add_field(name=f"Alerts ({len(data['alerts'])})", inline=False, value=_lines(
            data["alerts"],
            lambda a: (f"{'🟢' if a['resolved'] else '🔴'} `{a['at']}` {a['title']}"
                       + (f" — back after {human_duration(a['down_s'])}" if a["resolved"] else " — still open"))))
    if data["grabs"] or data["imports"]:
        rows = [{"title": f"⬇️ {g['title']}", "at": g["at"]} for g in data["grabs"]]
        rows += [{"title": f"✅ {i['title']}", "at": i["at"]} for i in data["imports"]]
        e.add_field(name=f"Media ({len(data['grabs'])} grabbed, {len(data['imports'])} imported)", inline=False,
                    value=_lines(rows, lambda r: f"`{r['at']}` {r['title']}"))
    if data["ci"]:
        e.add_field(name=f"Builds that failed ({len(data['ci'])})", inline=False,
                    value=_lines(data["ci"], lambda c: f"`{c['at']}` {c['title']}"))
    return e


# ----- the scheduler hook ------------------------------------------------------------------------------
class DigestSchedule:
    """Once a day, at `hour`. Remembers when it last ran so a restart does not repeat the card.

    `state` is anything with `get(name, default)` / `set(name, value)` — a `JsonState` namespace does. Without
    one the schedule remembers in memory only, which is enough for a process that stays up.
    """

    def __init__(self, hour: int = DEFAULT_HOUR, *, minute: int = 0, state: Any = None,
                 max_window_h: float = MAX_WINDOW_H) -> None:
        self.hour = max(0, min(23, int(hour)))
        self.minute = max(0, min(59, int(minute)))
        self.max_window_h = float(max_window_h)
        self._state = state
        self._last = 0.0

    @property
    def last(self) -> float:
        if self._state is not None:
            try:
                return float(self._state.get("last_run", 0.0) or 0.0)
            except Exception:                              # noqa: BLE001 - a bad state file is not fatal
                return 0.0
        return self._last

    def mark(self, now: float | None = None) -> None:
        """Remember that the recap for this morning has gone out."""
        stamp = float(now if now is not None else time.time())
        self._last = stamp
        if self._state is not None:
            try:
                self._state.set("last_run", stamp)
            except Exception:                              # noqa: BLE001
                log.debug("could not remember the digest run", exc_info=True)

    def _today(self, now: float) -> float:
        moment = dt.datetime.fromtimestamp(now).replace(hour=self.hour, minute=self.minute, second=0,
                                                        microsecond=0)
        return moment.timestamp()

    def due(self, now: float | None = None) -> bool:
        """True on the first tick at or after today's hour, until `mark()` says it has been sent."""
        stamp = float(now if now is not None else time.time())
        target = self._today(stamp)
        if stamp < target:
            return False
        return self.last < target

    def window(self, now: float | None = None) -> tuple[float, float]:
        """What the recap covers: since the last one, capped at `max_window_h`, ending now."""
        stamp = float(now if now is not None else time.time())
        cap = stamp - self.max_window_h * 3600
        since = max(self.last, cap) if self.last else cap
        return min(since, stamp - MIN_WINDOW_S), stamp


async def post_digest(bot: Any, schedule: DigestSchedule, *, servers: Any = (), now: float | None = None,
                      channel_id: int | None = None) -> discord.Message | None:
    """Send this morning's recap if it is due. Returns the message, or None when it was not due or not sent.

    The card goes through `bot.messages.apply(core.digest, …)`, so a user who switched it off on the Messages
    page gets no card and a user who rewrote it gets their wording."""
    stamp = float(now if now is not None else time.time())
    if not schedule.due(stamp):
        return None
    since, until = schedule.window(stamp)
    schedule.mark(stamp)
    try:
        data = digest_ctx(bot.history, since, until, servers)
        embed = digest_embed(data, lab_name=getattr(bot, "lab_name", None))
        post = bot.messages.apply(DIGEST_KIND, embed, data)
        if post is None:
            return None
        cid = channel_id or getattr(bot.settings, "status_channel_id", None)
        if not cid:
            log.info("no status channel for the recap — skipping it")
            return None
        channel = await bot.get_channel_safe(int(cid))
        return await channel.send(embed=post) if channel is not None else None
    except Exception:                                      # noqa: BLE001 - a missed recap is not worth a crash
        log.exception("could not post the recap")
        return None


# ----- the message kind --------------------------------------------------------------------------------
class _SampleHistory:
    """A stand-in log for the preview: a night with one alert that came back and one that did not."""

    def events(self, **_kw: Any) -> list[dict[str, Any]]:
        base = 1_700_000_000.0
        return [
            {"ts": base + 60, "service": "docker", "kind": "down", "key": "sonarr", "severity": "critical",
             "title": "sonarr exited (code 1)", "detail": "", "value": None, "payload": {}},
            {"ts": base + 900, "service": "docker", "kind": "up", "key": "sonarr", "severity": "ok",
             "title": "sonarr is running again", "detail": "", "value": None, "payload": {}},
            {"ts": base + 1800, "service": "pve", "kind": "alert", "key": "pve1:cpu", "severity": "warning",
             "title": "High CPU on pve1", "detail": "", "value": None, "payload": {}},
            {"ts": base + 2400, "service": "arr", "kind": "grab", "key": "sonarr", "severity": "info",
             "title": "Grabbed: The Expanse S06E01", "detail": "", "value": None, "payload": {}},
            {"ts": base + 3000, "service": "arr", "kind": "import", "key": "sonarr", "severity": "ok",
             "title": "Imported: The Expanse S06E01", "detail": "", "value": None, "payload": {}},
            {"ts": base + 3600, "service": "github", "kind": "ci", "key": "anthill", "severity": "critical",
             "title": "CI failing: anthill / tests on main", "detail": "", "value": None, "payload": {}},
        ]


def _sample_digest() -> tuple[discord.Embed, dict[str, Any]]:
    since = 1_700_000_000.0
    data = digest_ctx(_SampleHistory(), since, since + 8 * 3600, ())
    return digest_embed(data, lab_name="my-lab"), data


VARIABLES = {
    "hours": "how many hours the recap covers",
    "total": "how many events were logged in the window",
    "quiet": "true when nothing at all was logged",
    "by_service": "how many events each service logged: a map of name to count",
    "alerts": "every alert: item.title · item.service · item.key · item.severity · item.at · item.resolved",
    "still_open": "the alerts that had not come back by the end of the window",
    "recovered": "the alerts that did, with item.down_s as how long they were away",
    "grabs": "what was grabbed: item.title · item.service · item.at",
    "imports": "what finished importing, the same shape",
    "ci": "the builds that failed: item.title · item.key · item.at",
    "servers": "the Discord servers this recap covers, by name",
}


def digest_kinds() -> tuple[MessageKind, ...]:
    """The recap as an editable message kind, so its wording can be changed on the Messages page."""
    return (
        MessageKind(DIGEST_KIND, "While you were asleep",
                    "one card each morning recapping the night: what alerted, what came back, what was "
                    "grabbed and imported, which builds failed",
                    where="the status channel", where_env="STATUS_CHANNEL_ID", sample=_sample_digest,
                    group="boards", variables=dict(VARIABLES)),
    )


register(*digest_kinds())


def kinds() -> Iterable[MessageKind]:
    """Older name for `digest_kinds()`, kept so a caller written against either one works."""
    return digest_kinds()
