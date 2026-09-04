"""Maintenance windows: the times periscope is meant to stay quiet.

`bot.windows` is an instance of `Windows` as soon as this module is importable (see `periscope.hooks`). A send
site asks one question before it pages anyone:

    if bot.windows.quiet("proxmox", server="main", key=fingerprint):
        return          # a window is open: log it, record it, post nothing

Everything here fails open. A missing file, a malformed file, a window with a time nobody can parse — all of
it is recorded in `self.errors` and then ignored, so `quiet()` answers False and the alert goes out. Silence
is never the accident.

The file is `config/maintenance.yaml`, next to `periscope.yaml`, and is re-read whenever it changes on disk,
so an edit on the /alerts page is live on the next poll without a restart:

    version: 1

    # switch everything off until a moment in time — the big red button for a whole-rack job
    quiet_until: "2026-09-05T02:00:00"
    quiet_reason: "Rack power work"

    # how the clock times below are read: "local" (default), "UTC", or a zone name like "Europe/Berlin"
    timezone: local

    windows:
      # a repeating window: weekdays + a time range. An end earlier than the start runs past midnight.
      - id: nightly-backups
        reason: "Nightly backups — the disks are meant to be busy"
        enabled: true
        days: [mon, tue, wed, thu, fri]
        start: "01:00"
        end: "04:30"
        services: [proxmox, docker]     # empty or absent: every service
        servers: [main]                 # empty or absent: every Discord server
        keys: ["docker:container:*"]    # empty or absent: every alert; matched against the fingerprint

      # a one-off window: two full timestamps, and it expires on its own
      - id: pve1-rebuild
        reason: "Rebuilding pve1"
        start: "2026-09-06T20:00:00"
        end: "2026-09-07T02:00:00"
        services: [proxmox]

Times without a zone are read in the file's `timezone`. Fingerprint patterns are shell globs (fnmatch).
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import logging
import os
import re
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LABELS = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}
DAY_INDEX = {name: i for i, name in enumerate(DAY_NAMES)}
# what people actually type: "Monday", "MON", "Weds", "1" (Monday-first, the way the UI numbers them)
DAY_ALIASES = {**{n: n for n in DAY_NAMES},
               "monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu", "friday": "fri",
               "saturday": "sat", "sunday": "sun",
               "tues": "tue", "thur": "thu", "thurs": "thu", "weds": "wed", "wednes": "wed", "satur": "sat"}
WEEKDAYS, WEEKEND, EVERY_DAY = ["mon", "tue", "wed", "thu", "fri"], ["sat", "sun"], list(DAY_NAMES)

CLOCK_RE = re.compile(r"^(\d{1,2})[:.h]?(\d{2})?$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
DEFAULT_NAME = "maintenance.yaml"


# ----- small parsers ----------------------------------------------------------------------------------------
def normalise_days(raw: Any) -> list[str]:
    """['Mon', 'weekdays', 3] → ['mon', 'tue', …]. Unknown names are dropped, order follows the week."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        raw = [p for p in re.split(r"[,\s]+", raw) if p]
    if not isinstance(raw, Iterable):
        raw = [raw]
    out: set[str] = set()
    for item in raw:
        token = str(item).strip().lower()
        if not token:
            continue
        if token in ("weekday", "weekdays"):
            out.update(WEEKDAYS)
        elif token in ("weekend", "weekends"):
            out.update(WEEKEND)
        elif token in ("all", "daily", "every day", "everyday"):
            out.update(EVERY_DAY)
        elif token in DAY_ALIASES:
            out.add(DAY_ALIASES[token])
        elif token.isdigit() and 1 <= int(token) <= 7:
            out.add(DAY_NAMES[int(token) - 1])
    return [d for d in DAY_NAMES if d in out]


def parse_clock(raw: Any) -> int | None:
    """'01:00' → 60 (minutes past midnight). '24:00' is the end of the day. None when it is not a clock time."""
    text = str(raw or "").strip()
    if not text:
        return None
    m = CLOCK_RE.match(text)
    if not m:
        return None
    hours, minutes = int(m.group(1)), int(m.group(2) or 0)
    if hours == 24 and minutes == 0:
        return 24 * 60
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        return None
    return hours * 60 + minutes


DATE_RE = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}")


def looks_like_a_date(raw: Any) -> bool:
    """Is this a one-off window's timestamp rather than a clock time? A calendar date is what tells them apart."""
    return bool(DATE_RE.search(str(raw or "")))


def parse_moment(raw: Any, tz: dt.tzinfo) -> float | None:
    """An ISO-ish timestamp → epoch seconds. Naive values are read in `tz`. None when it will not parse."""
    text = str(raw or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00").replace("/", "-")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                moment = dt.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return moment.timestamp()


def load_timezone(name: str) -> tuple[dt.tzinfo, str]:
    """The file's `timezone` as a tzinfo, plus a plain-language problem when the name is not one we know."""
    text = (name or "").strip()
    if not text or text.lower() == "local":
        return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc, ""
    if text.lower() in ("utc", "z", "gmt"):
        return dt.timezone.utc, ""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(text), ""
    except Exception:  # noqa: BLE001 - an unknown zone must not take the windows down
        local = dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
        return local, f"timezone {text!r} is not a zone this box knows — using the local clock instead"


def as_list(raw: Any) -> list[str]:
    """A YAML list, a comma-separated string or a single value → a list of trimmed lowercase strings."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        return [p.strip().lower() for p in raw.split(",") if p.strip()]
    if isinstance(raw, (list, tuple, set)):
        return [str(p).strip().lower() for p in raw if str(p).strip()]
    return [str(raw).strip().lower()]


def slugify(text: str, taken: Iterable[str] = ()) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")[:40] or "window"
    if not ID_RE.match(base):
        base = "window"
    used, candidate, n = set(taken), base, 2
    while candidate in used:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


# ----- one window -------------------------------------------------------------------------------------------
@dataclass
class Window:
    """One entry under `windows:`. Repeating when `start`/`end` are clock times, one-off when they are dates."""

    id: str
    reason: str = ""
    enabled: bool = True
    days: list[str] = field(default_factory=list)
    start: str = ""
    end: str = ""
    servers: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    one_off: bool = False
    start_min: int | None = None     # repeating: minutes past midnight
    end_min: int | None = None
    start_ts: float | None = None    # one-off: epoch seconds
    end_ts: float | None = None
    problem: str = ""                # why this window does nothing; it is then never open

    # ----- scope ------------------------------------------------------------------------------------
    def covers(self, service: str = "", *, server: str = "", key: str = "") -> bool:
        """Does this window apply to that alert? An empty filter means 'everything'; an alert whose service or
        server the caller could not name only matches windows that do not filter on it."""
        if self.services and str(service or "").lower() not in self.services:
            return False
        if self.servers and str(server or "").lower() not in self.servers:
            return False
        if self.keys:
            fp = str(key or "")
            if not fp or not any(fnmatch.fnmatch(fp, pattern) for pattern in self.keys):
                return False
        return True

    # ----- clock ------------------------------------------------------------------------------------
    def open_at(self, ts: float, tz: dt.tzinfo) -> bool:
        if not self.enabled or self.problem:
            return False
        if self.one_off:
            return self.start_ts is not None and self.end_ts is not None and self.start_ts <= ts < self.end_ts
        if self.start_min is None or self.end_min is None:
            return False
        local = dt.datetime.fromtimestamp(ts, tz)
        minute, today = local.hour * 60 + local.minute, DAY_NAMES[local.weekday()]
        days = self.days or EVERY_DAY
        if self.end_min > self.start_min:
            return today in days and self.start_min <= minute < self.end_min
        # runs past midnight: the tail belongs to the day the window started on
        if minute >= self.start_min:
            return today in days
        yesterday = DAY_NAMES[(local.weekday() - 1) % 7]
        return minute < self.end_min and yesterday in days

    def ends_at(self, ts: float, tz: dt.tzinfo) -> float | None:
        """When the window that is open at `ts` closes, as epoch seconds (None when it is not open)."""
        if not self.open_at(ts, tz):
            return None
        if self.one_off:
            return self.end_ts
        local = dt.datetime.fromtimestamp(ts, tz)
        minute = local.hour * 60 + local.minute
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = self.end_min or 0
        closes = midnight + dt.timedelta(minutes=end)
        if end <= (self.start_min or 0) and minute >= (self.start_min or 0):
            closes += dt.timedelta(days=1)          # the tail is tomorrow morning
        return closes.timestamp()

    # ----- words ------------------------------------------------------------------------------------
    def when(self) -> str:
        """'Mon–Fri, 01:00–04:30' / 'Once: 6 Sep 20:00 → 7 Sep 02:00'."""
        if self.problem:
            return self.problem
        if self.one_off:
            if self.start_ts is None or self.end_ts is None:
                return "once (times not set)"
            fmt = "%-d %b %H:%M" if os.name != "nt" else "%d %b %H:%M"
            try:
                first = dt.datetime.fromtimestamp(self.start_ts).strftime(fmt)
                last = dt.datetime.fromtimestamp(self.end_ts).strftime(fmt)
            except (ValueError, OSError):
                return "once"
            return f"once: {first} → {last}"
        return f"{self.day_words()}, {self.start}–{self.end}"

    def day_words(self) -> str:
        days = self.days or EVERY_DAY
        if days == EVERY_DAY:
            return "every day"
        if days == WEEKDAYS:
            return "Mon–Fri"
        if days == WEEKEND:
            return "Sat & Sun"
        return " ".join(DAY_LABELS[d] for d in days)

    def scope_words(self) -> str:
        """'every service, every server' / 'proxmox, docker on main'."""
        who = ", ".join(self.services) if self.services else "every service"
        where = ", ".join(self.servers) if self.servers else "every server"
        text = f"{who} on {where}"
        if self.keys:
            text += " · alerts matching " + ", ".join(self.keys)
        return text

    def to_dict(self) -> dict[str, Any]:
        """The shape the file holds — only the keys that carry something, so hand-edits stay readable."""
        out: dict[str, Any] = {"id": self.id}
        if self.reason:
            out["reason"] = self.reason
        if not self.enabled:
            out["enabled"] = False
        if self.days and not self.one_off:
            out["days"] = list(self.days)
        out["start"], out["end"] = self.start, self.end
        for name, value in (("servers", self.servers), ("services", self.services), ("keys", self.keys)):
            if value:
                out[name] = list(value)
        return out


def build_window(raw: dict[str, Any], tz: dt.tzinfo, taken: Iterable[str] = ()) -> Window:
    """One `windows:` entry → a Window. A window that cannot be read carries its `problem` and stays shut."""
    reason = str(raw.get("reason") or raw.get("why") or "").strip()
    wid = str(raw.get("id") or "").strip().lower()
    if not ID_RE.match(wid):
        wid = slugify(wid or reason, taken)
    win = Window(id=wid, reason=reason, enabled=raw.get("enabled", True) is not False,
                 days=normalise_days(raw.get("days") or raw.get("weekdays")),
                 start=str(raw.get("start") or raw.get("from") or "").strip(),
                 end=str(raw.get("end") or raw.get("until") or raw.get("to") or "").strip(),
                 servers=as_list(raw.get("servers") or raw.get("server")),
                 services=as_list(raw.get("services") or raw.get("service")),
                 keys=as_list(raw.get("keys") or raw.get("key") or raw.get("fingerprints")))
    win.one_off = looks_like_a_date(win.start) or looks_like_a_date(win.end)
    if win.one_off:
        win.start_ts, win.end_ts = parse_moment(win.start, tz), parse_moment(win.end, tz)
        if win.start_ts is None or win.end_ts is None:
            win.problem = f"{win.id}: a one-off window needs a start and an end date it can read"
        elif win.end_ts <= win.start_ts:
            win.problem = f"{win.id}: the one-off window ends before it starts"
    else:
        win.start_min, win.end_min = parse_clock(win.start), parse_clock(win.end)
        if win.start_min is None or win.end_min is None:
            win.problem = f"{win.id}: start and end must be clock times like 01:00 (or full dates for a one-off)"
        elif win.start_min == win.end_min:
            win.problem = f"{win.id}: the window starts and ends at the same minute, so it never opens"
    return win


# ----- the file ---------------------------------------------------------------------------------------------
def blank() -> dict[str, Any]:
    return {"version": 1, "timezone": "local", "quiet_until": "", "quiet_reason": "", "windows": []}


class Windows:
    """`bot.windows` when maintenance windows are configured. Never raises out of `quiet`/`active`/`reason`."""

    enabled = True

    def __init__(self, path: str | os.PathLike, *, clock: Any = None):
        self.path = Path(path)
        self.clock = clock or time.time     # injectable so tests can stand at a chosen minute
        self.errors: list[str] = []
        self.tz: dt.tzinfo = dt.timezone.utc
        self.windows: list[Window] = []
        self.data: dict[str, Any] = blank()
        self._mtime: float = -1.0
        self.reload(force=True)

    # ----- io -----------------------------------------------------------------------------------------
    def reload(self, force: bool = False) -> None:
        """Re-read the file when it changed on disk. A file that will not parse leaves nothing quiet."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            if force or self._mtime != -1.0:
                self.data, self.windows, self.errors, self._mtime = blank(), [], [], -1.0
                self.tz, _ = load_timezone("local")
            return
        if mtime == self._mtime and not force:
            return
        self._mtime = mtime
        self.errors = []
        try:
            raw = yaml.safe_load(self.path.read_text()) or {}
        except (OSError, yaml.YAMLError) as e:
            self.errors.append(f"config/{self.path.name} could not be read ({e}) — nothing is being kept quiet")
            log.error("maintenance windows: %s", self.errors[-1])
            self.data, self.windows = blank(), []
            self.tz, _ = load_timezone("local")
            return
        if not isinstance(raw, dict):
            self.errors.append(f"config/{self.path.name} should be a mapping with a `windows:` list — "
                               "nothing is being kept quiet")
            log.error("maintenance windows: %s", self.errors[-1])
            self.data, self.windows = blank(), []
            self.tz, _ = load_timezone("local")
            return
        data = blank()
        data.update({k: v for k, v in raw.items() if v is not None})
        self.data = data
        self.tz, problem = load_timezone(str(data.get("timezone") or "local"))
        if problem:
            self.errors.append(problem)
        self.windows = self._build(data.get("windows"))
        for win in self.windows:
            if win.problem:
                self.errors.append(win.problem)
        if self.errors:
            log.warning("maintenance windows: %s", "; ".join(self.errors))

    def _build(self, raw: Any) -> list[Window]:
        if raw is None:
            return []
        if not isinstance(raw, list):
            self.errors.append("`windows:` should be a list — every window in the file was skipped")
            return []
        out: list[Window] = []
        for entry in raw:
            if not isinstance(entry, dict):
                self.errors.append(f"skipped a window that is not a mapping: {entry!r}")
                continue
            try:
                out.append(build_window(entry, self.tz, [w.id for w in out]))
            except Exception as e:  # noqa: BLE001 - one bad window must not silence the rest of the file
                self.errors.append(f"skipped a window that could not be read ({e})")
        return out

    def save(self) -> None:
        """Write the file back, atomically, in the shape the docstring describes."""
        self.data["version"] = 1
        self.data["windows"] = [w.to_dict() for w in self.windows]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".maintenance-", suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            f.write("# When periscope stays quiet. Edited from the Alerts page; safe to hand-edit.\n")
            yaml.safe_dump(self.data, f, sort_keys=False, allow_unicode=True, default_flow_style=False)
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime
        self.errors = [w.problem for w in self.windows if w.problem]

    # ----- the question every send site asks ------------------------------------------------------------
    def quiet(self, service: str = "", *, server: str = "", key: str = "") -> bool:
        try:
            return bool(self._match(service, server, key))
        except Exception:  # noqa: BLE001 - a broken window config must never stop an alert
            log.exception("maintenance windows: quiet() failed — alerting anyway")
            return False

    def reason(self, service: str = "", *, server: str = "", key: str = "") -> str:
        """One sentence for the card and the log: why this alert is being held back."""
        try:
            hit = self._match(service, server, key)
            if hit is None:
                return ""
            if hit.id == "quiet-everything":
                return hit.reason or "everything is switched off until the quiet period ends"
            return hit.reason or f"the maintenance window {hit.id!r} is open ({hit.when()})"
        except Exception:  # noqa: BLE001
            log.exception("maintenance windows: reason() failed")
            return ""

    def active(self) -> list[dict[str, Any]]:
        """Every window that is open right now, newest reason first, as plain dicts for the UI and the log."""
        try:
            now = float(self.clock())
            self.reload()
            out = []
            for win in self._all_windows():
                if win.open_at(now, self.tz):
                    out.append({"id": win.id, "reason": win.reason, "when": win.when(), "scope": win.scope_words(),
                                "services": list(win.services), "servers": list(win.servers),
                                "keys": list(win.keys),
                                "one_off": win.one_off, "until": win.ends_at(now, self.tz)})
            return out
        except Exception:  # noqa: BLE001
            log.exception("maintenance windows: active() failed")
            return []

    def _match(self, service: str, server: str, key: str) -> Window | None:
        now = float(self.clock())
        self.reload()
        for win in self._all_windows():
            if win.open_at(now, self.tz) and win.covers(service, server=server, key=key):
                return win
        return None

    def _all_windows(self) -> list[Window]:
        """The configured windows, with the global switch in front of them when it is set."""
        big_red = self.quiet_everything_window()
        return ([big_red] if big_red else []) + list(self.windows)

    # ----- the global switch ---------------------------------------------------------------------------
    def quiet_everything_window(self) -> Window | None:
        until = parse_moment(self.data.get("quiet_until"), self.tz)
        if until is None:
            return None
        reason = str(self.data.get("quiet_reason") or "").strip() or "everything is switched off for now"
        win = Window(id="quiet-everything", reason=reason, one_off=True, start_ts=0.0, end_ts=until,
                     start="", end=str(self.data.get("quiet_until") or ""))
        return win

    def quiet_until(self) -> tuple[str, str, float | None]:
        """(the raw value in the file, the reason, when it ends as epoch seconds) — ('', '', None) when off."""
        raw = str(self.data.get("quiet_until") or "").strip()
        return raw, str(self.data.get("quiet_reason") or "").strip(), parse_moment(raw, self.tz)

    def set_quiet_until(self, until: str, reason: str = "") -> None:
        """Hold every alert until `until` (an empty value switches the big red button off)."""
        self.reload()
        text = str(until or "").strip()
        if text and parse_moment(text, self.tz) is None:
            raise ValueError("that quiet-until time could not be read — use something like 2026-09-05T02:00")
        self.data["quiet_until"] = text
        self.data["quiet_reason"] = str(reason or "").strip()
        self.save()

    # ----- the editor behind /alerts --------------------------------------------------------------------
    def rows(self) -> list[dict[str, Any]]:
        """Every configured window as a dict the page can render, open ones marked."""
        self.reload()
        now = float(self.clock())
        out = []
        for win in self.windows:
            out.append({"id": win.id, "reason": win.reason, "enabled": win.enabled, "days": list(win.days),
                        "start": win.start, "end": win.end, "servers": list(win.servers),
                        "services": list(win.services), "keys": list(win.keys), "one_off": win.one_off,
                        "when": win.when(), "scope": win.scope_words(), "problem": win.problem,
                        "open": win.open_at(now, self.tz)})
        return out

    def get(self, wid: str) -> Window | None:
        return next((w for w in self.windows if w.id == wid), None)

    def add(self, *, reason: str = "", days: Any = None, start: str = "", end: str = "", servers: Any = None,
            services: Any = None, keys: Any = None, enabled: bool = True, wid: str = "") -> Window:
        """Add a window. Raises ValueError with a sentence the page can show when it will not work."""
        self.reload()
        entry = {"id": wid or slugify(reason or "window", [w.id for w in self.windows]), "reason": reason,
                 "enabled": enabled, "days": days, "start": start, "end": end, "servers": servers,
                 "services": services, "keys": keys}
        win = build_window(entry, self.tz, [w.id for w in self.windows])
        if win.problem:
            raise ValueError(win.problem.split(": ", 1)[-1])
        if any(w.id == win.id for w in self.windows):
            win.id = slugify(win.id, [w.id for w in self.windows])
        self.windows.append(win)
        self.save()
        return win

    def remove(self, wid: str) -> bool:
        self.reload()
        before = len(self.windows)
        self.windows = [w for w in self.windows if w.id != wid]
        if len(self.windows) == before:
            return False
        self.save()
        return True

    def set_enabled(self, wid: str, on: bool) -> bool:
        self.reload()
        win = self.get(wid)
        if win is None:
            return False
        win.enabled = bool(on)
        self.save()
        return True


def default_path(config_dir: str | os.PathLike) -> Path:
    return Path(config_dir) / DEFAULT_NAME
