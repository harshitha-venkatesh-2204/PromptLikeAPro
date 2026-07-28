"""Gateway orchestration: auth-scoped request -> pooled, rate-limited Claude call.

Ties together the key pool, the concurrency/queue controls, the per-player
protections, structured logging, and metrics. This is where requirement 3's
retry/backoff/jitter/retry-after/failover/cooldown policy lives, and where the
per-request structured log line is emitted.
"""
from __future__ import annotations

import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .anthropic_client import AnthropicClient, MessageResult
from .config import Settings
from .errors import GatewayBusy, NoKeysAvailable, UpstreamNonRetryable, UpstreamRetryable
from .keypool import KeyPool
from .limits import GlobalLimiter, PlayerLimiter
from .logging_utils import hash_player_id, log_event
from .metrics import Metrics


@dataclass
class GatewayRequest:
    player_id: str
    prompt: str
    system: str | None = None
    max_tokens: int | None = None
    server_label: str = ""
    preferred_key_label: str | None = None


@dataclass
class GatewayResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    request_id: str
    upstream_request_id: str
    key_label: str
    retry_count: int
    latency_ms: float
    estimated_cost_usd: float


@dataclass
class _Telemetry:
    key_label: str = ""
    retry_count: int = 0
    failovers: int = 0
    cooldowns: int = 0
    events: list[str] = field(default_factory=list)


class LLMGateway:
    def __init__(
        self,
        settings: Settings,
        pool: KeyPool,
        client: AnthropicClient,
        global_limiter: GlobalLimiter,
        player_limiter: PlayerLimiter,
        metrics: Metrics,
        logger: logging.Logger,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.pool = pool
        self.client = client
        self.global_limiter = global_limiter
        self.player_limiter = player_limiter
        self.metrics = metrics
        self.logger = logger
        self._rng = rng
        self._clock = clock
        if sleep is None:
            import asyncio

            self._sleep = asyncio.sleep
        else:
            self._sleep = sleep

    # -- public entry --------------------------------------------------------
    async def handle(self, req: GatewayRequest) -> GatewayResponse:
        request_id = uuid.uuid4().hex
        player_hash = hash_player_id(req.player_id, self.settings.hash_salt)
        self.metrics.inc("requests_total")

        if not req.player_id:
            raise ValueError("player_id is required.")

        # Enforce input-token limit (heuristic: chars / chars_per_token).
        approx_input = self._estimate_input_tokens(req)
        if approx_input > self.settings.max_input_tokens:
            self._log(
                request_id, player_hash, status=400, tel=_Telemetry(),
                input_tokens=approx_input, output_tokens=0, latency_ms=0.0,
                cost=0.0, outcome="input_too_large",
            )
            raise ValueError(
                f"Prompt is too long ({approx_input} tokens; limit "
                f"{self.settings.max_input_tokens})."
            )

        max_tokens = self._clamp_output_tokens(req.max_tokens)
        tel = _Telemetry()
        started = self._clock()

        # Per-player protection (concurrency + cooldown).
        try:
            self.player_limiter.try_begin(req.player_id)
        except GatewayBusy as busy:
            self.metrics.inc("player_busy_total")
            self._log(
                request_id, player_hash, status=429, tel=tel, input_tokens=approx_input,
                output_tokens=0, latency_ms=0.0, cost=0.0, outcome=busy.reason,
            )
            raise

        try:
            # Global concurrency + bounded queue (org-wide limit).
            async with self.global_limiter.slot():
                result = await self._dispatch(req, max_tokens, tel)
        except GatewayBusy as busy:
            if busy.reason == "queue_full":
                self.metrics.inc("queue_overflow_total")
            self._log(
                request_id, player_hash, status=503, tel=tel, input_tokens=approx_input,
                output_tokens=0, latency_ms=self._elapsed_ms(started), cost=0.0,
                outcome=busy.reason,
            )
            raise
        except UpstreamNonRetryable as exc:
            self.metrics.inc("failure_client_error_total")
            self._log(
                request_id, player_hash, status=exc.status, tel=tel, input_tokens=approx_input,
                output_tokens=0, latency_ms=self._elapsed_ms(started), cost=0.0,
                outcome="upstream_client_error",
            )
            raise
        except NoKeysAvailable:
            self._log(
                request_id, player_hash, status=503, tel=tel, input_tokens=approx_input,
                output_tokens=0, latency_ms=self._elapsed_ms(started), cost=0.0,
                outcome="no_keys_available",
            )
            raise
        finally:
            self.player_limiter.end(req.player_id)

        latency_ms = self._elapsed_ms(started)
        cost = self._estimate_cost(result.input_tokens, result.output_tokens)
        self.metrics.inc("success_total")
        self.metrics.inc("input_tokens_total", result.input_tokens)
        self.metrics.inc("output_tokens_total", result.output_tokens)
        self.metrics.inc("estimated_cost_usd_total", cost)

        self._log(
            request_id, player_hash, status=200, tel=tel,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
            latency_ms=latency_ms, cost=cost, outcome="success",
            upstream_request_id=result.request_id,
        )

        return GatewayResponse(
            text=result.text,
            model=result.model or self.settings.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            request_id=request_id,
            upstream_request_id=result.request_id,
            key_label=tel.key_label,
            retry_count=tel.retry_count,
            latency_ms=latency_ms,
            estimated_cost_usd=cost,
        )

    # -- retry / failover loop ----------------------------------------------
    async def _dispatch(self, req: GatewayRequest, max_tokens: int, tel: _Telemetry) -> MessageResult:
        candidates = self.pool.candidate_sequence(req.preferred_key_label)
        if not candidates:
            self.metrics.inc("failure_no_keys_total")
            raise NoKeysAvailable()

        attempts_per_key = self.settings.max_attempts_per_key
        for c_index, key in enumerate(candidates):
            for attempt in range(attempts_per_key):
                try:
                    result = await self.client.create_message(
                        api_key=key.secret,
                        model=self.settings.model,
                        prompt=req.prompt,
                        max_tokens=max_tokens,
                        system=req.system,
                        thinking_mode=self.settings.thinking_mode,
                    )
                    self.pool.record_success(key.label)
                    tel.key_label = key.label
                    return result
                except UpstreamNonRetryable:
                    # 4xx invalid request: do NOT rotate or cooldown; fail fast.
                    tel.key_label = key.label
                    raise
                except UpstreamRetryable as exc:
                    tel.retry_count += 1
                    self.metrics.inc("retries_total")
                    if attempt < attempts_per_key - 1:
                        delay = self._compute_delay(attempt, exc.retry_after)
                        tel.events.append(f"retry:{key.label}:{exc.kind}:{round(delay, 3)}")
                        await self._sleep(delay)
                        continue
                    # Attempts on this key are exhausted -> cooldown + failover.
                    self.pool.put_cooldown(key.label, self.settings.key_cooldown_seconds)
                    self.metrics.inc("cooldowns_total")
                    tel.cooldowns += 1
                    tel.events.append(f"cooldown:{key.label}:{exc.kind}")
                    if c_index < len(candidates) - 1:
                        self.metrics.inc("failovers_total")
                        tel.failovers += 1
                        tel.events.append(f"failover:{key.label}->{candidates[c_index + 1].label}")
                    break  # try next candidate key

        self.metrics.inc("failure_upstream_total")
        raise NoKeysAvailable("All API keys failed or are in cooldown; please retry shortly.")

    # -- helpers -------------------------------------------------------------
    def _compute_delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self.settings.retry_after_cap_seconds)
        base = self.settings.backoff_base_seconds
        delay = base * (2 ** attempt)
        delay += self._rng() * base  # random jitter in [0, base)
        return min(delay, self.settings.backoff_max_seconds)

    def _estimate_input_tokens(self, req: GatewayRequest) -> int:
        chars = len(req.prompt) + (len(req.system) if req.system else 0)
        per = max(1, self.settings.chars_per_token)
        return (chars + per - 1) // per

    def _clamp_output_tokens(self, requested: int | None) -> int:
        cap = self.settings.max_output_tokens
        if requested is None or requested <= 0:
            return cap
        return min(requested, cap)

    def _estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        cost = (
            input_tokens / 1_000_000 * self.settings.price_input_per_mtok
            + output_tokens / 1_000_000 * self.settings.price_output_per_mtok
        )
        return round(cost, 8)

    def _elapsed_ms(self, started: float) -> float:
        return round((self._clock() - started) * 1000.0, 2)

    def _log(
        self,
        request_id: str,
        player_hash: str,
        *,
        status: int,
        tel: _Telemetry,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
        cost: float,
        outcome: str,
        upstream_request_id: str = "",
    ) -> None:
        log_event(
            self.logger,
            "llm_request",
            request_id=request_id,
            player_id_hash=player_hash,
            key_label=tel.key_label,
            model=self.settings.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            http_status=status,
            retry_count=tel.retry_count,
            failover_count=tel.failovers,
            cooldown_count=tel.cooldowns,
            events=tel.events,
            estimated_cost_usd=cost,
            outcome=outcome,
            upstream_request_id=upstream_request_id,
        )
