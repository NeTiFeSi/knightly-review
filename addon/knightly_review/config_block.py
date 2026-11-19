"""Validate a card config and rewrite the `window.KR_CONFIG = {...};` block in templates.

Pure Python (no aqt), so it can be tested outside Anki. Mirrors card/src/cardRef.ts.
"""

from __future__ import annotations

import re
from typing import Any

# Key order matches DEFAULT_CONFIG in card/src/cardRef.ts (and the template).
SCHEMA: dict[str, tuple] = {
    "acceptNags": ("nags",),
    "autoReplyDelay": ("num", 0, 10000, False),
    "showEval": ("bool",),
    "evalOnPuzzleFront": ("bool",),
    "intro": ("choice", ("lastMove", "fullGame")),
    "introMoveDelay": ("num", 100, 10000, False),
    "afterAnswer": ("choice", ("flip", "stay", "grade")),
    "flipDelay": ("num", 0, 30000, False),
    "gradeDelay": ("num", 0, 60000, False),
    "gradeSolved": ("num", 1, 4, True),
    "gradeHinted": ("num", 1, 4, True),
    "gradeFailed": ("num", 1, 4, True),
    "hints": ("bool",),
    "live": ("bool",),
    "goodMove": ("num", 0.1, 20, False),
    "liveMaxMoves": ("num", 1, 30, True),
}

BLOCK_RE = re.compile(r"window\.KR_CONFIG\s*=\s*\{[^{}]*\}\s*;?")


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate(cfg: Any) -> str | None:
    """Error message, or None when `cfg` is a complete, valid config."""
    if not isinstance(cfg, dict):
        return "config must be an object"
    for key in cfg:
        if key not in SCHEMA:
            return f"unknown setting {key!r}"
    for key, spec in SCHEMA.items():
        if key not in cfg:
            return f"missing setting {key!r}"
        v = cfg[key]
        kind = spec[0]
        if kind == "bool" and not isinstance(v, bool):
            return f"{key} must be true or false"
        if kind == "choice" and v not in spec[1]:
            return f"{key} must be one of {', '.join(spec[1])}"
        if kind == "num":
            _, lo, hi, integer = spec
            if not _is_num(v) or v != v or not lo <= v <= hi or (integer and v != int(v)):
                return f"{key} must be a number from {lo} to {hi}"
        if kind == "nags" and not (
            isinstance(v, list) and all(_is_num(x) and x == int(x) and x > 0 for x in v)
        ):
            return f"{key} must be a list of NAG numbers"
    return None


def _js(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return "[" + ", ".join(_js(x) for x in v) + "]"
    if isinstance(v, str):
        return "'" + v + "'"
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


def format_block(cfg: dict[str, Any]) -> str:
    """Same text as formatConfig() in card/src/cardRef.ts."""
    lines = [f"  {key}: {_js(cfg[key])}" for key in SCHEMA]
    return "window.KR_CONFIG = {\n" + ",\n".join(lines) + "\n};"


def rewrite(template: str, cfg: dict[str, Any]) -> tuple[str, int]:
    """Replace every config block in `template`. Returns (text, number replaced)."""
    block = format_block(cfg)
    return BLOCK_RE.subn(lambda _m: block, template)
