"""Required scenarios: normal success, 429 retry, retry-after, 5xx retry,
key cooldown, spare-key failover, queue overflow, per-player limit, and the
guarantee that secrets never reach the logs.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from gateway.errors import GatewayBusy, NoKeysAvailable, UpstreamNonRetryable
from gateway.logging_utils import log_event
from gateway.service import GatewayRequest

from .conftest import (
    ACTIVE_SECRET,
    SPARE_SECRET,
    FakeBlockingClient,
    bad_request,
    make_gateway,
    make_settings,
    make_transport,
    ok_response,
    rate_limited,
    server_error,
)


def _req(player_id="p1", prompt="hello world", preferred="active-1", **kw):
    return GatewayRequest(player_id=player_id, prompt=prompt, preferred_key_label=preferred, **kw)


async def test_normal_success():
    h = make_gateway(transport=make_transport({ACTIVE_SECRET.format(1): [ok_response]}))
    r = await h.gateway.handle(_req())
    assert r.text == "scored"
    assert r.key_label == "active-1"
    assert (r.input_tokens, r.output_tokens) == (10, 5)
    assert r.retry_count == 0
    snap = h.metrics.snapshot()
    assert snap["requests_total"] == 1
    assert snap["success_total"] == 1
    # cost = 10/1e6*3 + 5/1e6*15
    assert r.estimated_cost_usd == pytest.approx(10 / 1e6 * 3 + 5 / 1e6 * 15)


async def test_structured_log_has_required_fields():
    h = make_gateway(transport=make_transport({ACTIVE_SECRET.format(1): [ok_response]}))
    await h.gateway.handle(_req(player_id="alice"))
    record = json.loads(h.log_stream.getvalue().strip().splitlines()[-1])
    for field in (
        "timestamp", "request_id", "player_id_hash", "key_label", "model",
        "input_tokens", "output_tokens", "latency_ms", "http_status",
        "retry_count", "cooldown_count", "failover_count", "estimated_cost_usd",
    ):
        assert field in record, f"missing {field}"
    assert record["http_status"] == 200
    assert record["player_id_hash"] != "alice"  # hashed, not clear text


async def test_429_then_retry_success():
    transport = make_transport({ACTIVE_SECRET.format(1): [lambda: rate_limited(), ok_response]})
    h = make_gateway(transport=transport)
    r = await h.gateway.handle(_req())
    assert r.text == "scored"
    assert r.key_label == "active-1"
    assert r.retry_count == 1
    assert len(h.sleeps.calls) == 1  # one backoff between the two attempts
    assert h.metrics.snapshot()["retries_total"] == 1


async def test_retry_after_header_respected():
    transport = make_transport({ACTIVE_SECRET.format(1): [lambda: rate_limited(retry_after=7), ok_response]})
    h = make_gateway(transport=transport)
    r = await h.gateway.handle(_req())
    assert r.text == "scored"
    # The backoff used the server-provided retry-after (7s), not exponential.
    assert h.sleeps.calls == [7.0]


async def test_5xx_then_retry_success():
    transport = make_transport({ACTIVE_SECRET.format(1): [server_error, ok_response]})
    h = make_gateway(transport=transport)
    r = await h.gateway.handle(_req())
    assert r.text == "scored"
    assert r.retry_count == 1


async def test_key_cooldown_after_exhausted_attempts():
    settings = make_settings(active=1, spare=0)
    transport = make_transport({ACTIVE_SECRET.format(1): [lambda: rate_limited()]})  # always 429
    h = make_gateway(settings=settings, transport=transport)
    with pytest.raises(NoKeysAvailable):
        await h.gateway.handle(_req())
    assert h.pool.is_available("active-1") is False
    snap = h.metrics.snapshot()
    assert snap["cooldowns_total"] == 1
    assert snap["retries_total"] == 3  # three attempts on the one key
    # Recovers automatically once the 60s cooldown elapses.
    h.clock.advance(61)
    assert h.pool.is_available("active-1") is True


async def test_spare_key_failover():
    settings = make_settings(active=1, spare=1)
    transport = make_transport({
        ACTIVE_SECRET.format(1): [lambda: rate_limited()],  # active always 429
        SPARE_SECRET.format(1): [ok_response],              # spare succeeds
    })
    h = make_gateway(settings=settings, transport=transport)
    r = await h.gateway.handle(_req())
    assert r.text == "scored"
    assert r.key_label == "spare-1"  # failed over to the spare
    snap = h.metrics.snapshot()
    assert snap["failovers_total"] == 1
    assert snap["cooldowns_total"] == 1
    assert h.pool.is_available("active-1") is False


async def test_invalid_request_does_not_rotate_or_cooldown():
    settings = make_settings(active=1, spare=1)
    transport = make_transport({ACTIVE_SECRET.format(1): [bad_request]})
    h = make_gateway(settings=settings, transport=transport)
    with pytest.raises(UpstreamNonRetryable) as ei:
        await h.gateway.handle(_req())
    assert ei.value.status == 400
    # 4xx must not cool down the key or fail over.
    assert h.pool.is_available("active-1") is True
    snap = h.metrics.snapshot()
    assert snap["cooldowns_total"] == 0
    assert snap["failovers_total"] == 0
    assert snap["retries_total"] == 0


async def test_queue_overflow_returns_busy():
    client = FakeBlockingClient(release_immediately=False)
    settings = make_settings(max_concurrency=1, max_queue=0)
    h = make_gateway(settings=settings, client=client)

    t1 = asyncio.create_task(h.gateway.handle(_req(player_id="p1")))
    await asyncio.sleep(0.05)  # let t1 occupy the single concurrency slot

    with pytest.raises(GatewayBusy) as ei:
        await h.gateway.handle(_req(player_id="p2"))
    assert ei.value.reason == "queue_full"
    assert h.metrics.snapshot()["queue_overflow_total"] == 1

    client.gate.set()
    await t1


async def test_per_player_one_concurrent_request():
    client = FakeBlockingClient(release_immediately=False)
    h = make_gateway(settings=make_settings(max_concurrency=8), client=client)

    t1 = asyncio.create_task(h.gateway.handle(_req(player_id="p1")))
    await asyncio.sleep(0.05)

    with pytest.raises(GatewayBusy) as ei:
        await h.gateway.handle(_req(player_id="p1"))
    assert ei.value.reason == "player_concurrent"
    assert h.metrics.snapshot()["player_busy_total"] == 1

    client.gate.set()
    await t1


async def test_per_player_cooldown_between_requests():
    client = FakeBlockingClient(release_immediately=True)
    h = make_gateway(client=client)  # 5s player cooldown, deterministic clock

    r1 = await h.gateway.handle(_req(player_id="p1"))
    assert r1.text == "ok"

    with pytest.raises(GatewayBusy) as ei:
        await h.gateway.handle(_req(player_id="p1"))
    assert ei.value.reason == "player_cooldown"
    assert ei.value.retry_after is not None

    h.clock.advance(6)  # past the cooldown window
    r3 = await h.gateway.handle(_req(player_id="p1"))
    assert r3.text == "ok"


async def test_input_token_limit_rejected():
    h = make_gateway(settings=make_settings(max_input_tokens=50, chars_per_token=4))
    long_prompt = "x" * 5000  # ~1250 tokens, over the 50-token limit
    with pytest.raises(ValueError):
        await h.gateway.handle(_req(prompt=long_prompt))


async def test_secrets_never_appear_in_logs():
    secret1 = ACTIVE_SECRET.format(1)
    h = make_gateway(transport=make_transport({secret1: [ok_response]}))
    await h.gateway.handle(_req(player_id="secret-player-name"))

    # Deliberately try to leak a secret through the same logging pipeline.
    log_event(h.logger, "leak_attempt", note=f"Authorization Bearer {secret1}", raw_key=secret1)

    out = h.log_stream.getvalue()
    assert "sk-ant" not in out                 # no key prefix anywhere
    assert secret1 not in out                  # exact secret never present
    assert "secret-player-name" not in out     # player id is hashed, not clear
    assert "REDACTED" in out                    # the deliberate leak was masked
