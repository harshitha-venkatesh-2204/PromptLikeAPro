"""Async Anthropic Messages client (raw HTTP via httpx).

Kept deliberately thin: build the request, classify the response, and surface a
typed result or a typed error. All retry/backoff/failover orchestration lives in
``service.py`` so this layer stays a single, testable round-trip.

Error classification (see shared/error-codes.md):
  - 429, 5xx, 529, and network/timeout errors  -> ``UpstreamRetryable``
  - other 4xx (400/401/403/404/413/422)         -> ``UpstreamNonRetryable``
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from .errors import UpstreamNonRetryable, UpstreamRetryable

MESSAGES_PATH = "/v1/messages"


@dataclass
class MessageResult:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    request_id: str
    status: int = 200


def _parse_retry_after(headers: httpx.Headers) -> float | None:
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


class AnthropicClient:
    """One method: send a Messages request with a specific API key."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str = "https://api.anthropic.com",
        anthropic_version: str = "2023-06-01",
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._version = anthropic_version

    async def create_message(
        self,
        *,
        api_key: str,
        model: str,
        prompt: str,
        max_tokens: int,
        system: str | None = None,
        thinking_mode: str = "disabled",
    ) -> MessageResult:
        payload: dict[str, object] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system
        if thinking_mode == "disabled":
            payload["thinking"] = {"type": "disabled"}
        elif thinking_mode == "adaptive":
            payload["thinking"] = {"type": "adaptive"}
        # "omit" -> send nothing (model default applies)

        headers = {
            "x-api-key": api_key,
            "anthropic-version": self._version,
            "content-type": "application/json",
        }

        try:
            resp = await self._client.post(
                self._base_url + MESSAGES_PATH, json=payload, headers=headers
            )
        except (httpx.TimeoutException,) as exc:
            raise UpstreamRetryable("timeout", f"Upstream request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamRetryable("network", f"Upstream network error: {exc}") from exc

        return self._handle_response(resp)

    def _handle_response(self, resp: httpx.Response) -> MessageResult:
        status = resp.status_code
        request_id = resp.headers.get("request-id", "")

        if status == 200:
            body = resp.json()
            text = "".join(
                block.get("text", "")
                for block in body.get("content", [])
                if block.get("type") == "text"
            )
            usage = body.get("usage") or {}
            return MessageResult(
                text=text,
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
                model=str(body.get("model", "")),
                request_id=request_id,
                status=status,
            )

        message = self._error_message(resp)

        if status == 429:
            raise UpstreamRetryable(
                "rate_limit", message, status=status, retry_after=_parse_retry_after(resp.headers)
            )
        if status == 529:
            raise UpstreamRetryable(
                "overloaded", message, status=status, retry_after=_parse_retry_after(resp.headers)
            )
        if status >= 500:
            raise UpstreamRetryable(
                "server_error", message, status=status, retry_after=_parse_retry_after(resp.headers)
            )
        # Any other 4xx is a non-retryable invalid request: do not rotate keys.
        raise UpstreamNonRetryable(status, message)

    @staticmethod
    def _error_message(resp: httpx.Response) -> str:
        try:
            body = resp.json()
            err = body.get("error") or {}
            return str(err.get("message") or body.get("message") or f"HTTP {resp.status_code}")
        except Exception:
            return f"HTTP {resp.status_code}"
