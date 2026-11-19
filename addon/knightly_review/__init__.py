"""Knightly Review desktop helper.

Lets the card's settings dialog (⚙) save its settings into the note type's
templates, so they sync to every device. The card talks to it with
pycmd('kr:hello') and pycmd('kr:config:{json}').
"""

from __future__ import annotations

import json
from typing import Any

from aqt import gui_hooks, mw

from .config_block import rewrite, validate

VERSION = "0.1.0"
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
    return {"ok": True, "updated": replaced}


def on_js_message(handled: tuple[bool, Any], message: str, context: Any) -> tuple[bool, Any]:
    if not message.startswith(PREFIX):
        return handled
    card = _card(context)
    if card is None:
        return handled
    if message == "kr:hello":
        return True, {"kr": 1, "version": VERSION}
    if message.startswith("kr:config:"):
        try:
            cfg = json.loads(message[len("kr:config:"):])
        except ValueError:
            return True, {"ok": False, "error": "invalid JSON"}
        try:
            return True, _save(card, cfg)
        except Exception as e:  # report to the card instead of an error dialog
            return True, {"ok": False, "error": str(e)}
    return handled


gui_hooks.webview_did_receive_js_message.append(on_js_message)
