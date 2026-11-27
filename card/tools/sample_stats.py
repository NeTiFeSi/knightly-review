#!/usr/bin/env python3
"""Make the Notes field of the sample deck: the explorer-style stats of each sample card's position.

The numbers are made up (a few invented games that continue the sample games' positions), only so the dev harness,
the sample deck and the README pictures show what the stats look like. They are not anyone's real games.
The HTML comes from the add-on's own code (`knightly.statsview`), so it is what a real note holds.

    python3 tools/sample_stats.py        # rewrites dev/samples-notes.html

Needs the core installed (`pip install -e ../core`).
"""

from __future__ import annotations

import io
from pathlib import Path

import chess
import chess.pgn
from knightly import statsview
from knightly.cache import fen_key
from knightly.model import Game
from knightly.stats import collect_positions

ROOT = Path(__file__).resolve().parents[1]
KR_TAG = "[%kr "

# card number → the other moves "played" from that card's position: (SAN, White wins, draws, Black wins)
OTHERS: dict[int, list[tuple[str, int, int, int]]] = {
    2: [  # 1.e4 e5 2.Nf3 d6 3.Bc4: the position before the sample's Bg4?!
        ("Nf6", 6, 3, 5),
        ("Be7", 3, 1, 2),
        ("h6", 1, 1, 1),
        ("c6", 1, 0, 1),
        ("Nc6", 1, 0, 1),
        ("Bd7", 0, 1, 1),
        ("g6", 1, 0, 0),
        ("Be6", 0, 0, 1),
    ],
    1: [("dxe5", 2, 1, 1), ("Be6", 1, 0, 0)],  # after 5.Nxe5: instead of the sample's Bxd1??
    3: [("g6", 1, 1, 1), ("Qe7", 1, 0, 1), ("Nh6", 0, 0, 1)],  # after 2.Qh5 Nc6 3.Bc4: instead of Nf6??
    4: [("Nxd4", 3, 1, 2), ("c3", 1, 1, 0), ("O-O", 1, 0, 1), ("h3", 0, 0, 1)],  # instead of Nxe5??
}


def _game(gid: str, moves: list[str], result: str) -> Game:
    return Game(
        source="sample",
        id=gid,
        url="",
        white="",
        black="",
        user_is_white=True,
        end_ts=0,
        result=result,
        moves=moves,
    )


def build() -> str:
    pgn_text = (ROOT / "dev" / "samples.pgn").read_text()
    stream = io.StringIO(pgn_text)
    games: list[Game] = []
    cards: dict[int, tuple[chess.Board, str, dict[str, int]]] = {}  # n → (board, mistake UCI, UCI → NAG)
    gi = 0
    while (g := chess.pgn.read_game(stream)) is not None:
        gi += 1
        moves = [n.move.uci() for n in g.mainline()]
        games.append(_game(f"sample{gi}", moves, g.headers.get("Result", "*")))
        for node in g.mainline():
            comment = node.comment
            if KR_TAG not in comment:
                continue
            n = int(comment.split(KR_TAG)[1].split("]")[0].split()[0])
            parent = node.parent
            nags = {}
            for sib in parent.variations:  # the main move and its alternatives, each with its NAG
                nag = next(iter(sib.nags), 0)
                if nag:
                    nags[sib.move.uci()] = nag
            cards[n] = (parent.board(), node.move.uci(), nags)
    for n, (board, _, _) in cards.items():
        prefix = [m.uci() for m in board.move_stack]
        for san, w, d, b in OTHERS.get(n, []):
            uci = board.parse_san(san).uci()
            for k, result in enumerate(["1-0"] * w + ["1/2-1/2"] * d + ["0-1"] * b):
                games.append(_game(f"card{n}-{san}-{k}", prefix + [uci], result))
    by_fen = collect_positions(games)
    out = []
    for n in sorted(cards):
        board, mistake, nags = cards[n]
        out.append(statsview.section(n, by_fen.get(fen_key(board.fen())), nags=nags, mistake=mistake))
    return "".join(out)


def main() -> None:
    html = build()
    (ROOT / "dev" / "samples-notes.html").write_text(html + "\n")
    print("wrote dev/samples-notes.html", len(html), "bytes")


if __name__ == "__main__":
    main()
