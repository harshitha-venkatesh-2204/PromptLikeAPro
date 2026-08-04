# Project Structure

```
app.py            Backend server, active game registry (GAMES dict), SQLite layer, APIs,
                  optional AI scoring proxy
index.html        Player app: login, game grid, 6 reference game renderers,
                  standardized header timer, score reports, profile, live leaderboard tab
run_stack.py      One-command launcher: starts the LLM gateway AND the booth app together
run_windows.bat   Convenience wrapper around run_stack.py (Windows)
run_mac_linux.sh  Convenience wrapper around run_stack.py (macOS / Linux)
gateway/          Internal LLM gateway (FastAPI): key pool, limits, logging, metrics
data/             SQLite database, created at runtime (not committed)
```

## Conventions
- Game logic, scoring weights, reports, animations, and interactions in
  `index.html` should stay aligned with the reference app except for the
  standardized 60-second header timer required by the Main App.
- New or changed active games must be reflected in both the `GAMES` dict in
  `app.py` and the `games` array in `index.html`.
- Do not reintroduce the reference app's in-player leaderboard UI or navigation.
- When removing a feature, remove its CSS, helpers, and handlers too; no dead UI
  code is left behind.
- No em dashes or en dashes in generated user-facing content.
