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
    if cfg.get("config_version", 1) < defaults.get("config_version", 1):
        # Version 2: one way of making puzzles (ply by ply). The old grouping settings are gone, and
        # `min_loss` means something else now (a move losing more than this is a puzzle).
        cfg.pop("note_grouping", None)
        cfg.pop("opening_min_loss", None)
        cfg["min_loss"] = defaults["min_loss"]
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
    tmp.write_text(json.dumps(data))
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    tmp.replace(SECRETS)


def core_config(cfg: dict[str, Any] | None = None) -> Config:
    c = Config.from_dict(cfg if cfg is not None else load())
    c.lichess_token = secrets().get("lichess_token", "")
    return c


def configured(cfg: dict[str, Any]) -> bool:
    return bool((cfg.get("lichess_user") or cfg.get("chesscom_user")) and cfg.get("stockfish_path"))
