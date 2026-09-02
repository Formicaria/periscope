"""Consistent embed styling across all lab bots."""

from __future__ import annotations

import datetime as dt
from enum import Enum

import discord


class Severity(str, Enum):
    OK = "ok"
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"

    @property
    def color(self) -> int:
        return {
            Severity.OK: 0x2ECC71,
            Severity.INFO: 0x3498DB,
            Severity.WARNING: 0xF1C40F,
            Severity.CRITICAL: 0xE74C3C,
            Severity.UNKNOWN: 0x95A5A6,
        }[self]

    @property
    def emoji(self) -> str:
        return {
            Severity.OK: "🟢",
            Severity.INFO: "🔵",
            Severity.WARNING: "🟡",
            Severity.CRITICAL: "🔴",
            Severity.UNKNOWN: "⚪",
        }[self]


def status_dot(ok: bool | None) -> str:
    if ok is None:
        return "⚪"
    return "🟢" if ok else "🔴"


def progress_bar(pct: float, width: int = 12) -> str:
    pct = max(0.0, min(100.0, float(pct)))
    filled = round(width * pct / 100)
    return "█" * filled + "░" * (width - filled) + f" {pct:5.1f}%"


def human_bytes(n: float | int | None, suffix: str = "B") -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("", "K", "M", "G", "T", "P"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}{suffix}"
        n /= 1024
    return f"{n:.1f} E{suffix}"


def human_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


def truncate(text: str, limit: int = 1024, marker: str = "…") -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(marker)] + marker


def lab_embed(
    title: str,
    description: str | None = None,
    *,
    severity: Severity = Severity.INFO,
    lab_name: str | None = None,
    color: int | None = None,
    url: str | None = None,
    timestamp: bool = True,
) -> discord.Embed:
    """Build an embed with lab branding: severity color, lab-name footer, timestamp."""
    e = discord.Embed(
        title=f"{severity.emoji} {title}" if severity is not Severity.INFO else title,
        description=description,
        color=color if color is not None else severity.color,
        url=url,
    )
    if timestamp:
        e.timestamp = dt.datetime.now(dt.timezone.utc)
    if lab_name:
        e.set_footer(text=f"🧪 {lab_name}")
    return e
