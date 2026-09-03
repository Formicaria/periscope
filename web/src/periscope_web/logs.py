"""Ring buffer of recent log lines + fan-out queues for the live tail (SSE)."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


@dataclass(frozen=True)
class LogLine:
    seq: int
    ts: float
    level: str
    name: str
    text: str  # fully formatted line

    def matches(self, q: str = "", min_level: str = "") -> bool:
        if min_level and min_level in LEVELS and self.level in LEVELS and LEVELS.index(self.level) < LEVELS.index(min_level):
            return False
        return not q or q.lower() in self.text.lower()


class LogBuffer(logging.Handler):
    """logging.Handler keeping the last `maxlen` lines and pushing new ones to every subscriber queue.
    `emit` may be called from any thread; queue hand-off always happens on the loop it was bound to."""

    def __init__(self, maxlen: int = 2000):
        super().__init__()
        self.setFormatter(logging.Formatter(FORMAT))
        self.lines: deque[LogLine] = deque(maxlen=maxlen)
        self.seq = 0
        self.loop: asyncio.AbstractEventLoop | None = None
        self._subs: set[asyncio.Queue[LogLine]] = set()
        self._lock = threading.Lock()

    # ----- logging.Handler ----------------------------------------------------------------------
    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:  # noqa: BLE001
            text = f"{record.levelname} {record.name}: {record.getMessage()}"
        with self._lock:
            self.seq += 1
            line = LogLine(self.seq, record.created, record.levelname, record.name, text)
            self.lines.append(line)
            subs = list(self._subs)
        if not subs:
            return
        loop = self.loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        for q in subs:
            if loop is None or running is loop:
                self._push(q, line)
            else:
                try:
                    loop.call_soon_threadsafe(self._push, q, line)
                except RuntimeError:
                    pass  # loop closed

    @staticmethod
    def _push(q: asyncio.Queue, line: LogLine) -> None:
        try:
            q.put_nowait(line)
        except asyncio.QueueFull:
            pass  # a slow consumer loses lines rather than blocking the process

    # ----- readers --------------------------------------------------------------------------------
    def since(self, seq: int) -> list[LogLine]:
        with self._lock:
            return [line for line in self.lines if line.seq > seq]

    def snapshot(self) -> list[LogLine]:
        with self._lock:
            return list(self.lines)

    def last_seq(self) -> int:
        with self._lock:
            return self.seq

    def subscribe(self) -> asyncio.Queue[LogLine]:
        q: asyncio.Queue[LogLine] = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    async def stream(self, *, since: int = 0, q: str = "", min_level: str = "", limit: int | None = None,
                     keepalive_s: float = 15.0) -> AsyncIterator[LogLine | None]:
        """Yield buffered lines newer than `since`, then live ones. `None` marks a keep-alive tick.
        `limit` (tests, curl) ends the stream after that many lines."""
        sent = 0
        queue = self.subscribe()
        try:
            for line in self.since(since):
                if line.matches(q, min_level):
                    yield line
                    sent += 1
                    if limit is not None and sent >= limit:
                        return
            last = self.last_seq()
            while limit is None or sent < limit:
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=keepalive_s)
                except asyncio.TimeoutError:
                    yield None
                    continue
                if line.seq <= last:
                    continue  # already replayed from the buffer
                if line.matches(q, min_level):
                    yield line
                    sent += 1
        finally:
            self.unsubscribe(queue)


def sse(lines: Iterable[LogLine | None]) -> Iterable[str]:
    """Format lines as text/event-stream frames (keep-alives become comments)."""
    for line in lines:
        if line is None:
            yield ": keep-alive\n\n"
            continue
        data = "\n".join(f"data: {part}" for part in line.text.splitlines() or [""])
        yield f"id: {line.seq}\nevent: log\n{data}\n\n"
