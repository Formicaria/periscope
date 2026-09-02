"""Pure parsing/formatting helpers (no network, no Discord) so they are unit-testable."""

from __future__ import annotations

import re
from typing import Any

from periscope import Alert, Severity, truncate

RANGE_RE = re.compile(r"^(\d{1,5})([smhdw])$")
MAX_LABEL_FIELDS = 8
CORE_LABELS = ("instance", "job")
SKIP_LABELS = {"alertname", "severity", "__name__"}


def parse_range(text: str | None, default: str = "6h") -> str:
    """Validate a Grafana relative range like `15m`, `6h`, `2d`. Returns the normalised string."""
    raw = (text or default).strip().lower()
    m = RANGE_RE.match(raw)
    if not m:
        raise ValueError(f"invalid range {text!r}; use e.g. 30m, 6h, 2d, 1w")
    return f"{int(m.group(1))}{m.group(2)}"


def severity_from_labels(labels: dict[str, Any]) -> Severity:
    sev = str(labels.get("severity", "")).lower()
    if sev in ("critical", "crit", "emergency", "page"):
        return Severity.CRITICAL
    if sev in ("warning", "warn"):
        return Severity.WARNING
    return Severity.INFO


def alert_fields(labels: dict[str, Any], cap: int = MAX_LABEL_FIELDS) -> dict[str, str]:
    """instance + job first, then the remaining labels alphabetically, capped."""
    out: dict[str, str] = {}
    for k in CORE_LABELS:
        if labels.get(k):
            out[k] = str(labels[k])
    for k in sorted(labels):
        if len(out) >= cap:
            break
        if k in out or k in SKIP_LABELS or k.startswith("__"):
            continue
        out[k] = str(labels[k])
    return out


def alert_from_am(alert: dict[str, Any]) -> Alert:
    """Build a periscope Alert from one entry of an Alertmanager webhook / api/v2 alert."""
    labels = dict(alert.get("labels") or {})
    ann = dict(alert.get("annotations") or {})
    fp = str(alert.get("fingerprint") or "").strip()
    if not fp:
        fp = "|".join(f"{k}={labels[k]}" for k in sorted(labels))
    desc = ann.get("summary") or ""
    if ann.get("description") and ann["description"] != desc:
        desc = f"{desc}\n{ann['description']}" if desc else ann["description"]
    url = alert.get("generatorURL") or None
    return Alert(
        fingerprint=f"am:{fp}",
        title=str(labels.get("alertname") or "Alert"),
        description=truncate(desc, 2000),
        severity=severity_from_labels(labels),
        fields=alert_fields(labels),
        url=url if isinstance(url, str) and url.startswith(("http://", "https://")) else None,
    )


def format_metric(metric: dict[str, Any]) -> str:
    name = metric.get("__name__", "")
    labels = ",".join(f'{k}="{v}"' for k, v in sorted(metric.items()) if k != "__name__")
    if labels:
        return f"{name}{{{labels}}}"
    return name or "{}"


def format_value(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f != f or f in (float("inf"), float("-inf")):
        return str(v)
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    return f"{f:.6g}"


def format_instant_result(data: dict[str, Any], max_rows: int = 20, row_width: int = 96) -> tuple[str, int]:
    """Render an api/v1/query `data` payload as aligned text rows. Returns (text, total_rows)."""
    rtype = data.get("resultType")
    result = data.get("result")
    if rtype in ("scalar", "string"):
        return format_value(result[1]) if isinstance(result, list) and len(result) == 2 else str(result), 1
    rows: list[tuple[str, str]] = []
    for item in result or []:
        metric = format_metric(item.get("metric") or {})
        if "value" in item:
            val = format_value(item["value"][1])
        elif "values" in item and item["values"]:
            val = format_value(item["values"][-1][1])
        else:
            val = "—"
        rows.append((metric, val))
    total = len(rows)
    if not rows:
        return "(empty result)", 0
    rows = rows[:max_rows]
    vw = max(len(v) for _, v in rows)
    lines = []
    for metric, val in rows:
        lines.append(f"{val.rjust(vw)}  {truncate(metric, row_width)}")
    if total > max_rows:
        lines.append(f"… {total - max_rows} more row(s)")
    return "\n".join(lines), total


def group_targets(targets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group Prometheus activeTargets by job -> {'up': n, 'down': n, 'unknown': n, 'down_list': [(instance, err)]}."""
    out: dict[str, dict[str, Any]] = {}
    for t in targets:
        labels = t.get("labels") or {}
        job = str(labels.get("job") or t.get("scrapePool") or "unknown")
        g = out.setdefault(job, {"up": 0, "down": 0, "unknown": 0, "down_list": []})
        health = str(t.get("health") or "unknown").lower()
        if health == "up":
            g["up"] += 1
        elif health == "down":
            g["down"] += 1
            g["down_list"].append((str(labels.get("instance") or t.get("scrapeUrl") or "?"),
                                   str(t.get("lastError") or "")))
        else:
            g["unknown"] += 1
    return dict(sorted(out.items()))


def target_fingerprint(job: str, instance: str) -> str:
    return f"prom:target:{job}:{instance}:down"


def count_by_severity(alerts: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"critical": 0, "warning": 0, "info": 0}
    for a in alerts:
        sev = severity_from_labels(a.get("labels") or {})
        counts[sev.value if sev.value in counts else "info"] += 1
    return counts


def silence_summary(s: dict[str, Any]) -> str:
    matchers = ", ".join(
        f"{m.get('name')}{'=~' if m.get('isRegex') else '='}\"{m.get('value')}\""
        for m in s.get("matchers") or []
    )
    ends = str(s.get("endsAt") or "")[:16].replace("T", " ")
    who = s.get("createdBy") or "?"
    comment = s.get("comment") or ""
    return f"`{s.get('id', '?')}`\n{matchers}\nuntil {ends} UTC · by {who}" + (f" · {comment}" if comment else "")
