"""AWS Bedrock Messages client (boto3 ``bedrock-runtime``).

Drop-in replacement for ``AnthropicClient``: same ``create_message`` signature,
same typed errors, so ``service.py`` (retries, backoff, failover, logging)
needs no changes. The ``api_key`` argument is accepted for interface
compatibility but ignored — AWS credentials are held by the boto3 client.

boto3 is synchronous, so each call runs in a worker thread via
``asyncio.to_thread`` to keep the FastAPI event loop free.

Error classification (mirrors anthropic_client):
  - ThrottlingException / ServiceUnavailable / ModelNotReady / 5xx / network
        -> ``UpstreamRetryable``
  - ValidationException / AccessDenied / ExpiredToken / other 4xx
        -> ``UpstreamNonRetryable``
"""
from __future__ import annotations

import asyncio
import json
import threading

from .anthropic_client import MessageResult
from .errors import UpstreamNonRetryable, UpstreamRetryable

_RETRYABLE_CODES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "InternalServerException",
    "ModelNotReadyException",
    "ModelTimeoutException",
}

_BEDROCK_ANTHROPIC_VERSION = "bedrock-2023-05-31"


class BedrockClient:
    """One method: send a Messages request to Claude on AWS Bedrock."""

    def __init__(
        self,
        *,
        region: str = "us-east-1",
        access_key: str = "",
        secret_key: str = "",
        session_token: str = "",
        timeout_seconds: float = 30.0,
    ) -> None:
        import boto3
        from botocore.config import Config as BotoConfig

        kwargs: dict[str, object] = {
            "region_name": region,
            "config": BotoConfig(
                connect_timeout=timeout_seconds,
                read_timeout=timeout_seconds,
                retries={"max_attempts": 0},  # the gateway owns retry policy
            ),
        }
        # Explicit credentials if provided; otherwise boto3 falls back to the
        # default chain (env vars, ~/.aws/credentials, instance role).
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key
            if session_token:
                kwargs["aws_session_token"] = session_token
        self._client = boto3.client("bedrock-runtime", **kwargs)
        # boto3 clients are thread-safe for calls, but serialize defensively
        # only around invoke_model construction-free state; calls can be
        # concurrent. Kept for clarity; no shared mutable state is touched.
        self._lock = threading.Lock()  # noqa: F841  (documented no-op)

    async def create_message(
        self,
        *,
        api_key: str,  # ignored (interface compatibility with AnthropicClient)
        model: str,
        prompt: str,
        max_tokens: int,
        system: str | None = None,
        thinking_mode: str = "disabled",
    ) -> MessageResult:
        payload: dict[str, object] = {
            "anthropic_version": _BEDROCK_ANTHROPIC_VERSION,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system
        # Extended thinking is left at the model default on Bedrock; the booth
        # runs Haiku with bounded outputs, so no thinking block is sent.

        return await asyncio.to_thread(self._invoke_sync, model, payload)

    # -- sync worker ---------------------------------------------------------
    def _invoke_sync(self, model: str, payload: dict[str, object]) -> MessageResult:
        from botocore.exceptions import (
            BotoCoreError,
            ClientError,
            ConnectionError as BotoConnectionError,
            ReadTimeoutError,
        )

        try:
            resp = self._client.invoke_model(
                modelId=model,
                body=json.dumps(payload),
                contentType="application/json",
                accept="application/json",
            )
        except ClientError as exc:
            raise self._classify_client_error(exc) from exc
        except (ReadTimeoutError,) as exc:
            raise UpstreamRetryable("timeout", f"Bedrock request timed out: {exc}") from exc
        except (BotoConnectionError, BotoCoreError) as exc:
            raise UpstreamRetryable("network", f"Bedrock network error: {exc}") from exc

        body = json.loads(resp["body"].read())
        text = "".join(
            block.get("text", "")
            for block in body.get("content", [])
            if block.get("type") == "text"
        )
        usage = body.get("usage") or {}
        request_id = str(
            (resp.get("ResponseMetadata") or {}).get("RequestId", "")
        )
        return MessageResult(
            text=text,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            model=str(body.get("model", "") or payload.get("anthropic_version", "")),
            request_id=request_id,
            status=200,
        )

    @staticmethod
    def _classify_client_error(exc: "Exception") -> Exception:
        err = getattr(exc, "response", {}) or {}
        meta = err.get("ResponseMetadata") or {}
        status = int(meta.get("HTTPStatusCode", 0) or 0)
        error = err.get("Error") or {}
        code = str(error.get("Code", ""))
        message = str(error.get("Message", "")) or f"Bedrock error {code or status}"

        if code == "ExpiredTokenException":
            return UpstreamNonRetryable(
                401,
                "AWS session token expired. Re-run `python -m gateway.bedrock_login` "
                "to mint fresh 24h credentials, then restart the gateway.",
            )
        if code in _RETRYABLE_CODES or status == 429 or status >= 500:
            kind = "rate_limit" if code in ("ThrottlingException", "TooManyRequestsException") or status == 429 else "server_error"
            return UpstreamRetryable(kind, message, status=status or 429)
        return UpstreamNonRetryable(status or 400, message)
