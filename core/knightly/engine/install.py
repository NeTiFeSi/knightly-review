"""Getting Stockfish onto the computer: the OS package manager, or the official download.

System package managers on Linux need root, so for those we only show the
command for the user to run. Homebrew and winget run without admin rights, so
those can be run for the user. The download path works everywhere: the
official release build goes into the add-on's own folder.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .. import http
from .discover import popen_kwargs, probe

RELEASES = "https://api.github.com/repos/official-stockfish/Stockfish/releases/latest"


@dataclass
class InstallOption:
    manager: str  # "yay", "paru", "apt", "dnf", "zypper", "brew", "winget"
    command: str  # what to show / copy
    argv: Optional[list[str]] = None  # set when we can run it for the user (no admin rights needed)


def _os_release() -> dict[str, str]:
    try:
        text = Path("/etc/os-release").read_text()
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v.strip().strip('"')
    return out


def package_options() -> list[InstallOption]:
    """How this OS's package manager installs Stockfish, if we know one."""
    if sys.platform == "win32":
        if shutil.which("winget"):
            argv = ["winget", "install", "--id", "Stockfish.Stockfish", "-e",
                    "--accept-source-agreements", "--accept-package-agreements"]
            return [InstallOption("winget", "winget install --id Stockfish.Stockfish -e", argv)]
        return []
    if sys.platform == "darwin":
        brew = shutil.which("brew") or next(
            (p for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if os.path.isfile(p)), None
        )
        if brew:
            return [InstallOption("brew", "brew install stockfish", [brew, "install", "stockfish"])]
        return []
    ids = (_os_release().get("ID", "") + " " + _os_release().get("ID_LIKE", "")).split()
    table = [
        ({"debian", "ubuntu", "linuxmint", "pop"}, "apt", "sudo apt install stockfish"),
        ({"fedora"}, "dnf", "sudo dnf install stockfish"),
        ({"opensuse", "suse", "opensuse-tumbleweed", "opensuse-leap"}, "zypper", "sudo zypper install stockfish"),
        ({"gentoo"}, "emerge", "sudo emerge games-board/stockfish"),
    ]
    out = []
    if {"arch", "manjaro", "endeavouros"} & set(ids):
        # Not in Arch's official repos, only the AUR: suggest it only when an AUR helper is installed,
        # otherwise the caller shows the download instead of a command that fails.
        for helper in ("yay", "paru"):
            if shutil.which(helper):
                out.append(InstallOption(helper, f"{helper} -S stockfish"))
                break
    for names, manager, cmd in table:
        if names & set(ids) and shutil.which(manager):
            out.append(InstallOption(manager, cmd))
    if shutil.which("brew"):  # Linuxbrew
        out.append(InstallOption("brew", "brew install stockfish", [shutil.which("brew") or "brew", "install", "stockfish"]))
    return out


def run_option(opt: InstallOption, timeout: float = 900) -> tuple[bool, str]:
    """Run a no-admin package install (brew/winget). Returns (ok, output)."""
    if not opt.argv:
        return False, "this one needs admin rights; run it in a terminal"
    try:
        p = subprocess.run(opt.argv, capture_output=True, text=True, timeout=timeout, **popen_kwargs())
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return p.returncode == 0, (p.stdout + p.stderr)[-2000:]


def asset_name_for(os_name: str, machine: str) -> list[str]:
    """Release asset names to look for, best first (Stockfish 18+ ships CPU-dispatching universal builds)."""
    m = machine.lower()
    arch = "arm64" if m in ("arm64", "aarch64") else "x86-64" if m in ("x86_64", "amd64") else m
    if os_name == "darwin":
        return ["stockfish-macos-universal", "stockfish-macos-m1-apple-silicon" if arch == "arm64" else "stockfish-macos-x86-64-avx2"]
    if os_name == "win32":
        return [f"stockfish-windows-{arch}-universal", f"stockfish-windows-{arch}-avx2", f"stockfish-windows-{arch}"]
    return [f"stockfish-linux-{arch}-universal", f"stockfish-ubuntu-{arch}-avx2", f"stockfish-ubuntu-{arch}"]


def pick_asset(assets: list[dict], os_name: Optional[str] = None, machine: Optional[str] = None) -> Optional[dict]:
    wanted = asset_name_for(os_name or sys.platform, machine or platform.machine())
    by_stem = {}
    for a in assets:
        stem = a["name"].split(".")[0]
        by_stem[stem] = a
    for w in wanted:
        if w in by_stem:
            return by_stem[w]
    return None


def _safe_extract(archive: Path, dest: Path) -> None:
    """Extract regular files and folders only, refusing paths that escape `dest`."""
    dest = dest.resolve()

    def check(name: str) -> None:
        if not (dest / name).resolve().is_relative_to(dest):
            raise ValueError(f"unsafe path in archive: {name}")

    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for n in z.namelist():
                check(n)
            z.extractall(dest)
        return
    with tarfile.open(archive) as t:
        members = [m for m in t.getmembers() if m.isfile() or m.isdir()]
        for m in members:
            check(m.name)
        t.extractall(dest, members=members)


def _find_binary(root: Path) -> Optional[Path]:
    exe = ".exe" if sys.platform == "win32" else ""
    best = None
    for p in root.rglob("stockfish*"):
        if p.is_file() and (p.suffix == exe if exe else p.suffix == ""):
            if best is None or p.stat().st_size > best.stat().st_size:
                best = p
    return best


def download_stockfish(
    engines_dir: str,
    *,
    progress: Optional[Callable[[int, int], None]] = None,
    cancel: Optional[threading.Event] = None,
    session=None,
) -> str:
    """Download the latest official Stockfish for this OS into `engines_dir`; return the binary's path."""
    s = session or http.session()
    r = http.get(s, RELEASES, headers={"Accept": "application/vnd.github+json"}, cancel=cancel)
    r.raise_for_status()
    rel = r.json()
    asset = pick_asset(rel.get("assets", []))
    if not asset:
        raise RuntimeError(f"no Stockfish build for {sys.platform}/{platform.machine()} in release {rel.get('tag_name')}")
    target = Path(engines_dir) / rel.get("tag_name", "stockfish")
    target.mkdir(parents=True, exist_ok=True)
    archive = target / asset["name"]
    h = hashlib.sha256()
    with http.get(s, asset["browser_download_url"], stream=True, cancel=cancel, timeout=60) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length", 0)) or int(asset.get("size", 0))
        done = 0
        with open(archive, "wb") as f:
            for chunk in resp.iter_content(1 << 16):
                if cancel is not None and cancel.is_set():
                    raise http.Cancelled()
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    digest = asset.get("digest") or ""
    if digest.startswith("sha256:") and digest[7:] != h.hexdigest():
        archive.unlink(missing_ok=True)
        raise RuntimeError("download checksum mismatch; try again")
    _safe_extract(archive, target)
    archive.unlink(missing_ok=True)
    binary = _find_binary(target)
    if not binary:
        raise RuntimeError("no Stockfish binary in the downloaded archive")
    if sys.platform != "win32":
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if not probe(str(binary), timeout=10):
        raise RuntimeError("the downloaded Stockfish does not run on this computer")
    return str(binary)
