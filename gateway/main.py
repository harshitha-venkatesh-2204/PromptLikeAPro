"""Entrypoint for running the gateway with uvicorn.

    python -m gateway.main
    # or, for reload/workers:
    uvicorn "gateway.main:create_asgi_app" --factory --host 0.0.0.0 --port 8100

Host/port/log level come from the environment (GATEWAY_HOST, GATEWAY_PORT,
GATEWAY_LOG_LEVEL). See gateway/README.md for deployment guidance.
"""
from __future__ import annotations

import os

import uvicorn

from .app import create_app


def create_asgi_app():
    """Factory for ``uvicorn --factory`` / process managers."""
    return create_app()


def main() -> None:
    host = os.environ.get("GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("GATEWAY_PORT", "8100"))
    log_level = os.environ.get("GATEWAY_LOG_LEVEL", "info").lower()
    uvicorn.run(create_asgi_app(), host=host, port=port, log_level=log_level)


if __name__ == "__main__":
    main()
