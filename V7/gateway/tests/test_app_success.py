"""End-to-end happy path through the assembled FastAPI app.

The other app tests cover the auth gate, oversized input, health, and metrics
but never exercise a full HTTP 200 through ``create_app`` -> lifespan wiring ->
JSON response. This monkeypatches the upstream client (so no network) and
asserts the success envelope the game servers actually receive.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

import gateway.app as app_module
from gateway.anthropic_client import MessageResult
from gateway.app import create_app

from .conftest import make_settings


class _FakeUpstream:
    """Duck-typed AnthropicClient: accepts the lifespan ctor args, returns a hit."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def create_message(self, *, api_key, model, prompt, max_tokens,
                             system=None, thinking_mode="disabled") -> MessageResult:
        # api_key is a real pooled secret here; we simply don't use it.
        return MessageResult(
            text="scored: " + prompt[:16],
            input_tokens=12,
            output_tokens=8,
            model=model,
            request_id="req_upstream_test",
        )


def test_full_success_path_through_http(monkeypatch):
    monkeypatch.setattr(app_module, "AnthropicClient", _FakeUpstream)
    with TestClient(create_app(make_settings())) as client:
        resp = client.post(
            "/v1/messages",
            headers={"Authorization": "Bearer servertoken-1"},
            json={"player_id": "PLAYER-1", "prompt": "hello there", "max_tokens": 100},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["text"].startswith("scored")
        assert body["model"]  # non-empty model string
        assert body["usage"] == {"input_tokens": 12, "output_tokens": 8}
        assert body["estimated_cost_usd"] >= 0.0
        assert "request_id" in body


def test_success_then_metrics_incremented(monkeypatch):
    monkeypatch.setattr(app_module, "AnthropicClient", _FakeUpstream)
    with TestClient(create_app(make_settings())) as client:
        client.post(
            "/v1/messages",
            headers={"Authorization": "Bearer servertoken-1"},
            json={"player_id": "PLAYER-2", "prompt": "score this prompt"},
        )
        metrics = client.get("/metrics").text
        # The counters are emitted; the successful call moved them off zero.
        assert "gateway_success_total 1" in metrics
        assert "gateway_requests_total 1" in metrics
