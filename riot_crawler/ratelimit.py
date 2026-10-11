"""Sliding-window rate limiter that mirrors Riot's multi-window limits.

Riot advertises limits as headers like ``X-App-Rate-Limit: 20:1,100:120``
(20 requests per 1 s AND 100 per 120 s). We keep one timestamp history per
limiter and wait until every window has room before letting a request go.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Awaitable, Callable

# Riot's development-key limits; replaced by whatever the server reports.
DEV_KEY_APP_LIMITS = [(20, 1.0), (100, 120.0)]

# Extra slack on each window to absorb clock skew / network latency.
WINDOW_PADDING_S = 0.15


def parse_limit_header(value: str | None) -> list[tuple[int, float]] | None:
    """Parse ``"20:1,100:120"`` into ``[(20, 1.0), (100, 120.0)]``."""
    if not value:
        return None
    limits = []
    for part in value.split(","):
        count, _, window = part.strip().partition(":")
        if not count or not window:
            continue
        limits.append((int(count), float(window)))
    return sorted(limits, key=lambda x: x[1]) or None


class SlidingWindowLimiter:
    def __init__(
        self,
        limits: list[tuple[int, float]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._limits = sorted(limits or [], key=lambda x: x[1])
        self._history: deque[float] = deque()
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()
        self._clock = clock
        self._sleep = sleep

    @property
    def limits(self) -> list[tuple[int, float]]:
        return list(self._limits)

    def set_limits(self, limits: list[tuple[int, float]] | None) -> None:
        if limits and limits != self._limits:
            self._limits = limits

    def block_for(self, seconds: float) -> None:
        """Pause all requests through this limiter (used on HTTP 429)."""
        self._blocked_until = max(self._blocked_until, self._clock() + seconds)

    def _wait_time(self, now: float) -> float:
        wait = self._blocked_until - now
        if not self._limits:
            return wait
        longest = self._limits[-1][1] + WINDOW_PADDING_S
        while self._history and self._history[0] <= now - longest:
            self._history.popleft()
        n = len(self._history)
        for count, window in self._limits:
            if n >= count:
                # The request `count` positions back must leave the window.
                oldest_relevant = self._history[n - count]
                wait = max(wait, oldest_relevant + window + WINDOW_PADDING_S - now)
        return wait

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                wait = self._wait_time(now)
                if wait <= 0:
                    self._history.append(now)
                    return
                await self._sleep(wait)
