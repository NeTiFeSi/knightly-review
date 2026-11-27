"""Add-on settings (Anki's add-on config), the local secrets file and data paths."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .vendor.knightly.config import Config, upgrade

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
    return upgrade(_manager().getConfig(ADDON) or {}, defaults)


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
