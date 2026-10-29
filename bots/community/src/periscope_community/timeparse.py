"""The times and durations admins actually type into /psa maint.

    parse_duration("2h") / "90m" / "1h30m" / "1.5h" / "45"            → seconds (a bare number is minutes)
    parse_when("now") / "in 2h" / "22:00" / "10pm" / "10:30 pm"       → epoch seconds
               "tomorrow 9am" / "fri 10pm" / "friday 22:00"
               "2026-10-30 22:00" / "10/30 10pm" / "10/30/2026 22:00"

A clock time with no day means the next time that clock comes round (so "10pm" at 11pm is tomorrow).
Everything is read in the zone you pass. Anything unreadable raises ValueError with a sentence the admin sees.
"""

from __future__ import annotations

import datetime as dt
import re

DAYS = {"mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "weds": 2, "wednesday": 2,
        "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
        "sun": 6, "sunday": 6}
DUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(days|day|d|hours|hour|hrs|hr|h|minutes|minute|mins|min|m)?", re.I)
CLOCK_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)?$", re.I)
EXAMPLES = "try `now`, `in 2h`, `10pm`, `tomorrow 9am`, `fri 22:00` or `2026-10-30 22:00`"


def parse_duration(text: str) -> int:
    raw = (text or "").strip().lower().replace(" ", "")
    if not raw:
        raise ValueError("how long? try `2h`, `90m` or `1h30m`")
    total, pos = 0.0, 0
    for m in DUR_RE.finditer(raw):
        if m.start() != pos or not m.group(0):
            break
        num, unit = float(m.group(1)), (m.group(2) or "m").lower()
        total += num * (86400 if unit.startswith("d") else 3600 if unit.startswith("h") else 60)
        pos = m.end()
    if pos != len(raw) or total <= 0:
        raise ValueError(f"couldn't read {text!r} as a length of time — try `2h`, `90m` or `1h30m`")
    if total > 14 * 86400:
        raise ValueError("that's more than two weeks — split it into separate windows")
    return int(round(total))


def _clock(text: str) -> tuple[int, int] | None:
    m = CLOCK_RE.match(text.strip())
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm.startswith("p") else 0)
    elif m.group(2) is None:
        return None          # a bare "10" is ambiguous — make them say 10pm or 22:00
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return hour, minute


def parse_when(text: str, tz: dt.tzinfo, now: dt.datetime | None = None) -> float:
    now = (now or dt.datetime.now(tz)).astimezone(tz)
    raw = re.sub(r"\s+", " ", (text or "").strip().lower()).replace(" at ", " ")
    if raw in ("", "now", "right now", "asap"):
        return now.timestamp()
    m = re.match(r"^in (.+)$", raw)
    if m:
        return now.timestamp() + parse_duration(m.group(1))

    day: dt.date | None = None
    rest = raw
    m = re.match(r"^(today|tonight|tomorrow|tmrw)\s*(.*)$", raw)
    if m:
        day = now.date() + dt.timedelta(days=0 if m.group(1) in ("today", "tonight") else 1)
        rest = m.group(2)
        if not rest and m.group(1) == "tonight":
            raise ValueError("tonight at what time? e.g. `tonight 10pm`")
    else:
        m = re.match(r"^([a-z]+)\s+(.+)$", raw)
        if m and m.group(1) in DAYS:
            ahead = (DAYS[m.group(1)] - now.weekday()) % 7
            day, rest = now.date() + dt.timedelta(days=ahead), m.group(2)
        else:
            m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[t ](.+))?$", raw)
            m2 = re.match(r"^(\d{1,2})[-/](\d{1,2})(?:[-/](\d{2,4}))?(?:[t ](.+))?$", raw)
            try:
                if m:
                    day, rest = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.group(4) or ""
                elif m2:
                    year = int(m2.group(3)) if m2.group(3) else now.year
                    year += 2000 if year < 100 else 0
                    day, rest = dt.date(year, int(m2.group(1)), int(m2.group(2))), m2.group(4) or ""
                    if not m2.group(3) and day < now.date():
                        day = day.replace(year=day.year + 1)
            except ValueError:
                raise ValueError(f"{text!r} isn't a real date — {EXAMPLES}") from None

    clock = _clock(rest) if rest else None
    if rest and clock is None:
        raise ValueError(f"couldn't read the time in {text!r} — {EXAMPLES}")
    if clock is None:
        if day is None:
            raise ValueError(f"couldn't read {text!r} — {EXAMPLES}")
        raise ValueError(f"what time on {day:%a %b %d}? e.g. `{raw} 10pm`")
    hour, minute = clock
    if day is None:
        cand = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand <= now:
            cand += dt.timedelta(days=1)
        return cand.timestamp()
    cand = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    if cand.timestamp() < now.timestamp() - 60:
        if re.match(r"^[a-z]+ ", raw) and raw.split(" ")[0] in DAYS:   # "fri 10pm" said on friday at 11pm
            cand += dt.timedelta(days=7)
        else:
            raise ValueError(f"{cand:%a %b %d %I:%M %p} is already in the past")
    return cand.timestamp()


def human_span(seconds: float) -> str:
    """7260 → '2h 1m'."""
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = [f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m or not (d or h) else ""]
    return " ".join(p for p in parts if p)
