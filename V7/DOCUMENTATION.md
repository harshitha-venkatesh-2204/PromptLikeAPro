# Prompt Like A PRO — System Documentation

A booth game that teaches practical prompt engineering. Players write prompts across
six mini‑games; each prompt is scored on a 6‑Block rubric — live by Claude when an AI
gateway is configured, or by a built‑in offline scorer otherwise. Scores feed a
projector leaderboard.

This document covers the whole system: the two components, how they connect, the games
and scoring, configuration, running, deployment, the data model, the APIs, and
day‑to‑day operations.

---

## 1. Architecture at a glance

The system is **two independent processes** plus Anthropic's API:

```
   Player browser (phone / laptop)
            │  HTTP  (game UI, scoring requests)
            ▼
   ┌──────────────────────┐        Authorization: Bearer <system token>
   │  Booth app  app.py    │  ───────────────────────────────────────────┐
   │  Python stdlib server │                                              │
   │  port 8000            │                                              ▼
   │  • serves index.html  │                                   ┌────────────────────────┐
   │  • SQLite (scores)    │                                   │  LLM Gateway (FastAPI) │
   │  • /api/ai_score  ────┼──────────────────────────────────▶│  port 8100 (PRIVATE)   │
   └──────────────────────┘                                    │  • holds 6+3 API keys  │
                                                               │  • retries / failover  │
                                                               │  • rate/queue limits   │
                                                               │  • x-api-key ──────────┼──▶ Anthropic
                                                               └────────────────────────┘        (Claude)
```

- **Booth app** (`app.py`) — a single‑file Python **standard‑library** HTTP server. It
  serves the player UI, stores scores in SQLite, and (for AI scoring) calls the gateway.
  It **never holds an Anthropic key**.
- **LLM gateway** (`gateway/`) — a **FastAPI** service that holds the Anthropic keys,
  authenticates the booth by a bearer token, manages a pool of keys with
  retries/failover/cooldown, and enforces organization‑wide and per‑player rate limits.
  It must stay **private** (only the booth calls it).
- **Anthropic API** — the gateway calls **Claude** (default model `claude-haiku-4-5`).

**Why split them?** So Anthropic keys live in exactly one place (the gateway), never
reach the browser or the booth process, and so the shared organization rate limit is
managed centrally with pooling, retries, and back‑pressure.

If no gateway is configured, the booth falls back to an **offline keyword scorer** and
still works fully.

---

## 2. Repository layout

```
app.py                     Booth backend: HTTP server, game registry (GAMES), SQLite, APIs, AI proxy
index.html                 Player app: login, game grid, 6 games, scoring, report, profile
leaderboard.html           Projector/Main-App live leaderboard (no login)
booth.env                  Local launch config for the booth (GATEWAY_URL, GATEWAY_TOKEN)  [not committed]
requirements.txt           Booth has no external deps (stdlib only)
run_stack.py               One-command launcher: starts the gateway AND the booth together
run_mac_linux.sh / .bat    Convenience wrappers around run_stack.py (double-click to run everything)
data/
  prompt_like_a_pro.sqlite3  SQLite DB (created at first run)   [not committed]

gateway/                   The LLM gateway (separate FastAPI service, its own venv)
  config.py                Settings + secret loading (keys, tokens) from env / secret file
  keypool.py               single-key pool with cooldown and recovery
  limits.py                GlobalLimiter (org concurrency+queue) + PlayerLimiter (per-player)
  anthropic_client.py      One async HTTP round-trip to Anthropic; error classification
  service.py               Orchestration: retry/backoff/jitter/retry-after/failover + logging
  logging_utils.py         Structured JSON logs + secret redaction + player-ID hashing
  metrics.py               Prometheus counters/gauges
  app.py                   FastAPI app: /v1/messages, /healthz, /metrics
  main.py                  uvicorn entrypoint
  errors.py                Typed gateway errors
  .env                     Real secrets: keys, server tokens, salt, tuning  [not committed]
  .env.example             Documented template
  README.md                Gateway-specific deep dive
  tests/                   pytest suite (retries, failover, cooldown, limits, no-secrets-in-logs)

.kiro/steering/            Project conventions (product / structure / tech)
```

---

## 3. The games

Six reference mini‑games, registered in the `GAMES` dict in `app.py` and rendered in
`index.html`:

| Game ID | Name | Badge | Gems |
|---|---|---|---|
| `personas` | Persona Roulette | Roulette Royalty | 20 |
| `missing` | Prompt Detective | Gap Hunter | 20 |
| `showdown` | Beat the Bot | Bot Slayer | 25 |
| `scene` | Draw With Words | Word Painter | 25 |
| `puzzle` | Crack the Vault | Vault Cracker | 30 |
| `mindreader` | The Mind Reader | Prompt Psychic | 30 |

**Rules:**
- **Play all six games; each game allows one retry.** A player can attempt each game up to
  twice (initial play + one retry, set by `MAX_ATTEMPTS_PER_GAME`); the **best score is
  kept**. Scores **accumulate** across games into the player's total. Enforced in the UI and
  on the server.
- **Start card shows the challenge.** Opening a game shows its challenge/question with a
  Start Game button; pressing Start arms the timer and shows the question beside the
  prompt box (side by side on tablet/laptop/projector; stacked on phones). The countdown
  begins on the player's first keystroke in the prompt box.
- **Prompt length is capped at 100 words** (`WORD_LIMIT` in `index.html`); the box shows a
  live `n / 100 words` counter and trims overflow.
- **Countdown timer** shown in the game header (difficulty‑based: Easy 2 minutes,
  Medium 3 minutes, Hard 4 minutes). It starts counting when the player starts typing.
  When it first hits 0:00 a bell rings and the player gets a one‑time
  **+1 minute grace period**; when the grace runs out, the current entry auto‑submits.

---

## 4. Scoring

Every prompt is graded out of **100**. Two scorers produce the same rubric shape:

- **AI judge (Claude, via the gateway)** — used when the booth has `GATEWAY_URL` +
  `GATEWAY_TOKEN`. Richer, generates example outputs and coaching.
- **Offline scorer (`heurJudge`)** — a keyword/heuristic block scanner in the browser,
  used automatically when the AI path is unavailable. Grades the same six blocks but
  cannot generate the example outputs.

### The 6‑Block rubric
Each prompt is scanned for six blocks, each graded **Full / Partial / Missing** with an
evidence quote:

| Block | What it is |
|---|---|
| **Role** | Who the AI should be |
| **Task** | One clear ask with an action verb |
| **Context** | Background facts |
| **Audience** | Who the output is for |
| **Format** | Exact structure and length |
| **Constraints** | Rules: tone, musts, nevers, verification |

Plus a **mission** layer (game‑specific) and a 10‑point **craft** layer (economy,
coherence).

### Ranks
Scores map to Builder ranks: **Master Builder** 90+, **Architect** 75+, **Builder** 60+,
**Apprentice** 40+, **Loose Blocks** below 40.

### The score report
The report screen shows:
- **The 6‑Block Scan** — each block's grade + evidence quote.
- **Mission and craft** rows.
- **Coaching** — *Power Move* (best thing done) and *Level Up* (highest‑value fix).
- **Side by side** (AI mode only): *your prompt → the output it produces*, and *an ideal
  prompt → the output it produces*.
- **Why the ideal prompt's output wins** (AI mode) — concrete bullets tying the ideal
  prompt's advantages to specific blocks/constraints (length cap, named audience, example
  style, tone, format).
- **Comparison** — a short justification of the score in terms of brief‑fit and control,
  not output aesthetics.

> The comparison and "why it wins" section deliberately justify the ideal prompt by
> **mission/audience fit and reliability**, so the reasoning holds even when a player's
> one‑off output happens to look flashier.

---

## 5. The AI path (booth → gateway → Claude)

1. Player submits a prompt → browser `POST /api/ai_score` with `{prompt_text, mission,
   game_id, san_id, extra}`.
2. `app.py` builds the judge request (a fixed system prompt + the player's prompt) and
   calls the gateway: `POST {GATEWAY_URL}/v1/messages` with
   `Authorization: Bearer {GATEWAY_TOKEN}` and `{player_id, system, prompt, max_tokens}`.
3. The gateway authenticates the booth, picks a pooled Anthropic key, calls Claude, and
   returns `{ok, text, usage, ...}`.
4. `app.py` parses the model's JSON into the rubric and returns it to the browser.

**Failure handling (three layers):**
- The **gateway** retries with exponential backoff + jitter, honors `retry-after`, and
  retries with backoff on the key before giving up.
- If the gateway ultimately fails (all keys exhausted, upstream error, timeout, or not
  configured), the booth returns a non‑OK response.
- The **browser** then falls back to the **offline scorer** and shows
  *"AI judge unavailable. Using the built‑in block scanner."* — the player always gets a
  score.

The booth's per‑request timeout is 60s; output is capped at `AI_MAX_TOKENS` (1500).

---

## 6. The LLM gateway

Full detail in `gateway/README.md`. Summary of behavior:

- **Keys** — a single API key, loaded by label from env or a secret file, **never
  logged**. Each booth "system" is assigned a preferred active key.
- **Key pool** — prefer the caller's assigned active key; on `429` / retryable `5xx` /
  network/timeout errors, retry with exponential backoff + jitter, respect the
  `retry-after` header, and after the configured attempts put the key in a **60s cooldown**
  and recover automatically after the cooldown. Invalid‑request
  (`4xx`) errors do **not** rotate keys.
- **Org‑wide limits** — all keys share one Anthropic organization rate limit, so the
  gateway caps **global concurrency** (default 16) and bounds a **wait queue** (default 32);
  excess load returns a clean "busy".
- **Per‑player limits** — player ID required; **1 concurrent** request per player; a
  **cooldown** between a player's requests; clean "busy" responses when hit.
- **Structured logs** — one JSON line per request: timestamp, request ID, **hashed** player
  ID, key **label** (never the secret), model, input/output tokens, latency, HTTP status,
  retry/cooldown/failover counts, estimated cost. A redaction filter masks anything
  key‑shaped as a safety net.
- **Endpoints** — `POST /v1/messages` (bearer‑authenticated), `GET /healthz` (readiness +
  key availability), `GET /metrics` (Prometheus).

### Concurrency, in practice
Each judge call takes ~6–10s on Haiku. With concurrency 16, up to 16 players are scored
fully in parallel (no queueing). Beyond that, requests queue (up to 32) and run as slots
free; past ~48 in flight, extras get instant offline scoring. The real ceiling is your
Anthropic **org rate limit**, not the key count.

---

## 7. Configuration

### Booth (`app.py`) — environment variables
| Variable | Default | Meaning |
|---|---|---|
| `GATEWAY_URL` | — | LLM gateway base URL, e.g. `http://127.0.0.1:8100`. Blank = offline scoring. |
| `GATEWAY_TOKEN` | — | The booth's gateway server token (a `system-*` token from `gateway/.env`). |

CLI equivalents: `--gateway-url`, `--gateway-token`, `--host`, `--port`, `--open`.
Prompt length is set by `WORD_LIMIT` in `index.html` (currently **100**).

### Gateway (`gateway/.env`) — key settings
| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_ACTIVE_KEY_1..6` | — | Active Anthropic keys. |
| `ANTHROPIC_SPARE_KEY_1..3` | — | Spare fallback keys. |
| `GATEWAY_SERVER_TOKENS` | — | `label:token,…` — one per backend system; system *N* → active key *N*. |
| `GATEWAY_HASH_SALT` | — | Salt for hashing player IDs in logs. |
| `GATEWAY_MODEL` | `claude-haiku-4-5` | Claude model used for judging. |
| `GATEWAY_MAX_CONCURRENCY` | `16` | Org‑wide simultaneous upstream calls. |
| `GATEWAY_MAX_QUEUE` | `32` | Requests allowed to wait before returning "busy". |
| `GATEWAY_MAX_ATTEMPTS_PER_KEY` | `3` | Retries per key before cooldown + failover. |
| `GATEWAY_KEY_COOLDOWN_SECONDS` | `60` | Cooldown after a key's attempts are exhausted. |
| `GATEWAY_PLAYER_COOLDOWN_SECONDS` | `5` | Minimum gap between one player's requests. |
| `GATEWAY_REQUEST_TIMEOUT_SECONDS` | `60` | Upstream request timeout. |

See `gateway/.env.example` for the full, commented list. **Secrets come only from the
environment / a secret manager — never commit `gateway/.env` or `booth.env`.**

---

## 8. Running locally

### One command (recommended)

After filling in `gateway/.env` (copy `gateway/.env.example` and add the keys/tokens/salt):

```bash
python3 run_stack.py          # macOS/Linux — or double-click run_mac_linux.sh
```
```bat
run_windows.bat               :: Windows — double-click; starts gateway + game
```

`run_stack.py` creates the gateway virtualenv on first run, starts the gateway,
waits for it to be healthy, then starts the booth wired to it (system‑1 token)
and prints the URLs. Ctrl+C stops both. Optional: `BOOTH_HOST`/`BOOTH_PORT`
env vars; `PLAP_OPEN=1` opens the browser (the run scripts set it).

### Manually, piece by piece

**Gateway** (has its own virtualenv):
```bash
cd <project>
python -m venv .venv-gateway
.venv-gateway/bin/pip install -r gateway/requirements.txt
cp gateway/.env.example gateway/.env      # fill in the API key, server tokens, salt
set -a; . gateway/.env; set +a
.venv-gateway/bin/python -m gateway.main   # listens on 127.0.0.1:8100
```

**Booth** (Python 3.9+, no dependencies):
```bash
# GATEWAY_TOKEN is the system-1 value from gateway/.env (GATEWAY_SERVER_TOKENS)
GATEWAY_URL=http://127.0.0.1:8100 GATEWAY_TOKEN=<system-1 token> \
  python3 app.py --host 0.0.0.0 --port 8000
```
Startup prints `AI scoring: ON (via gateway …)`. Then open:
- Player app: `http://localhost:8000/`
- Projector leaderboard: `http://localhost:8000/leaderboard.html`

Without `GATEWAY_URL`/`GATEWAY_TOKEN` the booth runs with **offline scoring**.

---

## 9. HTTP APIs

### Booth (`app.py`)
| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/login` | Upsert a player by name‑derived ID |
| `POST` | `/api/ai_score` | Score a prompt via the gateway (falls back to offline in the browser) |
| `POST` | `/api/score` (`/api/submit_score`) | Record one score per player per game (+ report details) |
| `GET` | `/api/player?san_id=` | Player state: scores, plays, badges, gems |
| `GET` | `/api/leaderboard` | Ranked leaderboard JSON |
| `GET` | `/api/export/leaderboard.csv`, `/api/export/scores.csv` | CSV exports |
| `GET` | `/api/health` | Liveness + `ai_scoring` flag |
| `GET` | any other path | Static file (index.html, leaderboard.html, …) |

### Gateway (`gateway/app.py`)
| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/messages` | Authenticated LLM call (`player_id`, `prompt`, `system`, `max_tokens`) |
| `GET` | `/healthz` | Readiness + key availability (no secrets) |
| `GET` | `/metrics` | Prometheus text exposition |

---

## 10. Data model (SQLite)

`data/prompt_like_a_pro.sqlite3`, created on first run.

**participants** — `san_id` (PK, derived from the player name), `name`, `photo_data`
(optional base64 avatar), `first_seen`, `last_seen`.

**scores** — `id`, `san_id` (FK → participants, `ON DELETE CASCADE`), `game_id`,
`game_name`, `score` (0–100), `badge`, `created_at`, `details` (JSON: the full report
data). Unique on `(san_id, game_id)` — one score per player per game.

---

## 11. Leaderboard

`leaderboard.html` is a standalone projector page (no login). It polls `/api/leaderboard`
every 5 seconds and offers a CSV export. **Ranking:** total score, then number of blocks
stacked **Full**, then **fewest total words** used (tiebreakers). Player names are shown.

**Reset the leaderboard** (e.g., before a live event) — back up first, then clear:
```bash
cp data/prompt_like_a_pro.sqlite3 /tmp/leaderboard-backup.sqlite3
python3 - <<'PY'
import sqlite3
c = sqlite3.connect("data/prompt_like_a_pro.sqlite3")
c.execute("PRAGMA foreign_keys = ON")
c.execute("DELETE FROM scores")
c.execute("DELETE FROM participants")
c.commit(); c.close()
PY
```
The projector page refreshes to an empty board within ~5 seconds.

---

## 12. Deployment

You are deploying **two long‑running processes + one SQLite file + secrets** — not static
hosting. Options:

- **A machine at the venue** — simplest for a physical booth. Gateway on localhost, booth
  on `0.0.0.0:8000`; players open `http://<machine-LAN-IP>:8000`. Needs outbound internet
  for Claude.
- **A small cloud VM** (DigitalOcean / Hetzner / Lightsail / Fly.io) — run both under
  systemd (a unit is in `gateway/README.md`), put Caddy/Cloudflare in front for HTTPS.
- **A PaaS** — Fly.io fits (persistent volume for SQLite, private gateway networking).
  Render/Railway work with a persistent disk. Avoid Cloud Run / Vercel / Netlify
  (serverless + ephemeral disk break SQLite and the always‑on gateway).

**Must‑dos regardless of host:**
- Keep the **gateway private** (localhost or private network); only the booth calls it.
  Run it with **one worker** (the org rate limit is enforced per process).
- Serve the booth over **HTTPS** if it isn't on `localhost` — required both for security
  and because the **camera/photo capture only works in a secure context** (otherwise
  players get initials avatars).
- Keys and tokens in the host's **secret store / env** — never in the repo. `.env` files
  are git‑ignored.
- Run a **single booth instance** (SQLite is a single‑file, single‑writer store).

---

## 13. Security

- **Anthropic keys live only in the gateway** — never in the browser, never in `app.py`,
  never logged. The booth authenticates to the gateway with a per‑system bearer token.
- **Logs are safe** — player IDs are stored/logged only as salted hashes; the gateway logs
  the key *label* (e.g. `active-1`), never the secret, and a redaction filter masks any
  key‑shaped text as defense in depth.
- **Rotate keys** by replacing the values in `gateway/.env` (or your secret store) and
  restarting the gateway — no code change. Rotate any key that has been shared in
  plaintext.

---

## 14. Operations & tuning

- **Model** — `GATEWAY_MODEL` (default `claude-haiku-4-5`, fast + cheap). Set
  `claude-sonnet-4-6` (or another Claude model) for richer output at higher latency/cost;
  restart the gateway after changing.
- **Burst capacity** — raise `GATEWAY_MAX_CONCURRENCY` for larger simultaneous bursts, but
  stay within your Anthropic org rate limit (watch for `429`s in the gateway log).
- **Monitoring** — scrape `gateway :8100/metrics` (requests, successes, retries, cooldowns,
  failovers, tokens, estimated cost, keys available) and probe `/healthz`.
- **Tests** — `.venv-gateway/bin/python -m pytest gateway/tests` covers success, 429 retry,
  retry‑after, 5xx retry, key cooldown, queue overflow, per‑player limits,
  and the guarantee that secrets never appear in logs.

---

## 15. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "AI judge unavailable. Using the built‑in block scanner." | Gateway unreachable/failing, or not configured. Players still get offline scores. Check the gateway is up on :8100 and `booth /api/health` shows `ai_scoring: true`. |
| `ai_scoring: false` at `/api/health` | Booth launched without `GATEWAY_URL`/`GATEWAY_TOKEN`. |
| Gateway `/healthz` shows keys in cooldown / 429s in logs | Hitting the Anthropic org rate limit; lower `GATEWAY_MAX_CONCURRENCY` or raise your org limit. |
| Camera/photo capture not working | The booth isn't on HTTPS or `localhost`; browsers block the camera on plain `http://<ip>`. |
| Slow scoring under load | Expected contention on the shared org limit; each call slows a bit but all run in parallel. Consider a smaller model or higher org limit. |
| Leaderboard shows stale/old players | Reset it (see §11). |
