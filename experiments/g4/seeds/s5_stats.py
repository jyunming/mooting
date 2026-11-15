import threading


class Stats:
    """Counters shared by every worker thread. Safe to call from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def incr(self, name: str, by: int = 1) -> None:
        current = self._counts.get(name, 0)
        with self._lock:
            self._counts[name] = current + by

    def get(self, name: str) -> int:
        with self._lock:
            return self._counts.get(name, 0)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)
