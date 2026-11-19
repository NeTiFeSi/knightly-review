Use **Tools → Knightly Review → Settings…** instead of editing this by hand.

- `lichess_user`, `chesscom_user`: whose games to fetch. Either can be empty.
- `stockfish_path` (required), `sf_depth` (fixed search depth; 13 by default, 20+ recommended), `threads` (0 = half the CPU cores), `hash_mb` (Stockfish's hash table in MB of RAM).
- `use_lc0`, `lc0_path`, `lc0_time` (seconds per position): Lc0 as a second engine, run in parallel with Stockfish. Its judgement is used except in mate positions, where Stockfish's is.
- `deck`: where new cards go.
- `update_on_startup`, `update_every_hours` (0 = off): automatic updates while Anki is open.
- `history_days`, `time_classes`, `rated_only`: which games count.
- `max_games_per_run`, `max_new_cards`: how much one update does.
- `include_opponent` (off by default): also make cards from your opponents' mistakes, played from their side. They go in the same deck and notes, tagged `knightly::opponent`.
- `min_loss`: how many points of win rate a move you played must lose to become a card.
- `good_move`: a move within this many points of the best move is a "good move" (accepted as an answer).

Win rate is the engine's own expected score from its win/draw/loss estimate (wins + draws / 2), so the
engines must support `UCI_ShowWDL`. Only Stockfish and Lc0 are supported.

The Lichess login token is kept in `user_files/secrets.json`, not here.
