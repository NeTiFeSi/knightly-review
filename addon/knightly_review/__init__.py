"""Knightly Review for Anki desktop.

- Makes cards from your own Lichess/Chess.com games: Tools → Knightly Review
  (runner.py, ui.py; the chess work is the bundled core in vendor/knightly).
- Lets the card's settings dialog (⚙) save its settings into the note type's
  templates, so they sync to every device. The card talks to it with
  pycmd('kr:hello') and pycmd('kr:config:{json}'). It also keeps which replies a card showed
  (pycmd('kr:visits:<card key>|{json}')) in the note's Visits field, so other devices show others; they are
  written when the card is answered, as part of its undo step.
"""

from __future__ import annotations

import json
import logging
from typing import Any

try:
    from aqt import gui_hooks, mw
except ImportError:  # used as a library (card/tools, tests) without the Anki GUI
    gui_hooks = mw = None

from .config_block import rewrite, validate

log = logging.getLogger(__name__)

PREFIX = "kr:"


def _card(context: Any):
    """The card shown by a reviewer or previewer, else None."""
    try:
        from aqt.reviewer import Reviewer

        if isinstance(context, Reviewer):
            return context.card
    except ImportError:
        pass
    try:
        from aqt.browser.previewer import Previewer

        if isinstance(context, Previewer):
            return context.card()
    except ImportError:
        pass
    return None


def _save(card, cfg: dict[str, Any]) -> dict[str, Any]:
    err = validate(cfg)
    if err:
        return {"ok": False, "error": err}
    nt = mw.col.models.get(card.note_type()["id"])
    if not nt:
        return {"ok": False, "error": "note type not found"}
    replaced = 0
    for tmpl in nt["tmpls"]:
        for side in ("qfmt", "afmt"):
            tmpl[side], n = rewrite(tmpl[side], cfg)
            replaced += n
    if not replaced:
        return {"ok": False, "error": "no window.KR_CONFIG block in this note type's templates"}
    mw.col.models.update_dict(nt)
    # The reviewer renders the back from the output cached when the front was shown;
    # re-render so this card's back already uses the new settings.
    try:
        card.question(reload=True)
    except Exception:
        log.exception("could not re-render the card")
    return {"ok": True, "updated": replaced}


def _merge_counts(a: dict, b: dict) -> dict[str, int]:
    """The larger count of each position, so a device never lowers what another saved."""
    return {k: max(int(a.get(k, 0)), int(b.get(k, 0))) for k in set(a) | set(b)}


# Visit counts the card reported, by card id, until the card is answered: {card key: counts}.
_pending_visits: dict[int, dict[str, dict[str, int]]] = {}


def _queue_visits(card: Any, payload: str) -> dict:
    """`payload` is '<card key>|<json counts>'. Kept in memory until the card is answered (`_flush_visits`):
    a write now would be an undo step of its own, and Ctrl+Z would undo it instead of the last answer."""
    key, _, raw = payload.partition("|")
    counts = json.loads(raw)
    if not key or not isinstance(counts, dict):
        return {"ok": False}
    counts = {str(k): int(v) for k, v in counts.items()}
    by_key = _pending_visits.setdefault(card.id, {})
    by_key[key] = _merge_counts(by_key.get(key, {}), counts)
    return {"ok": True}


def _flush_visits(col: Any, card: Any) -> bool:
    """Write the card's queued counts into the note's Visits field ({card key: counts}), right after the card
    was answered, merged into the "Answer Card" undo step: undoing the answer undoes them too, and the undo
    history is otherwise unchanged. True when the note changed. No reviewer refresh (the field is hidden)."""
    pending = _pending_visits.pop(card.id, None)
    note = card.note()
    if not pending or "Visits" not in note:
        return False
    try:
        stored = json.loads(note["Visits"] or "{}")
    except ValueError:
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    changed = False
    for key, counts in pending.items():
        mine = stored.get(key, {})
        merged = _merge_counts(counts, mine)
        if merged != mine:
            stored[key] = merged
            changed = True
    if not changed:
        return False
    status = col.undo_status()
    after_answer = status.undo == col.tr.actions_answer_card()
    note["Visits"] = json.dumps(stored, separators=(",", ":"))
    col.update_note(note)
    if after_answer:  # anything else on top (rare): the write stays a step of its own rather than swallow it
        col.merge_undo_entries(status.last_step)
    return True


def _on_answer(_reviewer: Any, card: Any, _ease: int) -> None:
    try:
        _flush_visits(mw.col, card)
    except Exception:
        log.exception("could not save the visits")


def on_js_message(handled: tuple[bool, Any], message: str, context: Any) -> tuple[bool, Any]:
    if not message.startswith(PREFIX):
        return handled
    card = _card(context)
    if card is None:
        return handled
    if message == "kr:hello":
        from .vendor.knightly import __version__  # the add-on is versioned with the core it bundles

        return True, {"kr": 1, "version": __version__}
    if message.startswith("kr:visits:"):  # which replies this card showed: kept in the note, so it syncs
        try:
            return True, _queue_visits(card, message[len("kr:visits:") :])
        except Exception as e:
            log.exception("could not save the visits")
            return True, {"ok": False, "error": str(e)}
    if message.startswith("kr:config:"):
        try:
            cfg = json.loads(message[len("kr:config:") :])
        except ValueError:
            return True, {"ok": False, "error": "invalid JSON"}
        try:
            return True, _save(card, cfg)
        except Exception as e:  # report to the card instead of an error dialog
            log.exception("could not save the settings")
            return True, {"ok": False, "error": str(e)}
    return handled


if gui_hooks is not None:
    gui_hooks.webview_did_receive_js_message.append(on_js_message)
    gui_hooks.reviewer_did_answer_card.append(_on_answer)


if mw is not None:  # not when imported by tests
    from . import runner, ui

    runner.setup()
    ui.setup_menu()
