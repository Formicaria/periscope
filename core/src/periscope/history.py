"""The event log: what happened, when, and the numbers behind it — so periscope remembers.

Every bot has `bot.history` (see `periscope.hooks`). At a send site it costs one line:

    bot.history.record(service="docker", kind="down", key="sonarr", title="sonarr exited")
    bot.history.sample(service="pve", metric="cpu", value=91.2, key="pve1")

and the runtime reads it back for the Trends page and the "while you were asleep" recap:

    history.events(since=t, service="arr", kind=("grab", "import"), limit=200)
    history.series(service="pve", metric="cpu", key="pve1", since=t, bucket=3600)
    history.counts(since=t, kind=ALERT_KINDS, by="service")
    history.uptime(service="docker", key="sonarr", since=t)

Two tables in `data/history.db`: `events` (one row per thing that happened) and `samples` (one row per
number). Writes go on a queue and a background thread commits them in batches, so a send site never waits on
the disk and a failing write can never take a bot down — the worst that happens is a dropped row and a line in
the log. The file is opened in WAL mode with a busy timeout, so a second process (the web UI, a second
periscope) can read and write the same file without either one corrupting it.

Kinds are just strings; the ones the shipped bots write are named below so the recap and the Trends page can
agree on what they mean. `up` and `down` are the pair `uptime()` counts: everything else is a plain event.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

log = logging.getLogger(__name__)

# ----- the vocabulary ---------------------------------------------------------------------------------
UP_KINDS = ("up", "online", "resolved")          # a thing came back (or never left)
DOWN_KINDS = ("down", "offline", "outage")       # a thing went away
ALERT_KINDS = ("alert", "down", "offline", "outage")   # what "how many alerts" counts
RESOLVE_KINDS = ("resolved", "up", "online")           # what closes one of the above
SEVERITIES = ("ok", "info", "warning", "critical")

MAX_PAYLOAD = 16_384          # a payload bigger than this is dropped rather than bloat the file
QUEUE_MAX = 20_000            # pending writes held in memory before the oldest are dropped
BATCH = 500                   # rows committed in one transaction
FLUSH_S = 0.5                 # how long the writer waits for more rows before committing what it has
PRUNE_EVERY_S = 3600          # retention runs hourly
BUSY_TIMEOUT_MS = 5000        # how long a write waits for another process to finish its transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL    NOT NULL,
    server   TEXT    NOT NULL DEFAULT '',
    service  TEXT    NOT NULL DEFAULT '',
    kind     TEXT    NOT NULL DEFAULT '',
    key      TEXT    NOT NULL DEFAULT '',
    severity TEXT    NOT NULL DEFAULT 'info',
    title    TEXT    NOT NULL DEFAULT '',
    detail   TEXT    NOT NULL DEFAULT '',
    value    REAL,
    payload  TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_ts        ON events(ts);
CREATE INDEX IF NOT EXISTS events_service   ON events(service, ts);
CREATE INDEX IF NOT EXISTS events_kind      ON events(kind, ts);
CREATE INDEX IF NOT EXISTS events_key       ON events(service, key, ts);
CREATE INDEX IF NOT EXISTS events_severity  ON events(severity, ts);

CREATE TABLE IF NOT EXISTS samples (
    ts      REAL NOT NULL,
    server  TEXT NOT NULL DEFAULT '',
    service TEXT NOT NULL DEFAULT '',
    metric  TEXT NOT NULL DEFAULT '',
    key     TEXT NOT NULL DEFAULT '',
    value   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_lookup ON samples(service, metric, key, ts);
CREATE INDEX IF NOT EXISTS samples_ts     ON samples(ts);
"""

EVENT_COLUMNS = ("id", "ts", "server", "service", "kind", "key", "severity", "title", "detail", "value", "payload")
GROUPABLE = {"service", "kind", "key", "server", "severity", "metric"}


# ----- small helpers ----------------------------------------------------------------------------------
def _texts(value: Any) -> list[str]:
    """One name or several, as a list of non-empty strings. `kind="grab"` and `kind=("grab", "import")`."""
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        text = value.decode() if isinstance(value, bytes) else value
        return [text] if text else []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(v) for v in value if str(v)]
    return [str(value)]


def _clause(column: str, value: Any, where: list[str], args: list[Any]) -> None:
    """Add `column = ?` or `column IN (?, ?)` when the filter names anything at all."""
    names = _texts(value)
    if not names:
        return
    if len(names) == 1:
        where.append(f"{column} = ?")
        args.append(names[0])
    else:
        where.append(f"{column} IN ({', '.join('?' * len(names))})")
        args.extend(names)


def _number(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _payload_json(payload: Any) -> str:
    if not payload:
        return ""
    try:
        text = json.dumps(payload, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""
    return text if len(text) <= MAX_PAYLOAD else ""


class _Flush:
    """A marker on the write queue: the writer sets the event once everything before it is committed."""

    __slots__ = ("done",)

    def __init__(self) -> None:
        self.done = threading.Event()


class History:
    """The event log, backed by SQLite. Writes are queued; reads are answered from a fresh connection."""

    enabled = True

    def __init__(self, path: str | os.PathLike, *, retention_days: int = 90, batch: int = BATCH,
                 flush_s: float = FLUSH_S, queue_max: int = QUEUE_MAX) -> None:
        self.path = Path(path)
        self.retention_days = max(0, int(retention_days or 0))
        self._batch = max(1, int(batch))
        self._flush_s = max(0.01, float(flush_s))
        self._queue: queue.Queue = queue.Queue(maxsize=max(100, int(queue_max)))
        self._dropped = 0
        self._closed = False
        self._lock = threading.Lock()
        self._last_prune = 0.0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()              # fails loudly here, where `hooks.history_for` can fall back
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()
        self._writer = threading.Thread(target=self._run, name="periscope-history", daemon=True)
        self._writer.start()

    # ----- connections ---------------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        """A connection for the calling thread. WAL + a busy timeout is what makes two processes safe."""
        conn = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    # ----- writing -------------------------------------------------------------------------------
    def record(self, *, service: str, kind: str, key: str = "", severity: str = "info", title: str = "",
               detail: str = "", server: str = "", value: float | None = None,
               payload: dict[str, Any] | None = None, at: float | None = None) -> None:
        """Remember that something happened. Never raises, never waits on the disk.

        `at` backdates the row; send sites leave it alone, backfills and tests use it."""
        try:
            row = (float(at) if at is not None else time.time(),
                   str(server or ""), str(service or ""), str(kind or ""), str(key or ""),
                   str(severity or "info"), str(title or ""), str(detail or ""), _number(value),
                   _payload_json(payload))
            self._offer(("events", row))
        except Exception:                                  # noqa: BLE001 - a bot must never fall over on this
            log.debug("history.record failed", exc_info=True)

    def sample(self, *, service: str, metric: str, value: float, key: str = "", server: str = "",
               at: float | None = None) -> None:
        """Remember a number: one point on a chart. Never raises, never waits on the disk."""
        try:
            number = _number(value)
            if number is None:
                return
            self._offer(("samples", (float(at) if at is not None else time.time(), str(server or ""),
                                     str(service or ""), str(metric or ""), str(key or ""), number)))
        except Exception:                                  # noqa: BLE001
            log.debug("history.sample failed", exc_info=True)

    def _offer(self, item: tuple[str, tuple]) -> None:
        """Queue a row, dropping the oldest rather than blocking when the writer has fallen behind."""
        if self._closed:
            return
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self._dropped += 1
            if self._dropped in (1, 100, 1000) or self._dropped % 10_000 == 0:
                log.warning("event log is behind — dropped %d rows so far", self._dropped)
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(item)
            except (queue.Empty, queue.Full):
                pass

    @property
    def dropped(self) -> int:
        """How many rows were thrown away because the writer could not keep up."""
        return self._dropped

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until everything queued so far is on disk. Queries call this, so a read never misses a write."""
        if self._closed or not self._writer.is_alive():
            return False
        marker = _Flush()
        try:
            self._queue.put(marker, timeout=timeout)
        except queue.Full:                                 # pragma: no cover - only when the writer is wedged
            return False
        return marker.done.wait(timeout)

    def _run(self) -> None:
        """The writer thread: drain the queue, commit in batches, prune on the hour."""
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            while True:
                try:
                    item = self._queue.get(timeout=60)
                except queue.Empty:
                    self._prune_if_due(conn)      # an idle log still ages out of retention
                    continue
                if item is None:
                    break
                events, samples, markers, stop = self._drain(item)
                self._commit(conn, events, samples)
                for marker in markers:
                    marker.done.set()
                self._prune_if_due(conn)
                if stop:
                    break
        except Exception:                                  # noqa: BLE001 - the log dies, the bots keep running
            log.exception("event log writer stopped — history will not be recorded")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:                          # noqa: BLE001
                    pass

    def _drain(self, first: Any) -> tuple[list[tuple], list[tuple], list[_Flush], bool]:
        """Take `first` plus whatever else is waiting (up to a batch), split by table."""
        events: list[tuple] = []
        samples: list[tuple] = []
        markers: list[_Flush] = []
        item = first
        deadline = time.monotonic() + self._flush_s
        while True:
            if item is None:
                return events, samples, markers, True
            if isinstance(item, _Flush):
                markers.append(item)
            elif item[0] == "events":
                events.append(item[1])
            else:
                samples.append(item[1])
            if len(events) + len(samples) >= self._batch or markers:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = self._queue.get(timeout=remaining)
            except queue.Empty:
                break
        return events, samples, markers, False

    def _commit(self, conn: sqlite3.Connection, events: list[tuple], samples: list[tuple]) -> None:
        if not events and not samples:
            return
        try:
            with conn:
                if events:
                    conn.executemany("INSERT INTO events (ts, server, service, kind, key, severity, title, detail,"
                                     " value, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", events)
                if samples:
                    conn.executemany("INSERT INTO samples (ts, server, service, metric, key, value)"
                                     " VALUES (?, ?, ?, ?, ?, ?)", samples)
        except sqlite3.Error as e:
            log.warning("event log write failed (%d events, %d samples): %s", len(events), len(samples), e)

    def _prune_if_due(self, conn: sqlite3.Connection) -> None:
        now = time.monotonic()
        if self._last_prune and now - self._last_prune < PRUNE_EVERY_S:
            return
        self._last_prune = now
        try:
            self._prune_on(conn)
        except sqlite3.Error as e:
            log.warning("event log retention pass failed: %s", e)

    def _prune_on(self, conn: sqlite3.Connection) -> int:
        if not self.retention_days:
            return 0
        cutoff = time.time() - self.retention_days * 86400
        with conn:
            gone = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
            gone += conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,)).rowcount
        return max(0, gone)

    def prune(self) -> int:
        """Throw away everything older than `retention_days`; returns how many rows went. Runs hourly anyway."""
        conn = None
        try:
            self.flush()
            conn = self._connect()
            return self._prune_on(conn)
        except Exception as e:                             # noqa: BLE001
            log.warning("event log retention pass failed: %s", e)
            return 0
        finally:
            if conn is not None:
                conn.close()

    def close(self) -> None:
        """Stop the writer, after everything queued is on disk. Safe to call twice."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._queue.put(None, timeout=2)
            self._writer.join(timeout=5)
        except Exception:                                  # noqa: BLE001
            log.debug("event log did not shut down cleanly", exc_info=True)

    # ----- reading -------------------------------------------------------------------------------
    def events(self, *, since: float | None = None, until: float | None = None, service: Any = None,
               kind: Any = None, severity: Any = None, key: Any = None, server: Any = None,
               search: str = "", limit: int = 200, offset: int = 0, newest_first: bool = True,
               **_ignored: Any) -> list[dict[str, Any]]:
        """The events that match, newest first by default. `payload` comes back as a dict."""
        where, args = self._window(since, until)
        _clause("service", service, where, args)
        _clause("kind", kind, where, args)
        _clause("severity", severity, where, args)
        _clause("key", key, where, args)
        _clause("server", server, where, args)
        if search:
            where.append("(title LIKE ? OR detail LIKE ? OR key LIKE ?)")
            args.extend([f"%{search}%"] * 3)
        order = "DESC" if newest_first else "ASC"
        sql = (f"SELECT {', '.join(EVENT_COLUMNS)} FROM events WHERE {' AND '.join(where)}"
               f" ORDER BY ts {order}, id {order} LIMIT ? OFFSET ?")
        args.extend([max(1, int(limit)), max(0, int(offset))])
        return [self._event(row) for row in self._query(sql, args)]

    def series(self, *, service: str = "", metric: str = "", key: Any = None, server: Any = None,
               since: float | None = None, until: float | None = None, bucket: float = 3600,
               agg: str = "avg", **_ignored: Any) -> list[tuple[float, float]]:
        """A metric over time as (bucket start, value), oldest first — what a sparkline is drawn from."""
        bucket = max(1.0, float(bucket or 3600))
        how = {"avg": "AVG", "max": "MAX", "min": "MIN", "sum": "SUM"}.get(str(agg).lower(), "AVG")
        where, args = self._window(since, until)
        _clause("service", service, where, args)
        _clause("metric", metric, where, args)
        _clause("key", key, where, args)
        _clause("server", server, where, args)
        sql = (f"SELECT CAST(ts / ? AS INTEGER) * ? AS bucket, {how}(value) AS value FROM samples"
               f" WHERE {' AND '.join(where)} GROUP BY bucket ORDER BY bucket ASC")
        rows = self._query(sql, [bucket, bucket, *args])
        return [(float(r["bucket"]), float(r["value"])) for r in rows if r["value"] is not None]

    def counts(self, *, since: float | None = None, until: float | None = None, service: Any = None,
               kind: Any = None, severity: Any = None, key: Any = None, server: Any = None,
               metric: Any = None, by: str = "service", of: str = "events",
               **_ignored: Any) -> dict[str, int]:
        """How many, grouped by one column — `by` is service · kind · key · server · severity · metric.

        `of="samples"` tallies the numbers table instead, which is how the Trends page finds out which
        metrics a service has actually written."""
        table = "samples" if str(of).lower().startswith("sample") else "events"
        column = by if by in GROUPABLE else "service"
        if table == "samples" and column in ("kind", "severity"):
            column = "metric"
        if table == "events" and column == "metric":
            column = "kind"
        where, args = self._window(since, until)
        _clause("service", service, where, args)
        _clause("key", key, where, args)
        _clause("server", server, where, args)
        if table == "events":
            _clause("kind", kind, where, args)
            _clause("severity", severity, where, args)
        else:
            _clause("metric", metric, where, args)
        sql = (f"SELECT {column} AS name, COUNT(*) AS n FROM {table} WHERE {' AND '.join(where)}"
               f" GROUP BY {column} ORDER BY n DESC, name ASC")
        return {str(row["name"]): int(row["n"]) for row in self._query(sql, args)}

    def uptime(self, *, service: str = "", key: str = "", server: Any = None, since: float | None = None,
               until: float | None = None, **_ignored: Any) -> float | None:
        """The share of the window a thing was up, as a percentage — None when nothing is known about it.

        Read off the `up` / `down` events: whatever state the last event before the window left it in holds
        until the next one, and the tail runs to the end of the window. When the window has events but nothing
        precedes it, the state before the first one is taken to be its opposite."""
        until = float(until if until is not None else time.time())
        since = float(since if since is not None else until - 86400)
        span = until - since
        if span <= 0:
            return None
        marks = (*UP_KINDS, *DOWN_KINDS)
        where, args = ["ts < ?"], [until]
        _clause("service", service, where, args)
        _clause("key", key, where, args)
        _clause("server", server, where, args)
        _clause("kind", marks, where, args)
        rows = self._query(f"SELECT ts, kind FROM events WHERE {' AND '.join(where)} ORDER BY ts ASC, id ASC",
                           args)
        before = [r for r in rows if r["ts"] <= since]
        inside = [r for r in rows if r["ts"] > since]
        if not before and not inside:
            return None
        if before:
            state = before[-1]["kind"] in UP_KINDS
        else:
            state = inside[0]["kind"] not in UP_KINDS      # it must have been the other way round beforehand
        up, cursor = 0.0, since
        for row in inside:
            at = min(float(row["ts"]), until)
            if state:
                up += at - cursor
            cursor, state = at, row["kind"] in UP_KINDS
        if state:
            up += until - cursor
        return round(max(0.0, min(span, up)) / span * 100, 3)

    # ----- query plumbing ------------------------------------------------------------------------
    @staticmethod
    def _window(since: float | None, until: float | None) -> tuple[list[str], list[Any]]:
        where, args = ["1 = 1"], []
        if since is not None:
            where.append("ts >= ?")
            args.append(float(since))
        if until is not None:
            where.append("ts < ?")
            args.append(float(until))
        return where, args

    def _query(self, sql: str, args: Sequence[Any]) -> list[sqlite3.Row]:
        """Read from a fresh connection, after the queue has drained. Never raises: a broken log reads empty."""
        self.flush()
        try:
            conn = self._connect()
        except sqlite3.Error as e:
            log.warning("event log unreadable: %s", e)
            return []
        try:
            return list(conn.execute(sql, list(args)))
        except sqlite3.Error as e:
            log.warning("event log query failed: %s", e)
            return []
        finally:
            conn.close()

    @staticmethod
    def _event(row: sqlite3.Row) -> dict[str, Any]:
        out = {name: row[name] for name in EVENT_COLUMNS}
        raw = out.pop("payload") or ""
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {}
        out["payload"] = payload if isinstance(payload, dict) else {"value": payload}
        return out

    def __enter__(self) -> "History":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<History {self.path} keep={self.retention_days}d>"


def window(days: float = 1.0, *, now: float | None = None) -> tuple[float, float]:
    """The last `days` days as (since, until) — the shape every query here takes."""
    end = float(now if now is not None else time.time())
    return end - days * 86400, end


def summarise(events: Iterable[dict[str, Any]], by: str = "service") -> dict[str, int]:
    """Tally events already in hand, the same way `counts()` tallies them in SQL."""
    out: dict[str, int] = {}
    for event in events:
        name = str(event.get(by) or "")
        out[name] = out.get(name, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))
