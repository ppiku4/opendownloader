"""Token-bucket rate limiter shared across threads (debt-based)."""
from __future__ import annotations

import threading
import time


class RateLimiter:
    """Each caller reserves `n` bytes; if the bucket goes negative the caller
    sleeps until the debt would be repaid. rate 0 means unlimited."""

    def __init__(self, rate_bps: float = 0):
        self._lock = threading.Lock()
        self._rate = float(rate_bps)
        self._tokens = 0.0
        self._last = time.monotonic()

    @property
    def rate(self) -> float:
        return self._rate

    def set_rate(self, rate_bps: float) -> None:
        with self._lock:
            self._rate = float(max(0, rate_bps))
            self._tokens = min(self._tokens, self._rate)
            self._last = time.monotonic()

    def consume(self, n: int, stop_event: threading.Event | None = None) -> None:
        with self._lock:
            rate = self._rate
            if rate <= 0:
                return
            now = time.monotonic()
            self._tokens = min(rate, self._tokens + (now - self._last) * rate)
            self._last = now
            self._tokens -= n
            wait = -self._tokens / rate if self._tokens < 0 else 0.0
        if wait > 0:
            if stop_event is not None:
                stop_event.wait(wait)
            else:
                time.sleep(wait)


class MultiLimiter:
    """Applies several limiters (e.g. global + per-download) in sequence."""

    def __init__(self, *limiters: RateLimiter | None):
        self.limiters = [lim for lim in limiters if lim is not None]

    def consume(self, n: int, stop_event: threading.Event | None = None) -> None:
        for lim in self.limiters:
            lim.consume(n, stop_event)
