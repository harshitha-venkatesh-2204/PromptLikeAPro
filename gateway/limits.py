"""Concurrency, queueing, and per-player protections.

``GlobalLimiter`` keeps the whole gateway within the Anthropic organization's
rate limits: the 6 active + 3 spare keys share one org limit, so adding keys
does not add throughput. It caps simultaneous in-flight upstream calls and
bounds how many requests may wait; excess requests are rejected with a clean
'busy' rather than piling up.

``PlayerLimiter`` enforces the per-player rules: player ID required, at most one
concurrent request per player, and a cooldown between a player's requests.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

from .errors import GatewayBusy


class GlobalLimiter:
    """A bounded concurrency gate with a bounded wait queue (org-wide)."""

    def __init__(self, max_concurrency: int, max_queue: int) -> None:
        self._max_concurrency = max(1, max_concurrency)
        self._max_queue = max(0, max_queue)
        self._sem = asyncio.Semaphore(self._max_concurrency)
        self._active = 0
        self._waiting = 0
        self._state_lock = asyncio.Lock()

    @property
    def in_flight(self) -> int:
        return self._active

    @property
    def queue_depth(self) -> int:
        return self._waiting

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        # Reject immediately if all slots are busy AND the wait queue is full.
        async with self._state_lock:
            if self._active >= self._max_concurrency and self._waiting >= self._max_queue:
                raise GatewayBusy(
                    "queue_full",
                    "The service is busy right now. Please try again shortly.",
                    retry_after=1.0,
                )
            self._waiting += 1
        try:
            await self._sem.acquire()
        finally:
            async with self._state_lock:
                self._waiting -= 1
        async with self._state_lock:
            self._active += 1
        try:
            yield
        finally:
            async with self._state_lock:
                self._active -= 1
            self._sem.release()


class PlayerLimiter:
    """Per-player concurrency (1) + inter-request cooldown."""

    def __init__(
        self,
        cooldown_seconds: float,
        max_concurrent: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cooldown = cooldown_seconds
        self._max_concurrent = max(1, max_concurrent)
        self._clock = clock
        self._in_flight: dict[str, int] = {}
        self._last_done: dict[str, float] = {}

    def try_begin(self, player_id: str) -> None:
        """Reserve a slot for the player or raise ``GatewayBusy``.

        Because per-player concurrency is 1, a second simultaneous request is
        rejected immediately (an effective queue length of 0) rather than piling
        up behind the first.
        """
        now = self._clock()
        active = self._in_flight.get(player_id, 0)
        if active >= self._max_concurrent:
            raise GatewayBusy(
                "player_concurrent",
                "You already have a request in progress. Please wait for it to finish.",
            )
        last = self._last_done.get(player_id)
        if last is not None:
            elapsed = now - last
            if elapsed < self._cooldown:
                raise GatewayBusy(
                    "player_cooldown",
                    "You're going a bit fast. Please try again shortly.",
                    retry_after=round(self._cooldown - elapsed, 2),
                )
        self._in_flight[player_id] = active + 1

    def end(self, player_id: str) -> None:
        active = self._in_flight.get(player_id, 0)
        if active <= 1:
            self._in_flight.pop(player_id, None)
        else:
            self._in_flight[player_id] = active - 1
        self._last_done[player_id] = self._clock()

    def prune(self, older_than_seconds: float = 3600.0) -> None:
        """Drop cooldown records for players idle longer than the threshold."""
        cutoff = self._clock() - older_than_seconds
        stale = [pid for pid, ts in self._last_done.items() if ts < cutoff and self._in_flight.get(pid, 0) == 0]
        for pid in stale:
            self._last_done.pop(pid, None)
