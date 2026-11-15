"""Per-client rate limiting for a public JSON API.

Each client may make bursts of up to CAPACITY requests and RATE requests a
second on average. The app runs behind one reverse proxy that appends the
connecting address to X-Forwarded-For.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock

CAPACITY = 10
RATE = 5.0            # tokens added per second


@dataclass
class Bucket:
    tokens: float = CAPACITY
    last: float = field(default_factory=time.monotonic)


class Limiter:
    def __init__(self, capacity: int = CAPACITY, rate: float = RATE, clock=time.monotonic) -> None:
        self.capacity = capacity
        self.rate = rate
        self.clock = clock
        self._buckets: dict[str, Bucket] = {}
        self._lock = Lock()

    def allow(self, client: str) -> bool:
        """Take one token for `client`; False means answer 429."""
        with self._lock:
            now = self.clock()
            b = self._buckets.get(client)
            if b is None:
                b = self._buckets[client] = Bucket(self.capacity, now)
            elapsed = now - b.last
            b.tokens = min(self.capacity, b.tokens + int(elapsed * self.rate))
            b.last = now
            if b.tokens >= 1:
                b.tokens -= 1
                return True
            return False

    def retry_after(self, client: str) -> float:
        """Seconds until the client has a token again, for the Retry-After header."""
        with self._lock:
            b = self._buckets.get(client)
            if b is None or b.tokens >= 1:
                return 0.0
            return (1 - b.tokens) / self.rate


def client_id(headers: dict[str, str], peer: str) -> str:
    """Who is asking: the original client address, not the proxy's."""
    xff = headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return peer


def handle(limiter: Limiter, headers: dict[str, str], peer: str) -> tuple[int, dict[str, str]]:
    who = client_id(headers, peer)
    if limiter.allow(who):
        return 200, {}
    return 429, {"Retry-After": str(max(1, round(limiter.retry_after(who))))}
