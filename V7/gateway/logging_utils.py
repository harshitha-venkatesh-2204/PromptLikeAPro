"""Structured JSON logging with hard secret redaction.

Two layers protect against ever writing a secret:
  1. The gateway only ever passes non-sensitive fields into log records.
  2. ``SecretRedactionFilter`` scans every record as defense-in-depth and masks
     anything that looks like an Anthropic key (``sk-ant-...``) or bearer token.

Player IDs are logged only as a salted SHA-256 hash, never in the clear.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from typing import Any

# Anthropic keys look like: sk-ant-...  (also covers sk-ant-oat01-, sk-ant-api03-)
_SECRET_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")
# Generic bearer tokens in any stray string.
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}")

_REDACTED = "***REDACTED***"

# Fields already present on a stdlib LogRecord that we do not want to duplicate
# into the JSON "fields" section.
_STD_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName",
}


def _scrub(value: Any) -> Any:
    """Recursively mask secret-looking substrings in strings/containers."""
    if isinstance(value, str):
        scrubbed = _SECRET_RE.sub(_REDACTED, value)
        scrubbed = _BEARER_RE.sub(lambda m: m.group(1) + _REDACTED, scrubbed)
        return scrubbed
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


class SecretRedactionFilter(logging.Filter):
    """Mask secret-looking text in the message and any structured fields."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _SECRET_RE.sub(_REDACTED, record.msg)
            record.msg = _BEARER_RE.sub(lambda m: m.group(1) + _REDACTED, record.msg)
        if record.args:
            record.args = tuple(_scrub(a) for a in record.args) if isinstance(record.args, tuple) else _scrub(record.args)
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            record.fields = _scrub(fields)
        return True


class JsonFormatter(logging.Formatter):
    """Render each record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Configure the ``gateway`` logger with JSON output and secret redaction."""
    logger = logging.getLogger("gateway")
    logger.setLevel(level)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(SecretRedactionFilter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def hash_player_id(player_id: str, salt: str) -> str:
    """Return a short, stable, non-reversible hash of a player ID."""
    digest = hashlib.sha256(f"{salt}:{player_id}".encode("utf-8")).hexdigest()
    return digest[:16]


def log_event(logger: logging.Logger, message: str, **fields: Any) -> None:
    """Emit a structured operational log line. Never pass secret values here."""
    logger.info(message, extra={"fields": fields})
