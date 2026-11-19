"""Find the supported engines already on this computer: Stockfish (required) and Lc0 (optional).

Only these two are supported. Both must report win/draw/loss (UCI_ShowWDL).
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

NAMES = ("stockfish", "lc0")
STOCKFISH, LC0 = "stockfish", "lc0"


def kind_of(name: str) -> Optional[str]:
    """"stockfish" or "lc0" from an engine's `id name`, else None (unsupported)."""
    n = name.lower()
    if "stockfish" in n:
        return STOCKFISH
    if n.startswith("lc0") or "leela" in n:
        return LC0
    return None


@dataclass
class EngineInfo:
    path: str
    name: str  # from the engine's `id name`, e.g. "Stockfish 19"
    found_in: str  # "PATH", "Homebrew", "winget", "downloaded", "custom", …
    kind: str = STOCKFISH  # "stockfish" | "lc0"


def popen_kwargs() -> dict:
    """Keep engine processes from opening a console window on Windows."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


WDL_OPTION = "option name UCI_ShowWDL"


def probe(path: str, timeout: float = 10.0) -> Optional[str]:
    """The engine's `id name` if `path` is Stockfish or Lc0 and reports win/draw/loss, else None."""
    d = probe_details(path, timeout)
    return d[0] if d and d[1] and kind_of(d[0]) else None


def probe_details(path: str, timeout: float = 10.0) -> Optional[tuple[str, bool]]:
    """(`id name`, supports UCI_ShowWDL) if `path` speaks UCI, else None."""
    try:
        p = subprocess.Popen(
            [path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            **popen_kwargs(),
        )
    except OSError:
        return None
    try:
        out, _ = p.communicate("uci\nquit\n", timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        return None
    if "uciok" not in out:
        return None
    wdl = WDL_OPTION in out
    for line in out.splitlines():
        if line.startswith("id name "):
            return line[len("id name ") :].strip(), wdl
    return "UCI engine", wdl


def _candidates(engines_dir: Optional[str]) -> Iterable[tuple[str, str]]:
    exe = ".exe" if sys.platform == "win32" else ""
    if engines_dir and os.path.isdir(engines_dir):
        for p in sorted(glob.glob(os.path.join(engines_dir, "**", f"stockfish*{exe}"), recursive=True), reverse=True):
            if os.path.isfile(p) and (exe or os.access(p, os.X_OK)):
                yield p, "downloaded"
    for name in NAMES:
        p = shutil.which(name)
        if p:
            yield p, "PATH"
    if sys.platform == "darwin":
        for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
            for name in NAMES:
                yield os.path.join(prefix, name), "Homebrew"
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            for p in glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "Stockfish*", "**", "*.exe"), recursive=True):
                yield p, "winget"
            for name in NAMES:
                yield os.path.join(local, "Microsoft", "WinGet", "Links", f"{name}.exe"), "winget"
        for base in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
            if base:
                for p in glob.glob(os.path.join(base, "Stockfish*", "**", "stockfish*.exe"), recursive=True):
                    yield p, "Program Files"
                for p in glob.glob(os.path.join(base, "lc0*", "lc0.exe")):
                    yield p, "Program Files"
    else:
        for d in ("/usr/games", "/usr/local/games", "/snap/bin", os.path.expanduser("~/.local/bin")):
            for name in NAMES:
                yield os.path.join(d, name), "system"


def discover(engines_dir: Optional[str] = None, custom: Optional[str] = None,
             kind: Optional[str] = None) -> list[EngineInfo]:
    """Supported engines (of `kind`, if given), best guess first: a custom path, then downloaded, then installed."""
    seen: set[str] = set()
    found: list[EngineInfo] = []
    pairs = ([(custom, "custom")] if custom else []) + list(_candidates(engines_dir))
    for path, where in pairs:
        if not path or not os.path.isfile(path):
            continue
        real = str(Path(path).resolve())
        if real in seen:
            continue
        seen.add(real)
        name = probe(path)
        k = kind_of(name) if name else None
        if k and (kind is None or k == kind):
            found.append(EngineInfo(path=path, name=name, found_in=where, kind=k))
    found.sort(key=lambda e: (e.found_in != "custom", e.kind != STOCKFISH))
    return found
