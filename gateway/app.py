"""FastAPI application: the internal LLM endpoint game servers call.

Endpoints:
  POST /v1/messages  - authenticated (game-server bearer token) LLM call
  GET  /healthz      - liveness/readiness (no secrets)
  GET  /metrics      - Prometheus text exposition

Only game servers can reach the LLM: every /v1/messages request must present a
valid server token in ``Authorization: Bearer <token>``. The token identifies
which of the 6 backend systems is calling, which selects that system's assigned
active key. Anthropic keys never leave the gateway.
"""
from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .anthropic_client import AnthropicClient
from .config import ServerToken, Settings, load_settings
from .errors import GatewayBusy, NoKeysAvailable, UpstreamNonRetryable
from .keypool import KeyPool
from .limits import GlobalLimiter, PlayerLimiter
from .logging_utils import configure_logging, log_event
from .metrics import Metrics
from .service import GatewayRequest, LLMGateway


class MessageRequest(BaseModel):
    player_id: str = Field(..., min_length=1, max_length=128)
    prompt: str = Field(..., min_length=1)
    system: Optional[str] = Field(default=None, max_length=20000)
    max_tokens: Optional[int] = Field(default=None, ge=1, le=8192)


def _authenticate(settings: Settings, authorization: Optional[str]) -> ServerToken:
    """Validate the game-server bearer token in constant time."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise GatewayBusy("unauthorized", "Missing or malformed Authorization header.")
    presented = authorization.split(" ", 1)[1].strip()
    match: Optional[ServerToken] = None
    for st in settings.server_tokens:
        # compare_digest for every candidate to avoid early-exit timing leaks
        if hmac.compare_digest(presented, st.token):
            match = st
    if match is None:
        raise GatewayBusy("unauthorized", "Invalid server token.")
    return match


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or load_settings()
    logger = configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http_client = httpx.AsyncClient(timeout=settings.request_timeout_seconds)
        pool = KeyPool(
            settings.active_keys, settings.spare_keys,
            cooldown_seconds=settings.key_cooldown_seconds,
        )
        client = AnthropicClient(
            http_client, base_url=settings.anthropic_base_url,
            anthropic_version=settings.anthropic_version,
        )
        global_limiter = GlobalLimiter(settings.max_concurrency, settings.max_queue)
        player_limiter = PlayerLimiter(settings.player_cooldown_seconds)
        metrics = Metrics()
        metrics.register_gauge("in_flight", lambda: global_limiter.in_flight)
        metrics.register_gauge("queue_depth", lambda: global_limiter.queue_depth)
        metrics.register_gauge("keys_available", lambda: pool.available_count())

        gateway = LLMGateway(
            settings, pool, client, global_limiter, player_limiter, metrics, logger,
        )
        app.state.settings = settings
        app.state.http_client = http_client
        app.state.pool = pool
        app.state.metrics = metrics
        app.state.gateway = gateway
        app.state.global_limiter = global_limiter

        if not settings.active_keys:
            logger.warning("No active Anthropic keys loaded; the gateway is degraded.")
        log_event(
            logger, "gateway_started",
            active_keys=len(settings.active_keys),
            spare_keys=len(settings.spare_keys),
            servers=len(settings.server_tokens),
            model=settings.model,
        )
        try:
            yield
        finally:
            await http_client.aclose()

    app = FastAPI(title="Prompt Like A PRO LLM Gateway", version="1.0.0", lifespan=lifespan)

    def _busy_response(busy: GatewayBusy) -> JSONResponse:
        if busy.reason == "unauthorized":
            return JSONResponse(
                status_code=401,
                content={"ok": False, "code": "unauthorized", "message": busy.message},
            )
        status = 503 if busy.reason == "queue_full" else 429
        headers = {}
        if busy.retry_after is not None:
            headers["Retry-After"] = str(int(busy.retry_after) or 1)
        return JSONResponse(
            status_code=status,
            headers=headers,
            content={
                "ok": False, "code": busy.reason,
                "message": "Busy, try again shortly.",
                "detail": busy.message, "retry_after": busy.retry_after,
            },
        )

    @app.post("/v1/messages")
    async def create_message(
        body: MessageRequest,
        request: Request,
        authorization: Optional[str] = Header(default=None),
    ):
        settings: Settings = request.app.state.settings
        gateway: LLMGateway = request.app.state.gateway
        try:
            server = _authenticate(settings, authorization)
        except GatewayBusy as busy:
            return _busy_response(busy)

        gw_req = GatewayRequest(
            player_id=body.player_id,
            prompt=body.prompt,
            system=body.system,
            max_tokens=body.max_tokens,
            server_label=server.label,
            preferred_key_label=server.preferred_key_label,
        )
        try:
            result = await gateway.handle(gw_req)
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "code": "invalid_request", "message": str(exc)},
            )
        except GatewayBusy as busy:
            return _busy_response(busy)
        except UpstreamNonRetryable as exc:
            # 400 from upstream is usually a bad prompt; other 4xx are key/config
            # problems we must not leak to the client.
            if exc.status == 400:
                return JSONResponse(
                    status_code=400,
                    content={"ok": False, "code": "invalid_request", "message": exc.message},
                )
            return JSONResponse(
                status_code=502,
                content={"ok": False, "code": "upstream_error", "message": "AI scoring is unavailable right now."},
            )
        except NoKeysAvailable:
            return JSONResponse(
                status_code=503,
                headers={"Retry-After": "2"},
                content={"ok": False, "code": "unavailable", "message": "Busy, try again shortly."},
            )

        return {
            "ok": True,
            "request_id": result.request_id,
            "model": result.model,
            "text": result.text,
            "usage": {
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            },
            "estimated_cost_usd": result.estimated_cost_usd,
            "latency_ms": result.latency_ms,
        }

    @app.get("/healthz")
    async def healthz(request: Request):
        settings: Settings = request.app.state.settings
        pool: KeyPool = request.app.state.pool
        limiter: GlobalLimiter = request.app.state.global_limiter
        available = pool.available_count()
        healthy = available > 0
        payload = {
            "ok": healthy,
            "status": "ok" if healthy else "degraded",
            "keys": pool.status(),
            "in_flight": limiter.in_flight,
            "queue_depth": limiter.queue_depth,
            "config": settings.public_summary(),
        }
        return JSONResponse(status_code=200 if healthy else 503, content=payload)

    @app.get("/metrics")
    async def metrics_endpoint(request: Request):
        metrics: Metrics = request.app.state.metrics
        return PlainTextResponse(metrics.render_prometheus(), media_type="text/plain; version=0.0.4")

    return app


app = None  # created by main.py / uvicorn factory to avoid import-time env reads
