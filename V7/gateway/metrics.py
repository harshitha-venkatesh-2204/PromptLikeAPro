"""Lightweight in-process metrics with a Prometheus text exposition endpoint.

No external client library — the counters are a plain thread-safe dict and the
renderer emits standard Prometheus exposition format, suitable for scraping.
"""
from __future__ import annotations

import threading
from typing import Callable

_COUNTERS = (
    "requests_total",
    "success_total",
    "failure_client_error_total",   # 4xx from upstream (invalid request)
    "failure_upstream_total",       # retryable upstream exhausted
    "failure_no_keys_total",
    "retries_total",
    "cooldowns_total",
    "failovers_total",
    "player_busy_total",
    "queue_overflow_total",
    "input_tokens_total",
    "output_tokens_total",
)


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = {name: 0.0 for name in _COUNTERS}
        self._counters["estimated_cost_usd_total"] = 0.0
        # Gauges are supplied by callables at render time (live values).
        self._gauges: dict[str, Callable[[], float]] = {}

    def inc(self, name: str, amount: float = 1.0) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0.0) + amount

    def register_gauge(self, name: str, provider: Callable[[], float]) -> None:
        self._gauges[name] = provider

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            data = dict(self._counters)
        for name, provider in self._gauges.items():
            try:
                data[name] = float(provider())
            except Exception:
                data[name] = 0.0
        return data

    def render_prometheus(self) -> str:
        data = self.snapshot()
        lines: list[str] = []
        for name in sorted(data):
            metric = f"gateway_{name}"
            kind = "gauge" if name in self._gauges else "counter"
            lines.append(f"# TYPE {metric} {kind}")
            lines.append(f"{metric} {data[name]}")
        return "\n".join(lines) + "\n"
