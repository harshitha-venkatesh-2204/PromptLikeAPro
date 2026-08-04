# Prompt Like A PRO - Python Booth App

A Python-only local web app for the Prompt Like A PRO event booth.

This package keeps the App_Copy architecture: one Python server, one SQLite database, and a player game page at `/` with the live leaderboard built in as the Leaderboard tab.

## What is included

- Player name login with App_Copy's duplicate-name protection
- Optional camera capture at login, with initials avatar fallback
- Six reference-app games implemented in the player app:
  - Persona Roulette
  - Prompt Detective
  - Beat the Bot
  - Draw With Words
  - Crack the Vault
  - The Mind Reader
- Reference gameplay mechanics are preserved, including rules, tiered game timers (Easy 2 minutes, Medium 3, Hard 4, each with a 1 minute bonus) that start on the player's first keystroke, word limits, rewards/progression, success/failure conditions, animations, game states, and game-specific interactions
- Scoring uses the 6-Block rubric: every prompt is scanned on Role, Task, Context, Audience, Format, and Constraints (Full / Partial / Missing with evidence quotes), plus a game-specific mission layer and a 10-point craft layer, out of 100 per game
- Scores map to Builder ranks (Master Builder 90+, Architect 75+, Builder 60+, Apprentice 40+, Loose Blocks below 40) and reports show the 6-Block Scan, mission and craft rows, and Power Move / Level Up coaching
- Leaderboard ranks by total score, then blocks stacked Full, then fewest total words used
- Play all six games per login; each game can be completed once (scores accumulate into the player total), enforced in the UI, in demo mode, and on the server
- Optional AI scoring through the local Python backend when `ANTHROPIC_API_KEY` is configured; otherwise the built-in offline block scanner grades the same six blocks with keyword heuristics
- One completed attempt per game per player
- SQLite storage for participants, photos, scores, badges, details, and report data
- Main App leaderboard data endpoint and CSV exports retained:
  - `/api/leaderboard`
  - `/api/export/leaderboard.csv`
  - `/api/export/scores.csv`
- Live leaderboard built into the player app as the Leaderboard tab

## Leaderboard handling

The leaderboard lives inside the player app: a Leaderboard tab with a stats strip (players, games completed, top score), a top-3 podium, ranked rows that highlight the logged-in player, a CSV export, and a 5-second live refresh while the tab is open.

Scores, gems, badges, completion state, and detailed report data are saved through the existing backend, which serves the centralized ranking data at `/api/leaderboard`.

## Run locally

```bash
python app.py
```

Open:

```text
http://localhost:8000/
```

The live leaderboard is the Leaderboard tab inside the app.

## Run on a booth/local network

```bash
python app.py --host 0.0.0.0 --port 8000
```

Player systems should open:

```text
http://MAIN-SYSTEM-IP:8000/
```

For a projector/wall display, open the same URL and switch to the Leaderboard tab.

## Optional AI scoring

Set an Anthropic API key before running, or pass it as a command-line argument:

```bash
export ANTHROPIC_API_KEY="your-key-here"
python app.py --host 0.0.0.0 --port 8000
```

or:

```bash
python app.py --api-key "your-key-here"
```

Without a key, the app works fully offline using the built-in scorer.

## Data storage

The app creates this database automatically:

```text
data/prompt_like_a_pro.sqlite3
```

The ZIP does not include old runtime data. If you have an existing database, back it up before replacing files.
