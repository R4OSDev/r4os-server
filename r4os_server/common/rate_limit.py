from __future__ import annotations

from collections import deque
import threading
import time
from typing import Callable


class PerIpRateLimiter:
    def __init__(self, limit_provider: Callable[[], int], clock: Callable[[], float] = time.monotonic):
        self._limit_provider = limit_provider
        self._clock = clock
        self._requests: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._last_cleanup = 0.0

    def allow(self, source_ip: str) -> bool:
        now = self._clock()
        cutoff = now - 1.0
        limit = self._limit_provider()
        with self._lock:
            if now - self._last_cleanup >= 10.0:
                self._requests = {
                    ip: timestamps
                    for ip, timestamps in self._requests.items()
                    if timestamps and timestamps[-1] > cutoff
                }
                self._last_cleanup = now
            timestamps = self._requests.setdefault(source_ip, deque())
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= limit:
                return False
            timestamps.append(now)
            return True
