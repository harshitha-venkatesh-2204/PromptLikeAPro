# Technology

- Backend: `app.py`, a single Python 3 standard-library HTTP server
  (`http.server.ThreadingHTTPServer`). No Flask, Streamlit, npm, or build step.
- Database: SQLite, auto-created at `data/prompt_like_a_pro.sqlite3` on first
  run. Never create a second database or a second server process.
- Frontend: one self-contained static HTML file (`index.html`) with inline CSS
  and vanilla JavaScript.
- Optional AI scoring: `POST /api/ai_score` routes prompt scoring through the
  internal LLM gateway (`gateway/`) when `GATEWAY_URL` and `GATEWAY_TOKEN` (or
  `--gateway-url` / `--gateway-token`) are configured. The booth process never
  holds an Anthropic API key; the gateway owns the keys, pooling, and limits.
  Without a gateway configured, the built-in offline scorer is used.
- Fonts load from Google Fonts with system-font fallbacks so the app degrades
  gracefully offline.

## Run
```
python app.py            # defaults to http://127.0.0.1:8000
python app.py --port 8080 --host 0.0.0.0
```
Or use `run_windows.bat` / `run_mac_linux.sh`.

## API surface (do not remove)
- `POST /api/login` - upsert player by derived ID
- `POST /api/score` - record a game score once per player per game, including game details/report data
- `POST /api/ai_score` - optional AI prompt scoring when configured
- `GET  /api/player?san_id=` - player state with plays, badges, tokens, and stored details
- `GET  /api/leaderboard` - ranked leaderboard JSON for the Main App display
- `GET  /api/export/leaderboard.csv` and `/api/export/scores.csv`
- `GET  /api/health`
- Any other GET path is served as a static file from the app directory.
