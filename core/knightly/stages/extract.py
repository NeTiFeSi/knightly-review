"""Stage 1, extract: collect games and hand them on.

Input: the accounts (Lichess and Chess.com usernames, names in PGN files), each yours (ME) or another player's
(OTHER), and the PGN inputs. Output: the `games` and `game_players` tables of the cache, and, when an export
folder is set, every account's games as PGN (`export.py`).

`games_of` is what the next stages read: the games of one role, filtered by the look-back and the game filters,
each seen from the side of the player followed (`Game.user_is_white`).
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .. import export, http, sources
from ..config import ME, SITES, Account, Config, Extract
from ..model import Game
from ..sources import chesscom, lichess, pgn

log = logging.getLogger(__name__)

Message = Callable[[str], None]
SITE_NAMES = {"lichess": "Lichess", "chesscom": "Chess.com"}


@dataclass
class Fetched:
    new_games: int = 0
    warnings: list[str] = field(default_factory=list)
    exported: dict[str, int] = field(default_factory=dict)  # file name → games written


def run(
    cfg: Config,
    cache,
    *,
    force: bool = False,
    cancel: threading.Event | None = None,
    message: Message | None = None,
) -> Fetched:
    """Download the accounts' new games (yours first), read the PGN inputs that changed, then export when new
    games arrived (or an account's export file is missing). A site failing is a warning; the others still run."""
    ex = cfg.extract
    msg = message or (lambda m: None)
    out = Fetched()
    accounts = sorted((a for a in ex.accounts if a.source in SITES), key=lambda a: a.role != ME)
    for a in accounts:
        try:
            out.new_games += fetch_account(a, ex, cache, force=force, cancel=cancel, message=msg)
        except http.Cancelled:
            raise
        except Exception as e:  # one account failing shouldn't stop the others
            log.exception("fetching %s's games from %s failed", a.name, a.source)
            out.warnings.append(f"{SITE_NAMES[a.source]} ({a.name}): {e}")
    if ex.pgn_paths:
        try:
            out.new_games += pgn.fetch(cache, ex.pgn_paths, cancel=cancel, progress=msg, skip=ex.export_dir)
        except http.Cancelled:
            raise
        except Exception as e:
            log.exception("reading the PGN inputs failed")
            out.warnings.append(f"PGN files: {e}")
    if ex.export_dir and (out.new_games or _export_missing(ex)):
        try:
            out.exported = export.export_all(cache, ex, ex.export_dir)
        except OSError as e:
            log.exception("exporting the games failed")
            out.warnings.append(f"PGN export: {e}")
    return out


def fetch_account(
    a: Account, ex: Extract, cache, *, force: bool, cancel: threading.Event | None, message: Message
) -> int:
    """Download one site account's new games, unless its cooldown is still on. Returns how many are new."""
    mod = lichess if a.source == "lichess" else chesscom
    # the look-back grew, or the filters let through games they left out
    backfill = mod.needs_backfill(cache, a.name, ex.history_days, ex.time_classes, ex.rated_only)
    # A manual update may always ask Lichess (incremental); Chess.com's cache makes frequent asks pointless,
    # so its cooldown always applies.
    if not sources.due(cache, a.source, a.name) and not (force and a.source == "lichess") and not backfill:
        return 0
    message(f"Fetching {a.name}'s games from {SITE_NAMES[a.source]}…")
    if a.source == "lichess":
        n = lichess.fetch(
            cache,
            a.name,
            token=ex.lichess_token,
            history_days=ex.history_days,
            time_classes=ex.time_classes,
            rated_only=ex.rated_only,
            cancel=cancel,
            progress=message,
        )
    else:
        n = chesscom.fetch(
            cache,
            a.name,
            history_days=ex.history_days,
            time_classes=ex.time_classes,
            rated_only=ex.rated_only,
            cancel=cancel,
            progress=message,
            session=http.session(contact=ex.contact_email),
        )
    sources.mark_fetched(cache, a.source, a.name)
    return n


def _export_missing(ex: Extract) -> bool:
    """An export file these settings call for is missing (a new account, or a file deleted)."""
    folder = Path(ex.export_dir).expanduser()
    return any(not (folder / name).is_file() for name in export.file_names(ex))


def side(game: Game, ex: Extract) -> tuple[bool, str] | None:
    """(plays White, role) of the player this game is followed for: an account of yours if one played it, else
    another player's; None when no account played it. A site's game matches that site's accounts; a PGN game
    matches any account's name."""
    found: tuple[bool, str] | None = None
    for a in ex.accounts:
        if game.source != "pgn" and a.source != game.source:
            continue
        name = a.name.lower()
        for is_white, player in ((True, game.white), (False, game.black)):
            if player.lower() == name and (found is None or (a.role == ME and found[1] != ME)):
                found = (is_white, a.role)
    return found


def passes_filters(game: Game, ex: Extract) -> bool:
    """The game filters, for games from the sites (a PGN header seldom says enough to filter on)."""
    if game.source == "pgn":
        return True
    return (not ex.time_classes or game.time_class in ex.time_classes) and (not ex.rated_only or game.rated)


def games_of(cfg: Config, cache, role: str = ME) -> list[Game]:
    """The games of the accounts of this role within the look-back and the filters, newest first, each seen
    from that player's side. A game between two of your accounts is there once."""
    ex = cfg.extract
    since = sources.since_ts(ex.history_days)
    found: dict[str, Game] = {}
    candidates: list[Game] = []
    for a in ex.accounts:
        if a.source in SITES and a.role == role:
            candidates += cache.games(a.source, a.name, since)
    if any(a.role == role for a in ex.accounts):
        candidates += cache.games_from("pgn", since)
    for g in candidates:
        if g.key in found or not passes_filters(g, ex):
            continue
        s = side(g, ex)
        if s is None or s[1] != role:
            continue
        found[g.key] = g if g.user_is_white == s[0] else dataclasses.replace(g, user_is_white=s[0])
    return sorted(found.values(), key=lambda g: g.end_ts, reverse=True)
