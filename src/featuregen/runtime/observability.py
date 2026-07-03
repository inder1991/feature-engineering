"""Dependency-free structured logging + in-process metrics for the durable runtime.

The platform is banking-grade but the runtime was effectively blind (review MAJOR #11): no
structured logs, no counters, no health snapshot. This module is deliberately tiny and has NO
external dependency (no prometheus / otel) — it emits one JSON object per event to stderr and keeps
process-local counters/gauges that a health endpoint (or a test) can snapshot. A real metrics
exporter can later read `counters.snapshot()`; nothing here forces that choice now.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any


def log(event: str, *, level: str = "info", **fields: Any) -> None:
    """Emit ONE structured event as a single JSON line to stderr.

    `event` is a stable, dotted event name (e.g. `worker.tick`, `control.auto_park.parked`) and
    `**fields` are arbitrary structured context. Non-JSON-native values are stringified (`default=str`)
    so logging can never itself raise. Flushed per line so a crash does not lose the last events."""
    record: dict[str, Any] = {"ts": time.time(), "level": level, "event": event}
    record.update(fields)
    try:
        line = json.dumps(record, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        line = json.dumps({"ts": time.time(), "level": "error", "event": "log.serialize_failed",
                           "original_event": event})
    print(line, file=sys.stderr, flush=True)


class Counters:
    """Thread-safe in-process counters + gauges. Counters are monotonic tallies (`incr`); gauges are
    last-write point-in-time values (`gauge`, e.g. queue depth / projection lag). `snapshot()` returns
    a plain-dict copy for a health endpoint or a test assertion; `reset()` is for test isolation."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        self._lock = threading.Lock()

    def incr(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + amount

    def gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {"counters": dict(self._counts), "gauges": dict(self._gauges)}

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()
            self._gauges.clear()


# Process-wide singleton — the runtime worker and its stages share this instance.
counters = Counters()
