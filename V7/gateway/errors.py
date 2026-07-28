"""Typed errors used across the gateway.

None of these carry secret values. Messages are safe to log.
"""
from __future__ import annotations


class GatewayError(Exception):
    """Base class for gateway errors."""


class GatewayBusy(GatewayError):
    """A protection limit was hit. Maps to a clean 'busy, try again shortly'.

    ``reason`` is a short machine code (e.g. ``player_concurrent``,
    ``player_cooldown``, ``queue_full``). ``retry_after`` is seconds (optional).
    """

    def __init__(self, reason: str, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.retry_after = retry_after


class UpstreamRetryable(GatewayError):
    """A retryable failure from Anthropic (429, 5xx) or a network/timeout error.

    ``retry_after`` is the parsed ``retry-after`` header value in seconds, if any.
    ``kind`` is a short label for metrics/logs (``rate_limit``, ``server_error``,
    ``overloaded``, ``network``, ``timeout``).
    """

    def __init__(self, kind: str, message: str, status: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.retry_after = retry_after


class UpstreamNonRetryable(GatewayError):
    """A non-retryable failure from Anthropic (4xx other than 429).

    Invalid-request errors must NOT trigger key rotation or cooldown.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class NoKeysAvailable(GatewayError):
    """Every candidate key failed or is in cooldown."""

    def __init__(self, message: str = "No API keys are currently available.") -> None:
        super().__init__(message)
        self.message = message
