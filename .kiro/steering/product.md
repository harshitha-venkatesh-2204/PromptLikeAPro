# Product Overview

Prompt Like A PRO is an AI Day booth application that teaches practical prompt
engineering through six reference-app mini games. Players log in with their name
(plus an optional camera-captured profile photo), play each game once, earn XP,
gems, badges, and detailed score reports, and a live leaderboard tab inside the
app ranks every player.

## Core rules
- Players can complete all six games; each game allows one retry (two attempts max) and the
  best score is kept. Scores accumulate into the player total.
- Game timing is standardized across all games: the Start Game button begins a
  60-second countdown shown in the game header.
- Prompt-writing games enforce a 100-word prompt limit.
- A stable player ID is derived from the player name. A name that already exists
  in the database cannot log in again from a new session (duplicate-name block).
- Scores, gems, badges, completion state, and score-report details are persisted
  through the existing backend for the Main App leaderboard integration.

## Pages
- `/` - the player app: login, game grid, game screens, profile, and the
  Leaderboard tab (stats strip, top-3 podium, ranked rows, CSV export). The tab
  polls `/api/leaderboard` every 5 seconds while open and highlights the
  logged-in player.

## Leaderboard scope
The leaderboard is built into the player app; there is no separate projector
page. Leaderboard data stays centralized behind `/api/leaderboard`.
