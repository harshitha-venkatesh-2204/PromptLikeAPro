"""Key-pool manager.

Rules implemented here (see requirement 3):
  - Prefer the calling system's assigned active key.
  - After a key's attempts are exhausted on retryable errors, put it in a
    cooldown (default 60s); it recovers automatically once the cooldown passes.
  - Failover order: preferred active -> other available actives -> spares.
  - Cooldowns/failover happen only for retryable failures. The caller must not
    cooldown a key for a 4xx invalid-request error.

Time is measured with ``time.monotonic`` (injectable for tests) so cooldowns are
immune to wall-clock changes. Secrets live only inside ``_Key.secret`` and are
never logged.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .config import LoadedKey


@dataclass
class _Key:
    label: str
    kind: str  # "active" | "spare"
    secret: str = field(repr=False)
    cooldown_until: float = 0.0
    consecutive_failures: int = 0

    def __repr__(self) -> str:
        return f"_Key(label={self.label!r}, kind={self.kind!r}, cooldown_until={self.cooldown_until:.3f})"


class KeyPool:
    def __init__(
        self,
        active: list[LoadedKey],
        spare: list[LoadedKey],
        cooldown_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._cooldown_seconds = cooldown_seconds
        self._lock = threading.Lock()
        self._active: list[_Key] = [_Key(k.label, k.kind, k.secret) for k in active]
        self._spare: list[_Key] = [_Key(k.label, k.kind, k.secret) for k in spare]
        self._by_label: dict[str, _Key] = {k.label: k for k in self._active + self._spare}

    # -- selection -----------------------------------------------------------
    def _available(self, key: _Key, now: float) -> bool:
        return key.cooldown_until <= now

    def candidate_sequence(self, preferred_label: str | None) -> list[_Key]:
        """Return currently-available keys in failover order.

        preferred active (if available) first, then the remaining available
        active keys, then available spares. Keys in cooldown are skipped.
        """
        with self._lock:
            now = self._clock()
            ordered: list[_Key] = []
            seen: set[str] = set()

            preferred = self._by_label.get(preferred_label) if preferred_label else None
            if preferred is not None and preferred.kind == "active" and self._available(preferred, now):
                ordered.append(preferred)
                seen.add(preferred.label)

            for key in self._active:
                if key.label not in seen and self._available(key, now):
                    ordered.append(key)
                    seen.add(key.label)

            for key in self._spare:
                if key.label not in seen and self._available(key, now):
                    ordered.append(key)
                    seen.add(key.label)

            return ordered

    # -- feedback ------------------------------------------------------------
    def record_success(self, label: str) -> None:
        with self._lock:
            key = self._by_label.get(label)
            if key is not None:
                key.consecutive_failures = 0
                key.cooldown_until = 0.0

    def put_cooldown(self, label: str, seconds: float | None = None) -> float:
        """Place a key in cooldown; returns the cooldown-until timestamp."""
        with self._lock:
            key = self._by_label.get(label)
            if key is None:
                return 0.0
            duration = self._cooldown_seconds if seconds is None else seconds
            key.consecutive_failures += 1
            key.cooldown_until = self._clock() + duration
            return key.cooldown_until

    # -- introspection (no secrets) -----------------------------------------
    def is_available(self, label: str) -> bool:
        with self._lock:
            key = self._by_label.get(label)
            return key is not None and self._available(key, self._clock())

    def status(self) -> dict[str, object]:
        with self._lock:
            now = self._clock()
            actives = [k for k in self._active if self._available(k, now)]
            spares = [k for k in self._spare if self._available(k, now)]
            return {
                "active_total": len(self._active),
                "active_available": len(actives),
                "spare_total": len(self._spare),
                "spare_available": len(spares),
                "in_cooldown": [
                    k.label for k in self._active + self._spare if not self._available(k, now)
                ],
            }

    def available_count(self) -> int:
        with self._lock:
            now = self._clock()
            return sum(1 for k in self._active + self._spare if self._available(k, now))
