"""App-level wiring: auth gate, health, and metrics (no upstream network calls)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from gateway.app import create_app

from .conftest import make_settings


def test_requires_server_token():
    with TestClient(create_app(make_settings())) as client:
        # No Authorization header -> 401, and no upstream call is made.
        resp = client.post("/v1/messages", json={"player_id": "p1", "prompt": "hi"})
        assert resp.status_code == 401
        assert resp.json()["code"] == "unauthorized"

        resp = client.post(
            "/v1/messages",
            headers={"Authorization": "Bearer wrong-token"},
            json={"player_id": "p1", "prompt": "hi"},
        )
        assert resp.status_code == 401


def test_input_too_large_returns_400_without_upstream():
    settings = make_settings(max_input_tokens=50, chars_per_token=4)
    with TestClient(create_app(settings)) as client:
        resp = client.post(
            "/v1/messages",
            headers={"Authorization": "Bearer servertoken-1"},
            json={"player_id": "p1", "prompt": "x" * 5000},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_request"


def test_healthz():
    with TestClient(create_app(make_settings())) as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["keys"]["active_available"] == 6
        assert "model" in body["config"]


def test_metrics_exposition():
    with TestClient(create_app(make_settings())) as client:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "gateway_requests_total" in resp.text
        assert "gateway_keys_available" in resp.text
