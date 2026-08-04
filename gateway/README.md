# Prompt Like A PRO — LLM Gateway

A production-ready, internal LLM gateway that sits between your game servers and
Claude. It authenticates game-server traffic, manages a single Anthropic key
with retry/cooldown, enforces global and
per-player limits, logs structured operational data (no secrets), and exposes
health/metrics endpoints.

> **Why a separate service?** The booth app (`app.py`, one directory up) is
> deliberately single-file and dependency-free. This gateway is an independent
> FastAPI deployable with its own dependencies, tests, and lifecycle — it does
> not change the booth app.

---

## What it does

- **Internal-only endpoint.** `POST /v1/messages` requires a game-server bearer
  token. The game **client** never gets an Anthropic key; only your backend
  systems hold a gateway token.
- **Keys from env / secret manager only.** The single API key is read from
  environment variables (or a pluggable secret manager). Keys are never
  hardcoded and never logged.
- **Key-pool manager.** Prefers the calling system's assigned active key; on
  `429` / retryable `5xx` / network errors it retries with exponential backoff +
  jitter, respects the `retry-after` header, and after the configured number of
  attempts puts the key in a 60s cooldown, after which it recovers automatically.
  Invalid-request (`4xx`) errors do **not** rotate keys.
- **Org-wide limits.** Throughput comes from the organization rate limit, so the
  gateway caps global concurrency and bounds a wait queue; excess load returns a
  clean "busy, try again shortly".
- **Per-player protections.** Player ID required; 1 concurrent request per
  player; cooldown between a player's requests; prompt (input) and output token
  limits; clean busy responses when limits are hit.
- **Structured logs.** One JSON line per request with timestamp, request ID,
  **hashed** player ID, key label (never the secret), model, input/output
  tokens, latency, HTTP status, retry count, cooldown/failover events, and
  estimated cost. A redaction filter masks anything key-shaped as a safety net.
- **Monitoring.** `GET /healthz` (readiness + key availability) and
  `GET /metrics` (Prometheus text).

---

## Setup

```bash
# From the project root
python -m venv .venv-gateway
. .venv-gateway/bin/activate            # Windows: .venv-gateway\Scripts\activate
pip install -r gateway/requirements.txt

cp gateway/.env.example gateway/.env    # then fill in real values (never commit)
```

Populate `gateway/.env` (see comments in `.env.example`):

- `ANTHROPIC_ACTIVE_KEY_1` — the API key.
- `GATEWAY_SERVER_TOKENS` — one `label:token` per game server. Generate tokens:
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- `GATEWAY_HASH_SALT` — a long random string for hashing player IDs in logs.

Load the env and run:

```bash
set -a; . gateway/.env; set +a        # export everything in .env
python -m gateway.main                # http://127.0.0.1:8100
```

### Using a secret manager instead of env files

Keys are read through a single indirection. At process start (e.g. a small
bootstrap before `create_app`), call:

```python
from gateway.config import set_secret_loader
import boto3, json

_sm = boto3.client("secretsmanager")
_cache = json.loads(_sm.get_secret_value(SecretId="prompt-like-a-pro/gateway")["SecretString"])
set_secret_loader(lambda name: _cache.get(name, ""))
```

Every `ANTHROPIC_*_KEY_*`, `GATEWAY_SERVER_TOKENS` is a plain env var, and the
key/salt reads route through the loader — so the same mechanism works for AWS
Secrets Manager, GCP Secret Manager, or Vault. Non-secret tuning stays in env.

---

## API

### `POST /v1/messages`
Headers: `Authorization: Bearer <server-token>`

```json
{ "player_id": "PLAYER-123", "prompt": "Act as a tutor...", "system": "optional", "max_tokens": 1024 }
```

Success `200`:

```json
{ "ok": true, "request_id": "…", "model": "claude-sonnet-5", "text": "…",
  "usage": {"input_tokens": 120, "output_tokens": 210},
  "estimated_cost_usd": 0.00378, "latency_ms": 812.4 }
```

Limit/busy responses: `429` (per-player concurrency/cooldown) or `503`
(global queue full / no keys), each with a `Retry-After` header and
`{"ok": false, "code": "...", "message": "Busy, try again shortly."}`.
Bad input is `400`; `401` for a missing/invalid server token.

Example:

```bash
curl -s http://127.0.0.1:8100/v1/messages \
  -H "Authorization: Bearer $SYSTEM_1_TOKEN" \
  -H "content-type: application/json" \
  -d '{"player_id":"PLAYER-123","prompt":"Act as a careful logic tutor. Solve step by step."}'
```

### `GET /healthz`
`200` when at least one key is available (else `503`). Returns key
availability, in-flight count, queue depth, and non-secret config.

### `GET /metrics`
Prometheus exposition: `gateway_requests_total`, `gateway_success_total`,
`gateway_retries_total`, `gateway_cooldowns_total`, `gateway_failovers_total`,
`gateway_queue_overflow_total`, `gateway_player_busy_total`,
`gateway_estimated_cost_usd_total`, `gateway_in_flight`,
`gateway_queue_depth`, `gateway_keys_available`, and more.

---

## Deployment

Single **centralized** gateway that all 6 game servers call (one process can
enforce the org-wide concurrency limit; per-server gateways cannot).

**Systemd + uvicorn workers** (example):

```ini
# /etc/systemd/system/plap-gateway.service
[Unit]
Description=Prompt Like A PRO LLM Gateway
After=network.target

[Service]
User=plap
EnvironmentFile=/opt/plap/gateway.env          # the API key, tokens, salt, tuning
WorkingDirectory=/opt/plap/app
ExecStart=/opt/plap/app/.venv-gateway/bin/uvicorn "gateway.main:create_asgi_app" \
          --factory --host 0.0.0.0 --port 8100 --workers 2
Restart=always
RestartSec=2

[Service]
# hardening
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/plap
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now plap-gateway
```

Notes:
- **Workers and global limits.** `GATEWAY_MAX_CONCURRENCY` is per process. With
  `--workers N`, set it to `desired_org_concurrency / N` so the fleet stays
  within the organization rate limit. For strict global control, run a single
  worker and scale via a load balancer to more single-worker instances only if
  the org limit allows.
- **Network.** Bind to a private interface / put it behind your internal LB or
  reverse proxy; game servers reach it over the internal network, players never
  do. Terminate TLS at the proxy.
- **Rotating keys.** Replace the `ANTHROPIC_*_KEY_*` values (env or secret
  manager) and restart — no code change. Because the keys were shared in a
  plaintext file during setup, rotate them in the Anthropic Console first.
- **Point the booth app at the gateway (optional).** The booth `app.py` calls
  Anthropic directly today. To route it through this gateway instead, have its
  `POST /api/ai_score` handler call `POST /v1/messages` with a server token —
  that removes the raw key from the booth process entirely. (Not done
  automatically; the booth app is left untouched.)

---

## Tests

```bash
. .venv-gateway/bin/activate
python -m pytest gateway -q
```

Covers: normal success, 429 retry, retry-after handling, 5xx retry, key
cooldown (and recovery), queue overflow, per-player
concurrency and cooldown, input-token limit, structured-log field completeness,
and the guarantee that secrets never appear in logs.

Tests use `httpx.MockTransport` (no network) and an injected sleep + clock (no
real waiting), so the full suite runs in well under a second.
