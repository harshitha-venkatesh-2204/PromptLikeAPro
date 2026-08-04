"""Configuration and secret loading.

Secrets (Anthropic API keys, game-server tokens, hash salt) are read ONLY from
environment variables here. A single indirection, ``_load_secret``, is the one
place to swap in a secret manager (AWS Secrets Manager, GCP Secret Manager,
Vault) without touching the rest of the gateway.

Nothing in this module logs a secret value. ``Settings.public_summary`` returns
only non-sensitive facts (counts, labels, limits) for health/metrics output.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

# ---- Secret loader indirection -------------------------------------------------
# Default: environment variables. To use a secret manager, set the loader via
# ``set_secret_loader`` at process start (see README) so every secret read below
# routes through it. Values are never logged.

_secret_loader: Callable[[str], str] = lambda name: os.environ.get(name, "")


def set_secret_loader(loader: Callable[[str], str]) -> None:
    """Override how named secrets are resolved (e.g. a secret-manager client)."""
    global _secret_loader
    _secret_loader = loader


def _load_secret(name: str) -> str:
    value = _secret_loader(name)
    return value.strip() if value else ""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name, "").strip()
    return raw or default


@dataclass(frozen=True)
class LoadedKey:
    """One loaded key. ``secret`` is sensitive and must never be logged."""

    label: str
    kind: str  # "active" | "spare"
    secret: str = field(repr=False)  # excluded from repr so it never prints

    def __repr__(self) -> str:  # extra safety: no secret, ever
        return f"LoadedKey(label={self.label!r}, kind={self.kind!r})"


@dataclass(frozen=True)
class ServerToken:
    """A game-server credential. ``token`` is sensitive and never logged."""

    label: str
    token: str = field(repr=False)
    preferred_key_label: str = ""

    def __repr__(self) -> str:
        return f"ServerToken(label={self.label!r}, preferred_key_label={self.preferred_key_label!r})"


@dataclass
class Settings:
    # --- upstream / model ---
    anthropic_base_url: str
    anthropic_version: str
    model: str
    thinking_mode: str  # "disabled" | "adaptive" | "omit"
    request_timeout_seconds: float

    # --- keys ---
    active_keys: list[LoadedKey]
    spare_keys: list[LoadedKey]

    # --- server auth ---
    server_tokens: list[ServerToken]

    # --- key-pool behavior ---
    max_attempts_per_key: int
    key_cooldown_seconds: float
    backoff_base_seconds: float
    backoff_max_seconds: float
    retry_after_cap_seconds: float

    # --- org-wide (global) controls ---
    max_concurrency: int
    max_queue: int

    # --- per-player controls ---
    player_cooldown_seconds: float
    player_max_queue: int
    max_input_tokens: int
    max_output_tokens: int
    chars_per_token: int

    # --- logging / cost ---
    hash_salt: str = field(repr=False)
    price_input_per_mtok: float = 3.0
    price_output_per_mtok: float = 15.0

    def token_to_server(self) -> dict[str, ServerToken]:
        return {st.token: st for st in self.server_tokens}

    def public_summary(self) -> dict[str, object]:
        """Non-sensitive config for /healthz. No secrets."""
        return {
            "model": self.model,
            "active_keys": [k.label for k in self.active_keys],
            "spare_keys": [k.label for k in self.spare_keys],
            "server_labels": [s.label for s in self.server_tokens],
            "max_concurrency": self.max_concurrency,
            "max_queue": self.max_queue,
            "player_cooldown_seconds": self.player_cooldown_seconds,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "key_cooldown_seconds": self.key_cooldown_seconds,
            "max_attempts_per_key": self.max_attempts_per_key,
        }


def _load_key_series(prefix: str, kind: str, count: int) -> list[LoadedKey]:
    """Load ``prefix``1..``prefix``N from secrets, skipping any that are unset."""
    keys: list[LoadedKey] = []
    for i in range(1, count + 1):
        secret = _load_secret(f"{prefix}{i}")
        if secret:
            keys.append(LoadedKey(label=f"{kind}-{i}", kind=kind, secret=secret))
    return keys


def _parse_server_tokens(raw: str, active_labels: list[str]) -> list[ServerToken]:
    """Parse ``GATEWAY_SERVER_TOKENS`` = ``label:token,label:token``.

    Each server is assigned a preferred active key round-robin over the loaded
    active keys, so system-1 prefers active-1, system-2 prefers active-2, etc.
    """
    servers: list[ServerToken] = []
    if not raw.strip():
        return servers
    for idx, part in enumerate(raw.split(",")):
        part = part.strip()
        if not part or ":" not in part:
            continue
        label, token = part.split(":", 1)
        label, token = label.strip(), token.strip()
        if not label or not token:
            continue
        preferred = active_labels[idx % len(active_labels)] if active_labels else ""
        servers.append(ServerToken(label=label, token=token, preferred_key_label=preferred))
    return servers


def load_settings() -> Settings:
    """Build Settings from the environment. Call once at startup."""
    active_count = _env_int("GATEWAY_ACTIVE_KEY_COUNT", 1)
    spare_count = _env_int("GATEWAY_SPARE_KEY_COUNT", 0)

    active_keys = _load_key_series("ANTHROPIC_ACTIVE_KEY_", "active", active_count)
    spare_keys = _load_key_series("ANTHROPIC_SPARE_KEY_", "spare", spare_count)

    server_tokens = _parse_server_tokens(
        os.environ.get("GATEWAY_SERVER_TOKENS", ""),
        [k.label for k in active_keys],
    )

    return Settings(
        anthropic_base_url=_env_str("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        anthropic_version=_env_str("ANTHROPIC_VERSION", "2023-06-01"),
        model=_env_str("GATEWAY_MODEL", "claude-sonnet-5"),
        thinking_mode=_env_str("GATEWAY_THINKING", "disabled"),
        request_timeout_seconds=_env_float("GATEWAY_REQUEST_TIMEOUT_SECONDS", 30.0),
        active_keys=active_keys,
        spare_keys=spare_keys,
        server_tokens=server_tokens,
        max_attempts_per_key=_env_int("GATEWAY_MAX_ATTEMPTS_PER_KEY", 3),
        key_cooldown_seconds=_env_float("GATEWAY_KEY_COOLDOWN_SECONDS", 60.0),
        backoff_base_seconds=_env_float("GATEWAY_BACKOFF_BASE_SECONDS", 0.5),
        backoff_max_seconds=_env_float("GATEWAY_BACKOFF_MAX_SECONDS", 8.0),
        retry_after_cap_seconds=_env_float("GATEWAY_RETRY_AFTER_CAP_SECONDS", 60.0),
        max_concurrency=_env_int("GATEWAY_MAX_CONCURRENCY", 8),
        max_queue=_env_int("GATEWAY_MAX_QUEUE", 12),
        player_cooldown_seconds=_env_float("GATEWAY_PLAYER_COOLDOWN_SECONDS", 5.0),
        player_max_queue=_env_int("GATEWAY_PLAYER_MAX_QUEUE", 0),
        max_input_tokens=_env_int("GATEWAY_MAX_INPUT_TOKENS", 2000),
        max_output_tokens=_env_int("GATEWAY_MAX_OUTPUT_TOKENS", 1200),
        chars_per_token=_env_int("GATEWAY_CHARS_PER_TOKEN", 4),
        hash_salt=_load_secret("GATEWAY_HASH_SALT") or "prompt-like-a-pro-default-salt",
        price_input_per_mtok=_env_float("GATEWAY_PRICE_INPUT_PER_MTOK", 3.0),
        price_output_per_mtok=_env_float("GATEWAY_PRICE_OUTPUT_PER_MTOK", 15.0),
    )
