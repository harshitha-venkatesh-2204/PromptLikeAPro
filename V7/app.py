#!/usr/bin/env python3
"""Prompt Like A PRO booth app.

Run with standard Python only:
  python app.py
  python app.py --host 0.0.0.0 --port 8000

No React, npm, Flask, Streamlit, or external packages are required.

AI scoring (optional): set GATEWAY_URL and GATEWAY_TOKEN (or pass --gateway-url
and --gateway-token) to route prompt judging through the internal LLM gateway
(see gateway/), which holds the Anthropic keys and enforces pooling/limits. The
booth process never holds an Anthropic API key. Without a gateway configured the
app falls back to the built-in keyword scorer and works fully offline.
Participant data, optional photos, and scores are stored in SQLite at:
  data/prompt_like_a_pro.sqlite3
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import mimetypes
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "prompt_like_a_pro.sqlite3"
SAN_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
PHOTO_RE = re.compile(r"^data:image/(jpeg|jpg|png|webp);base64,[A-Za-z0-9+/=\r\n]+$")
MAX_PHOTO_CHARS = 450_000
MAX_JSON_BYTES = 1024 * 1024
DB_LOCK = threading.Lock()

# AI scoring is routed through the internal LLM gateway (see gateway/), never to
# Anthropic directly. The booth process holds only a gateway server token, not an
# Anthropic API key; the keys live solely inside the gateway. Configure with
# GATEWAY_URL and GATEWAY_TOKEN (env) or --gateway-url / --gateway-token. The
# model is chosen by the gateway (GATEWAY_MODEL), so the booth no longer sets it.
GATEWAY_URL = ""
GATEWAY_TOKEN = ""
GATEWAY_MESSAGES_PATH = "/v1/messages"
AI_TIMEOUT_SECONDS = 60
AI_MAX_PROMPT_CHARS = 4000
AI_MAX_TOKENS = 1500

# Attempts allowed per game per player: the first play plus one retry. The best
# score across attempts is kept.
MAX_ATTEMPTS_PER_GAME = 2

AI_JUDGE_SYSTEM = (
    "You are the judge of Prompt Like A PRO, a prompt-writing arcade game built on the 6-Block rubric. "
    "A great prompt stacks six blocks: Role (who the AI should be), Task (one clear ask with an action verb), "
    "Context (background facts), Audience (who the output is for), Format (exact structure and length), "
    "Constraints (rules: tone, musts, nevers, verification). "
    "Grade EACH block on the player's prompt as full, partial, or missing. "
    "full = a specific, usable instruction for that block. partial = a vague gesture at it. missing = absent. "
    "evidence must be a verbatim quote from the player's prompt, at most 12 words, that IS an instruction, not a keyword drop. Empty string when missing. "
    "Anti-stuffing rule: listing block keywords without wiring them into instructions earns missing on every block. "
    "Floor rule: near-empty, off-topic, or gibberish prompts earn missing on every block. "
    "craft: economy is full when the prompt does its job with no filler (partial if padded or very thin, missing if mostly filler); "
    "coherence is full for one clear ask with no self-contradictions (partial if instructions partially conflict, missing if the prompt fights itself). "
    "mission: an object whose shape is defined by the mission-specific judging instructions in the user message; return {} if none are given. "
    "feedback: at most 2 sentences, playful but specific. "
    "power_move: one sentence naming the best thing the player did. "
    "level_up: one sentence naming the single highest-value fix, phrased as a block to add or strengthen. "
    "your_output must BE the actual output the player's prompt would generate: follow it faithfully, flaws included, up to 120 words. If the prompt requests an image, describe the exact image. If empty or incoherent, write the generic filler an AI would produce. "
    "ideal_prompt is a strong example prompt for this mission in under 80 words. "
    "ideal_output must BE the actual output the ideal_prompt would generate: follow that ideal prompt faithfully, up to 120 words, in the same medium as your_output (if it asks for an image, describe the exact image). "
    "comparison is 2 or 3 sentences that JUSTIFY the score by tying the outputs to prompt quality: first name one real strength of the player's prompt, then explain why the ideal prompt's output better fits THIS mission and audience (on-brief length, tone, examples, format), framed as control and reliability rather than personal taste, even when the player's output is longer or flashier. "
    "ideal_edge is an array of 2 to 4 short strings, each under 22 words, each naming one concrete way the ideal prompt engineers a better on-brief output than the player's, tied to a specific block or constraint it added (a length cap, a named audience, an example style, a tone rule, or an exact format). Be specific to this prompt and mission, not generic. "
    "Never use em dashes or en dashes anywhere. "
    "Respond with ONLY a JSON object, no markdown fences, no preamble, exactly this shape: "
    '{"blocks": {"Role": {"grade": "", "evidence": ""}, "Task": {"grade": "", "evidence": ""}, '
    '"Context": {"grade": "", "evidence": ""}, "Audience": {"grade": "", "evidence": ""}, '
    '"Format": {"grade": "", "evidence": ""}, "Constraints": {"grade": "", "evidence": ""}}, '
    '"craft": {"economy": "", "coherence": ""}, "mission": {}, '
    '"feedback": "", "power_move": "", "level_up": "", "your_output": "", "ideal_prompt": "", "ideal_output": "", "ideal_edge": [], "comparison": ""}'
)

GAMES: dict[str, dict[str, Any]] = {
    "personas": {"name": "Persona Roulette", "badge": "Roulette Royalty", "tokens": 20},
    "missing": {"name": "Prompt Detective", "badge": "Gap Hunter", "tokens": 20},
    "showdown": {"name": "Beat the Bot", "badge": "Bot Slayer", "tokens": 20},
    "scene": {"name": "Draw With Words", "badge": "Word Painter", "tokens": 20},
    "puzzle": {"name": "Crack the Vault", "badge": "Vault Cracker", "tokens": 20},
    "mindreader": {"name": "The Mind Reader", "badge": "Prompt Psychic", "tokens": 20},
}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with DB_LOCK:
        with get_db() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS participants (
                    san_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    photo_data TEXT,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    san_id TEXT NOT NULL,
                    game_id TEXT NOT NULL,
                    game_name TEXT NOT NULL,
                    score INTEGER NOT NULL CHECK(score >= 0 AND score <= 100),
                    badge TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(san_id, game_id),
                    FOREIGN KEY(san_id) REFERENCES participants(san_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_scores_san ON scores(san_id);
                CREATE INDEX IF NOT EXISTS idx_scores_game ON scores(game_id);
                """
            )
            cols = {row[1] for row in conn.execute("PRAGMA table_info(participants)").fetchall()}
            if "photo_data" not in cols:
                conn.execute("ALTER TABLE participants ADD COLUMN photo_data TEXT")
            score_cols = {row[1] for row in conn.execute("PRAGMA table_info(scores)").fetchall()}
            if "details" not in score_cols:
                conn.execute("ALTER TABLE scores ADD COLUMN details TEXT")
            if "attempts" not in score_cols:
                conn.execute("ALTER TABLE scores ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1")


def clean_san(value: Any) -> str:
    san = str(value or "").strip().upper()
    if not SAN_RE.match(san):
        raise ValueError("Player ID must be 3-32 characters using letters, numbers, underscore, dot, or hyphen.")
    return san


def clean_name(value: Any) -> str:
    name = " ".join(str(value or "").strip().split())
    if len(name) < 2:
        raise ValueError("Please enter player name.")
    if len(name) > 60:
        raise ValueError("Player name is too long.")
    return name


def clean_photo(value: Any) -> str:
    photo = str(value or "").strip()
    if not photo:
        return ""
    if len(photo) > MAX_PHOTO_CHARS:
        raise ValueError("Photo is too large. Please capture or upload a smaller image.")
    if not PHOTO_RE.match(photo):
        raise ValueError("Photo format is not supported.")
    return photo.replace("\n", "").replace("\r", "")


def badge_list(scores: list[dict[str, Any]]) -> list[str]:
    badges: list[str] = []
    for row in scores:
        badge = row.get("badge")
        if badge and badge not in badges:
            badges.append(str(badge))
    if len(scores) == len(GAMES) and "PRO Champion" not in badges:
        badges.append("PRO Champion")
    return badges


def tokens_for(game_id: str) -> int:
    return int(GAMES.get(game_id, {}).get("tokens", 0))


def active_game_ids() -> tuple[str, ...]:
    return tuple(GAMES.keys())


def active_score_filter(alias: str = "s") -> tuple[str, tuple[Any, ...]]:
    conditions: list[str] = []
    params: list[Any] = []
    for game_id, meta in GAMES.items():
        conditions.append(f"({alias}.game_id=? AND {alias}.game_name=?)")
        params.extend([game_id, meta["name"]])
    return " OR ".join(conditions), tuple(params)


def player_state(san_id: str) -> dict[str, Any]:
    with get_db() as conn:
        p = conn.execute(
            "SELECT san_id, name, photo_data, first_seen, last_seen FROM participants WHERE san_id=?",
            (san_id,),
        ).fetchone()
        if not p:
            raise KeyError("Player not found.")
        score_filter, score_params = active_score_filter("s")
        rows = conn.execute(
            f"SELECT s.game_id, s.game_name, s.score, s.badge, s.created_at, s.details, s.attempts FROM scores s WHERE s.san_id=? AND ({score_filter}) ORDER BY s.created_at",
            (san_id, *score_params),
        ).fetchall()
    scores = []
    for r in rows:
        row = dict(r)
        try:
            row["details"] = json.loads(row.get("details") or "{}")
        except (TypeError, ValueError):
            row["details"] = {}
        scores.append(row)
    total = sum(int(r["score"]) for r in scores)
    best = max([int(r["score"]) for r in scores] or [0])
    played = {r["game_id"]: {**dict(r), "tokens": tokens_for(r["game_id"])} for r in scores}
    tokens = sum(tokens_for(r["game_id"]) for r in scores)
    return {
        "san_id": p["san_id"],
        "name": p["name"],
        "photo_data": p["photo_data"] or "",
        "total_score": total,
        "best_score": best,
        "tokens": tokens,
        "games_completed": len(scores),
        "played": played,
        "plays": played,
        "badges": badge_list(scores),
    }


def leaderboard(limit: int = 500, photos_top: int = 15) -> list[dict[str, Any]]:
    """photos_top: photo_data is included only for the first N rows (the projector
    shows 15). Everything else gets '' so payloads stay small as players pile up:
    with 200 photographed players the full-photo payload is ~27 MB per response,
    which would saturate booth Wi-Fi on every submit and projector poll."""
    score_filter, score_params = active_score_filter("s")
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT
                p.san_id,
                p.name,
                p.photo_data,
                COALESCE(SUM(s.score), 0) AS total_score,
                COALESCE(MAX(s.score), 0) AS best_score,
                COUNT(s.id) AS games_completed,
                COALESCE(SUM(CASE WHEN json_valid(s.details) THEN CAST(COALESCE(json_extract(s.details, '$.blocks_full'), 0) AS INTEGER) ELSE 0 END), 0) AS blocks_stacked,
                COALESCE(SUM(CASE WHEN json_valid(s.details) THEN CAST(COALESCE(json_extract(s.details, '$.words'), 0) AS INTEGER) ELSE 0 END), 0) AS words_used,
                COALESCE(SUM(
                    CASE
                        WHEN s.id IS NULL THEN 0
                        WHEN json_valid(s.details) THEN MIN(120, MAX(0, CAST(COALESCE(json_extract(s.details, '$.time_seconds'), 120) AS INTEGER)))
                        ELSE 120
                    END
                ), 0) AS total_time,
                GROUP_CONCAT(s.badge, ', ') AS badges,
                GROUP_CONCAT(s.game_id, ',') AS game_ids,
                MIN(s.created_at) AS first_score_at
            FROM participants p
            LEFT JOIN scores s ON s.san_id = p.san_id AND ({score_filter})
            GROUP BY p.san_id, p.name, p.photo_data
            HAVING games_completed > 0
            ORDER BY total_score DESC, total_time ASC, blocks_stacked DESC, words_used ASC, first_score_at ASC
            LIMIT ?
            """,
            (*score_params, limit),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        game_ids = [x.strip() for x in (row["game_ids"] or "").split(",") if x.strip()]
        game_count = int(row["games_completed"] or 0)
        badges = [x.strip() for x in (row["badges"] or "").split(",") if x.strip()]
        if game_count == len(GAMES) and "PRO Champion" not in badges:
            badges.append("PRO Champion")
        result.append(
            {
                "rank": idx,
                "san_id": row["san_id"],
                "name": row["name"],
                "photo_data": (row["photo_data"] or "") if idx <= photos_top else "",
                "total_score": int(row["total_score"] or 0),
                "best_score": int(row["best_score"] or 0),
                "blocks_stacked": int(row["blocks_stacked"] or 0),
                "words_used": int(row["words_used"] or 0),
                "total_time": int(row["total_time"] or 0),
                "tokens": sum(tokens_for(g) for g in game_ids),
                "games_completed": game_count,
                "badges": badges,
            }
        )
    return result


def ai_enabled() -> bool:
    return bool(GATEWAY_URL and GATEWAY_TOKEN)


def clamp_score(value: Any) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


def parse_ai_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.S).strip()
    data = json.loads(cleaned)
    grades = {"full", "partial", "missing"}
    blocks_in = data.get("blocks") or {}
    blocks: dict[str, Any] = {}
    for b in ("Role", "Task", "Context", "Audience", "Format", "Constraints"):
        entry = blocks_in.get(b) or {}
        g = str(entry.get("grade") or "missing").strip().lower()
        blocks[b] = {
            "grade": g if g in grades else "missing",
            "evidence": str(entry.get("evidence") or "").strip()[:120],
        }
    craft_in = data.get("craft") or {}

    def craft_grade(key: str) -> str:
        g = str(craft_in.get(key) or "missing").strip().lower()
        return g if g in grades else "missing"

    mission_in = data.get("mission") if isinstance(data.get("mission"), dict) else {}
    mission: dict[str, Any] = {}
    for k, v in list(mission_in.items())[:8]:
        key = str(k).strip()[:40]
        if isinstance(v, list):
            mission[key] = [str(x).strip()[:80] for x in v if str(x).strip()][:8]
        else:
            mission[key] = str(v).strip()[:80]
    return {
        "blocks": blocks,
        "craft": {"economy": craft_grade("economy"), "coherence": craft_grade("coherence")},
        "mission": mission,
        "feedback": str(data.get("feedback") or "Scored by Claude.").strip()[:400],
        "power_move": str(data.get("power_move") or "").strip()[:300],
        "level_up": str(data.get("level_up") or "").strip()[:300],
        "your_output": str(data.get("your_output") or "").strip()[:1500],
        "ideal_prompt": str(data.get("ideal_prompt") or "").strip()[:1500],
        "ideal_output": str(data.get("ideal_output") or "").strip()[:1500],
        "ideal_edge": [str(x).strip()[:180] for x in (data.get("ideal_edge") or []) if str(x).strip()][:4] if isinstance(data.get("ideal_edge"), list) else [],
        "comparison": str(data.get("comparison") or "").strip()[:600],
    }


def ai_score_prompt(prompt_text: str, mission: str, game_name: str, extra: dict[str, Any] | None = None, player_id: str = "") -> dict[str, Any]:
    user_content = (
        f"Game: {game_name}\n"
        f"Mission given to the player: {mission}\n"
        + (
            f"Mission-specific judging instructions: {extra.get('note')}\n"
            if extra and extra.get("note")
            else ""
        )
        + (
            f"Mission data: {json.dumps(extra.get('data'), ensure_ascii=False)[:1200]}\n"
            if extra and isinstance(extra.get("data"), dict) and extra.get("data")
            else ""
        )
        + f"Player's prompt to judge:\n<player_prompt>\n{prompt_text}\n</player_prompt>"
    )
    # Call the internal gateway, which authenticates this booth (bearer token),
    # selects a pooled Anthropic key, and enforces retries/limits. The Anthropic
    # key never enters this process. player_id drives the gateway's per-player
    # protections, so pass the real player so limits are scoped correctly.
    body = {
        "player_id": player_id or "booth-anon",
        "system": AI_JUDGE_SYSTEM,
        "prompt": user_content,
        "max_tokens": AI_MAX_TOKENS,
    }
    req = urllib.request.Request(
        GATEWAY_URL.rstrip("/") + GATEWAY_MESSAGES_PATH,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "authorization": "Bearer " + GATEWAY_TOKEN,
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=AI_TIMEOUT_SECONDS) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = str(data.get("text") or "")
    return parse_ai_json(text)


def read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length > MAX_JSON_BYTES:
        raise ValueError("Request too large.")
    raw = handler.rfile.read(length) if length else b"{}"
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


class AppHandler(BaseHTTPRequestHandler):
    server_version = "PromptLikeAPro/4.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[Prompt Like A PRO] " + (fmt % args))

    def send_json(self, payload: dict[str, Any] | list[Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/health":
                self.send_json({"ok": True, "data_file": str(DB_PATH), "games": GAMES, "ai_scoring": ai_enabled()})
                return
            if path == "/api/leaderboard":
                self.send_json({"ok": True, "leaderboard": leaderboard()})
                return
            if path == "/api/player":
                san_id = clean_san(qs.get("san_id", [""])[0])
                state = player_state(san_id)
                self.send_json({"ok": True, "player": state, "participant": state})
                return
            if path == "/api/export/leaderboard.csv":
                self.send_leaderboard_csv()
                return
            if path == "/api/export/scores.csv":
                self.send_scores_csv()
                return
            self.send_static(path)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc), "message": str(exc)}, 400)
        except KeyError as exc:
            self.send_json({"ok": False, "error": str(exc), "message": str(exc)}, 404)
        except Exception as exc:
            self.send_json({"ok": False, "error": "Server error: " + str(exc), "message": "Server error: " + str(exc)}, 500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            payload = read_json(self)
            if path == "/api/login":
                san_id = clean_san(payload.get("san_id"))
                name = clean_name(payload.get("name"))
                photo_data = clean_photo(payload.get("photo_data"))
                ts = now_iso()
                with DB_LOCK:
                    with get_db() as conn:
                        conn.execute(
                            """
                            INSERT INTO participants(san_id, name, photo_data, first_seen, last_seen)
                            VALUES(?, ?, ?, ?, ?)
                            ON CONFLICT(san_id) DO UPDATE SET
                                name=excluded.name,
                                photo_data=CASE
                                    WHEN excluded.photo_data IS NOT NULL AND excluded.photo_data <> '' THEN excluded.photo_data
                                    ELSE participants.photo_data
                                END,
                                last_seen=excluded.last_seen
                            """,
                            (san_id, name, photo_data, ts, ts),
                        )
                state = player_state(san_id)
                self.send_json({"ok": True, "player": state, "participant": state, "leaderboard": leaderboard(photos_top=0), "games": GAMES, "ai_scoring": ai_enabled()})
                return
            if path == "/api/ai_score":
                if not ai_enabled():
                    self.send_json({"ok": False, "code": "ai_disabled", "error": "AI scoring is not configured.", "message": "AI scoring is not configured."}, 503)
                    return
                san_id = clean_san(payload.get("san_id"))
                prompt_text = str(payload.get("prompt_text") or "").strip()[:AI_MAX_PROMPT_CHARS]
                mission = str(payload.get("mission") or "").strip()[:600]
                game_id = str(payload.get("game_id") or "").strip()
                game_name = str(GAMES.get(game_id, {}).get("name") or "Prompt game")
                extra_in = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
                extra = {
                    "note": str(extra_in.get("note") or "").strip()[:800],
                    "data": extra_in.get("data") if isinstance(extra_in.get("data"), dict) else {},
                }
                if not prompt_text:
                    raise ValueError("Nothing to score yet. Write a prompt first.")
                try:
                    result = ai_score_prompt(prompt_text, mission, game_name, extra, player_id=san_id)
                except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, KeyError, OSError) as exc:
                    print("[Prompt Like A PRO] AI scoring via gateway failed: " + str(exc))
                    self.send_json({"ok": False, "code": "ai_error", "error": "AI scoring is unavailable right now.", "message": "AI scoring is unavailable right now."}, 502)
                    return
                self.send_json({"ok": True, "result": result, "ai": True})
                return
            if path == "/api/reset":
                with DB_LOCK:
                    with get_db() as conn:
                        conn.execute("DELETE FROM scores")
                        conn.execute("DELETE FROM participants")
                print("[Prompt Like A PRO] Leaderboard reset: all players and scores cleared.")
                self.send_json({"ok": True, "leaderboard": []})
                return
            if path in ("/api/submit_score", "/api/score"):
                san_id = clean_san(payload.get("san_id"))
                game_id = str(payload.get("game_id") or "").strip()
                if game_id not in GAMES:
                    raise ValueError("Unknown game.")
                score = int(round(float(payload.get("score"))))
                if score < 0 or score > 100:
                    raise ValueError("Score must be from 0 to 100.")
                badge = str(payload.get("badge") or GAMES[game_id]["badge"]).strip()[:60]
                details_raw = payload.get("details")
                details_json = "{}"
                if isinstance(details_raw, dict):
                    candidate = json.dumps(details_raw, ensure_ascii=False)
                    if len(candidate) <= 30000:
                        details_json = candidate
                ts = now_iso()
                # Each game allows an initial attempt plus one retry (2 total). The
                # player's best score is kept; a retry that scores lower just uses up
                # the retry. MAX_ATTEMPTS_PER_GAME can be bumped to allow more.
                with DB_LOCK:
                    with get_db() as conn:
                        if not conn.execute("SELECT 1 FROM participants WHERE san_id=?", (san_id,)).fetchone():
                            raise KeyError("Player not found. Please log in again.")
                        row = conn.execute(
                            "SELECT id, score, attempts, details, badge FROM scores WHERE san_id=? AND game_id=?",
                            (san_id, game_id),
                        ).fetchone()
                        if row is None:
                            conn.execute(
                                """
                                INSERT INTO scores(san_id, game_id, game_name, score, badge, created_at, details, attempts)
                                VALUES(?, ?, ?, ?, ?, ?, ?, 1)
                                """,
                                (san_id, game_id, GAMES[game_id]["name"], score, badge, ts, details_json),
                            )
                        elif int(row["attempts"] or 1) >= MAX_ATTEMPTS_PER_GAME:
                            state = player_state(san_id)
                            self.send_json(
                                {
                                    "ok": False,
                                    "error": "No retries left for this game.",
                                    "message": "No retries left for this game.",
                                    "code": "no_retries",
                                    "player": state,
                                    "participant": state,
                                    "leaderboard": leaderboard(photos_top=0),
                                },
                                409,
                            )
                            return
                        else:
                            # Retry keep-rule mirrors the leaderboard tie-break:
                            # a higher score always wins; an equal score keeps
                            # whichever run was faster (time_seconds, default 120).
                            def _run_time(raw: Any) -> int:
                                try:
                                    parsed = raw if isinstance(raw, dict) else json.loads(raw or "{}")
                                    return max(0, min(120, int(parsed.get("time_seconds", 120))))
                                except (TypeError, ValueError, AttributeError):
                                    return 120

                            old_score = int(row["score"] or 0)
                            new_wins = score > old_score or (
                                score == old_score and _run_time(details_raw) < _run_time(row["details"])
                            )
                            if new_wins:
                                conn.execute(
                                    "UPDATE scores SET score=?, badge=?, created_at=?, details=?, attempts=attempts+1 WHERE id=?",
                                    (score, badge, ts, details_json, row["id"]),
                                )
                            else:
                                # Retry didn't improve: keep the previous best, just consume the retry.
                                conn.execute(
                                    "UPDATE scores SET attempts=attempts+1 WHERE id=?",
                                    (row["id"],),
                                )
                state = player_state(san_id)
                self.send_json({"ok": True, "player": state, "participant": state, "leaderboard": leaderboard(photos_top=0)})
                return
            self.send_json({"ok": False, "error": "Not found", "message": "Not found"}, 404)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc), "message": str(exc)}, 400)
        except KeyError as exc:
            self.send_json({"ok": False, "error": str(exc), "message": str(exc)}, 404)
        except Exception as exc:
            self.send_json({"ok": False, "error": "Server error: " + str(exc), "message": "Server error: " + str(exc)}, 500)

    def send_static(self, path: str) -> None:
        if path in ("/", ""):
            target = APP_DIR / "index.html"
        else:
            target = (APP_DIR / path.lstrip("/")).resolve()
            if not str(target).startswith(str(APP_DIR.resolve())):
                self.send_text("Forbidden", 403)
                return
        if not target.exists() or not target.is_file():
            self.send_text("Not found", 404)
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.end_headers()
        self.wfile.write(data)

    def send_leaderboard_csv(self) -> None:
        import io
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["Rank", "Name", "Player ID", "Total Score", "Total Time (s)", "Games Completed", "Best Score", "Tokens", "Badges"])
        for row in leaderboard(500):
            writer.writerow([row["rank"], row["name"], row["san_id"], row["total_score"], row["total_time"], row["games_completed"], row["best_score"], row["tokens"], "; ".join(row["badges"])])
        self.send_text(out.getvalue(), 200, "text/csv; charset=utf-8")

    def send_scores_csv(self) -> None:
        import io
        score_filter, score_params = active_score_filter("s")
        with get_db() as conn:
            rows = conn.execute(
                f"""
                SELECT p.san_id, p.name, s.game_id, s.game_name, s.score, s.badge, s.created_at,
                       CASE
                           WHEN json_valid(s.details) THEN MIN(120, MAX(0, CAST(COALESCE(json_extract(s.details, '$.time_seconds'), 120) AS INTEGER)))
                           ELSE 120
                       END AS time_seconds
                FROM scores s JOIN participants p ON p.san_id = s.san_id
                WHERE ({score_filter})
                ORDER BY s.created_at ASC
                """,
                score_params,
            ).fetchall()
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["Player ID", "Name", "Game ID", "Game", "Score", "Time (s)", "Badge", "Submitted At"])
        for row in rows:
            writer.writerow([row["san_id"], row["name"], row["game_id"], row["game_name"], row["score"], row["time_seconds"], row["badge"], row["created_at"]])
        self.send_text(out.getvalue(), 200, "text/csv; charset=utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Prompt Like A PRO locally")
    parser.add_argument("--host", default="127.0.0.1", help="Use 0.0.0.0 to share on your LAN")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--open", action="store_true", help="Open browser automatically")
    parser.add_argument("--gateway-url", default="", help="Internal LLM gateway base URL for AI scoring, e.g. http://127.0.0.1:8100 (or set GATEWAY_URL)")
    parser.add_argument("--gateway-token", default="", help="Gateway server token for this booth (or set GATEWAY_TOKEN). The booth never holds an Anthropic key.")
    args = parser.parse_args()
    global GATEWAY_URL, GATEWAY_TOKEN
    GATEWAY_URL = (args.gateway_url or os.environ.get("GATEWAY_URL", "")).strip()
    GATEWAY_TOKEN = (args.gateway_token or os.environ.get("GATEWAY_TOKEN", "")).strip()
    os.chdir(APP_DIR)
    init_db()
    url = f"http://{args.host}:{args.port}"
    print("Prompt Like A PRO is running")
    print("Open: " + url)
    print("Data file: " + str(DB_PATH))
    print("AI scoring: " + ("ON (via gateway " + GATEWAY_URL + ")" if ai_enabled() else "OFF (set GATEWAY_URL and GATEWAY_TOKEN to enable)"))
    print("Press Ctrl+C to stop")
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
