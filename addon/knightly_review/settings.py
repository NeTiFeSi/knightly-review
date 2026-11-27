"""Add-on settings (Anki's add-on config), the local secrets file and data paths."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .vendor.knightly.config import Config

ADDON_DIR = Path(__file__).resolve().parent
USER_FILES = ADDON_DIR / "user_files"
CACHE_PATH = USER_FILES / "cache.sqlite"
ENGINES_DIR = USER_FILES / "engines"
SECRETS = USER_FILES / "secrets.json"


ADDON = __name__.split(".")[0]  # the add-on's folder name (numeric when installed from AnkiWeb)


def _manager():
    from aqt import mw

    return mw.addonManager


def load() -> dict[str, Any]:
    defaults = json.loads((ADDON_DIR / "config.json").read_text())
    cfg = {**defaults, **(_manager().getConfig(ADDON) or {})}
    if cfg.get("config_version", 1) < 2:
        # Version 2: one way of making puzzles (ply by ply). The old grouping settings are gone, and
        # `min_loss` means something else now (a move losing more than this is a puzzle).
        cfg.pop("note_grouping", None)
        cfg.pop("opening_min_loss", None)
        cfg["min_loss"] = defaults["min_loss"]
    if not cfg.get("accounts"):
        # Version 3: accounts (several per site, yours or other players') instead of one username per site.
        # Checked by the old keys, not the version: a config that never stored its version gets the default's.
        cfg["accounts"] = [
            {"source": site, "name": cfg[f"{site}_user"], "role": "me"}
            for site in ("lichess", "chesscom")
            if cfg.get(f"{site}_user")
        ]
    for old in ("lichess_user", "chesscom_user"):
        cfg.pop(old, None)
    if "keep_running" in cfg:
        # The stages run on their own now: "keep analysing after the update" became annotating in the
        # background (at low priority); cards are only made by the build stage, up to its limit per run.
        if cfg["keep_running"] in ("precalculate", "unlimited"):
            cfg["keep_annotating"] = True
            cfg["keep_annotating_hours"] = float(cfg.get("keep_running_hours") or 0)
        cfg.pop("keep_running", None)
        cfg.pop("keep_running_hours", None)
    cfg["config_version"] = defaults["config_version"]
    if cfg.get("engine_path") and not cfg.get("stockfish_path"):  # settings from an early version
        cfg["stockfish_path"] = cfg["engine_path"]
    return cfg


def save(cfg: dict[str, Any]) -> None:
    _manager().writeConfig(ADDON, cfg)


def secrets() -> dict[str, Any]:
    try:
        return json.loads(SECRETS.read_text())
    except (OSError, ValueError):
        return {}


def save_secrets(data: dict[str, Any]) -> None:
    USER_FILES.mkdir(exist_ok=True)
    tmp = SECRETS.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)  # created afresh below, so it is never readable by others, even for a moment
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        f.write(json.dumps(data))
    tmp.replace(SECRETS)


def core_config(cfg: dict[str, Any] | None = None) -> Config:
    c = Config.from_dict(cfg if cfg is not None else load())
    c.extract.lichess_token = secrets().get("lichess_token", "")
    return c


def configured(cfg: dict[str, Any]) -> bool:
    return Config.from_dict(cfg).extract.has_games() and bool(cfg.get("stockfish_path"))
