Use **Tools → Knightly Review → Settings…** instead of editing this by hand.

- `lichess_user`, `chesscom_user`: whose games to fetch. Either can be empty.
- `stockfish_path` (required), `sf_depth` (fixed search depth; 13 by default, 20+ recommended), `threads` (0 = half the CPU cores), `hash_mb` (Stockfish's hash table in MB of RAM).
- `use_lc0`, `lc0_path`: Lc0 as a second engine. It runs after Stockfish on each position, for as long as Stockfish took there. Its judgement is used except in mate positions, where Stockfish's is. Lc0 only joins in the last step, where a card is made; the depth-by-depth steps before it are Stockfish alone.
- `deck`: where new cards go.
- `update_on_startup`, `update_every_hours` (0 = off): automatic updates while Anki is open.
- `history_days`, `time_classes`, `rated_only`: which games count. `history_days` is how far back to look; `0` (or any negative number) means all time. Raising it later downloads the older games on the next update.
- `analysis_minutes`, `max_new_cards`: how much one update does. An update stops once it has `max_new_cards` new cards or after `analysis_minutes` of engine time (`0` = until it has the cards), and the next update carries on from there.
- How positions are analysed: every position of your games, most-seen first, gets a quick Stockfish look (depth 1). Moves that look like mistakes are then looked at deeper, one depth at a time, dropping those that turn out fine, up to `sf_depth`. Only there does the full setup run (Lc0 when on, every legal move), which makes the card. Other positions are deepened when no possible mistakes are left.
- `note_grouping`: how cards are made and grouped into notes.
  - `"game"` (default): one note per game and phase (opening, middlegame, endgame; decided as Lichess does). Games are taken newest first, and every move of a game that loses at least `min_loss` becomes a card before the next game is looked at.
  - `"ranked"`: one note per game and phase, with mistakes from all games ranked together, biggest first: win rate lost × 1.0 (your move in a game you didn't win), 0.6 (in a game you won) or 0.8 (an opponent's mistake), × 1.5 for each extra game that reached the same position.
  - `"opening"`: one note per opening family, your colour and ply, e.g. *Sicilian Defense (Black), move 5…*. Openings are ranked by the points they cost you over `history_days` (4 per loss, 2 per draw, 1 per win). The most important one is gone through ply by ply from the first move, then the next opening, and so on, `max_new_cards` at a time. Every move that loses more than `opening_min_loss` becomes a card. After each update, the new cards of these notes are moved to the front of the new-card queue in that order. Notes are tagged `knightly::by-opening::<colour>-<opening>::ply-<n>`.
- Tags: every note has `knightly`, and a key tag that finds it again (`knightly::game::<site>-<id>::<phase>`, or `knightly::by-opening::<colour>-<opening>::ply-<n>`). Plus `knightly::phase::opening|middlegame|endgame`, `knightly::site::lichess|chesscom`, `knightly::color::white|black`, `knightly::time::<blitz, rapid…>`, `knightly::result::win|draw|loss`, `knightly::rated` or `knightly::casual`, `knightly::opening::<family>`, and `knightly::severity::blunder|mistake|inaccuracy` (the played move's ??, ? or ?!). A note with several cards or games gets each tag that any of them has. Search with them in the browser, or build filtered decks, e.g. `tag:knightly::phase::endgame`. Updates replace a game note's `knightly::` tags; your own tags are kept.
- `opening_min_loss` (default 2, the same as `good_move`): in opening mode, a move that loses more than this becomes a card, so by default every ?!, ? and ?? move does.
- `include_opponent` (off by default): also make cards from your opponents' mistakes, played from their side. They go in the same deck and notes, tagged `knightly::opponent`.
- `min_loss`: how many points of win rate a move you played must lose to become a card (per-game mode).
- `good_move`: a move within this many points of the best move is a "good move" (accepted as an answer).

Win rate is the engine's own expected score from its win/draw/loss estimate (wins + draws / 2), so the
engines must support `UCI_ShowWDL`. Only Stockfish and Lc0 are supported.

The Lichess login token is kept in `user_files/secrets.json`, not here.
