"""Test helpers: build gateways with scripted upstream behavior, no real network,
no real sleeping, and a deterministic clock.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

import httpx

from gateway.anthropic_client import AnthropicClient, MessageResult
from gateway.config import LoadedKey, ServerToken, Settings
from gateway.keypool import KeyPool
from gateway.limits import GlobalLimiter, PlayerLimiter
from gateway.logging_utils import JsonFormatter, SecretRedactionFilter
from gateway.metrics import Metrics
from gateway.service import LLMGateway

# Distinctive fake secrets that match the real key shape so the redaction test
# is meaningful. These are NOT real keys.
ACTIVE_SECRET = "sk-ant-api03-ACTIVEKEY{}TESTONLYnotreal000000"
SPARE_SECRET = "sk-ant-api03-SPAREKEY{}TESTONLYnotreal0000000"


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class RecordingSleep:
    """Async sleep stand-in that records durations without waiting."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)


def make_settings(active: int = 6, spare: int = 3, **overrides) -> Settings:
    active_keys = [LoadedKey(f"active-{i}", "active", ACTIVE_SECRET.format(i)) for i in range(1, active + 1)]
    spare_keys = [LoadedKey(f"spare-{i}", "spare", SPARE_SECRET.format(i)) for i in range(1, spare + 1)]
    server_tokens = [
        ServerToken(f"system-{i}", f"servertoken-{i}", active_keys[(i - 1) % len(active_keys)].label if active_keys else "")
        for i in range(1, active + 1)
    ]
    base = dict(
        anthropic_base_url="https://api.anthropic.com",
        anthropic_version="2023-06-01",
        model="claude-sonnet-5",
        thinking_mode="disabled",
        request_timeout_seconds=5.0,
        active_keys=active_keys,
        spare_keys=spare_keys,
        server_tokens=server_tokens,
        max_attempts_per_key=3,
        key_cooldown_seconds=60.0,
        backoff_base_seconds=0.5,
        backoff_max_seconds=8.0,
        retry_after_cap_seconds=60.0,
        max_concurrency=8,
        max_queue=32,
        player_cooldown_seconds=5.0,
        player_max_queue=0,
        max_input_tokens=2000,
        max_output_tokens=1200,
        chars_per_token=4,
        hash_salt="test-salt",
        price_input_per_mtok=3.0,
        price_output_per_mtok=15.0,
    )
    base.update(overrides)
    return Settings(**base)


# ---- upstream response factories ----------------------------------------------

def ok_response(input_tokens: int = 10, output_tokens: int = 5) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": "scored"}],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            "model": "claude-sonnet-5",
        },
        headers={"request-id": "req_upstream_1"},
    )


def rate_limited(retry_after: Optional[int] = None) -> httpx.Response:
    headers = {}
    if retry_after is not None:
        headers["retry-after"] = str(retry_after)
    return httpx.Response(
        429,
        json={"type": "error", "error": {"type": "rate_limit_error", "message": "rate limited"}},
        headers=headers,
    )


def server_error() -> httpx.Response:
    return httpx.Response(500, json={"error": {"type": "api_error", "message": "boom"}})


def bad_request() -> httpx.Response:
    return httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": "bad prompt"}})


def make_transport(by_secret: dict[str, list[Callable[[], httpx.Response]]]) -> httpx.MockTransport:
    """MockTransport that returns scripted responses per API key (secret).

    Each value is a list of zero-arg factories; the last one repeats once the
    list is exhausted so a key can "always succeed" after its scripted prefix.
    """
    idx: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("x-api-key", "")
        seq = by_secret.get(key, [ok_response])
        i = idx.get(key, 0)
        factory = seq[min(i, len(seq) - 1)]
        idx[key] = i + 1
        return factory()

    return httpx.MockTransport(handler)


@dataclass
class GatewayHarness:
    gateway: LLMGateway
    metrics: Metrics
    pool: KeyPool
    sleeps: RecordingSleep
    clock: FakeClock
    log_stream: io.StringIO
    logger: logging.Logger
    settings: Settings


def make_logger() -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    logger = logging.getLogger(f"gateway.test.{id(stream)}")
    logger.handlers.clear()
    logger.setLevel("INFO")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(SecretRedactionFilter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger, stream


def make_gateway(
    *,
    settings: Optional[Settings] = None,
    transport: Optional[httpx.MockTransport] = None,
    client=None,
) -> GatewayHarness:
    settings = settings or make_settings()
    clock = FakeClock()
    sleeps = RecordingSleep()
    logger, stream = make_logger()
    metrics = Metrics()
    pool = KeyPool(settings.active_keys, settings.spare_keys, settings.key_cooldown_seconds, clock=clock)

    if client is None:
        http_client = httpx.AsyncClient(transport=transport or make_transport({}))
        client = AnthropicClient(http_client, base_url=settings.anthropic_base_url)

    global_limiter = GlobalLimiter(settings.max_concurrency, settings.max_queue)
    player_limiter = PlayerLimiter(settings.player_cooldown_seconds, clock=clock)

    gateway = LLMGateway(
        settings, pool, client, global_limiter, player_limiter, metrics, logger,
        sleep=sleeps, rng=lambda: 0.0, clock=clock,
    )
    return GatewayHarness(gateway, metrics, pool, sleeps, clock, stream, logger, settings)


class FakeBlockingClient:
    """Duck-typed AnthropicClient whose calls block on an event until released."""

    def __init__(self, release_immediately: bool = True) -> None:
        import asyncio

        self.gate = asyncio.Event()
        if release_immediately:
            self.gate.set()
        self.calls = 0

    async def create_message(self, **kwargs) -> MessageResult:
        self.calls += 1
        await self.gate.wait()
        return MessageResult(text="ok", input_tokens=1, output_tokens=1, model="m", request_id="r")
