"""Thread-safe progress counter.

Adapted from legalize-pipeline (MIT/Apache-2.0). See sync/NOTICES.md.
"""

import threading


class Counter:
    """Thread-safe counters for tracking cached/fetched/errors progress."""

    def __init__(self):
        self._lock = threading.Lock()
        self.cached = 0
        self.fetched = 0
        self.errors = 0
        self._extra: dict[str, int] = {}

    def inc(self, field: str) -> None:
        with self._lock:
            if hasattr(self, field) and field != "_extra":
                setattr(self, field, getattr(self, field) + 1)
            else:
                self._extra[field] = self._extra.get(field, 0) + 1

    def snapshot(self) -> tuple[int, int, int]:
        with self._lock:
            return self.cached, self.fetched, self.errors

    def snapshot_all(self) -> dict[str, int]:
        with self._lock:
            result = {"cached": self.cached, "fetched": self.fetched, "errors": self.errors}
            result.update(self._extra)
            return result
