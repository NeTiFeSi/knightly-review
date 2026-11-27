"""Qt dialogs: settings (first-run setup), engine finder/installer, and update progress."""

from __future__ import annotations

import logging
import threading
from typing import ClassVar

from aqt import mw
from aqt.qt import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDesktopServices,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFontDatabase,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    Qt,
    QTableWidget,
    QTableWidgetItem,
    QTimer,
    QUrl,
    QVBoxLayout,
    QWidget,
)
from aqt.utils import askUser, showWarning, tooltip

from . import settings
from .runner import updater
from .vendor.knightly.cache import SqliteCache
from .vendor.knightly.engine import discover as kd
from .vendor.knightly.engine import install as ki
from .vendor.knightly.export import export_all
from .vendor.knightly.sources import lichess as kl
from .vendor.knightly.sources.lichess_login import LoginFlow

log = logging.getLogger(__name__)


def _bg(work, done, fail=None) -> None:
    """Run `work()` in a thread, then `done(result)` or `fail(exc)` on the main thread."""

    def run():
        try:
            r = work()
        except Exception as e:
            log.exception("background task failed")
            err = e  # `e` is unbound once the except block ends; the callback runs later
            mw.taskman.run_on_main(lambda: (fail or (lambda x: showWarning(str(x))))(err))
            return
        mw.taskman.run_on_main(lambda: done(r))

    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- engine dialog


class EngineDialog(QDialog):
    """Find Stockfish, or help install it. `self.path` is the choice after accept()."""

    def __init__(self, parent: QWidget, current: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Knightly Review – Stockfish")
        self.path = current
        self.cancel = threading.Event()
        lay = QVBoxLayout(self)
        intro = QLabel("Knightly Review analyses your games with Stockfish on this computer.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self.found = QComboBox()
        self.status = QLabel("Looking…")
        self.status.setWordWrap(True)
        found_box = QGroupBox("Stockfish found on this computer")
        fl = QVBoxLayout(found_box)
        fl.addWidget(self.found)
        fl.addWidget(self.status)
        row = QHBoxLayout()
        recheck = QPushButton("Check again")
        recheck.clicked.connect(self.scan)
        browse = QPushButton("Choose a file…")
        browse.clicked.connect(self.browse)
        row.addWidget(recheck)
        row.addWidget(browse)
        row.addStretch()
        fl.addLayout(row)
        lay.addWidget(found_box)
        get_box = QGroupBox("Don't have it? Get Stockfish")
        gl = QVBoxLayout(get_box)
        self.options = ki.package_options()
        if self.options:
            opt = self.options[0]
            gl.addWidget(QLabel(f"Install it yourself with {opt.manager}, then press “Check again”:"))
            cmd_row = QHBoxLayout()
            cmd = QLineEdit(opt.command)
            cmd.setReadOnly(True)
            copy = QPushButton("Copy")
            copy.clicked.connect(lambda: (QApplication.clipboard().setText(opt.command), tooltip("Copied")))
            cmd_row.addWidget(cmd)
            cmd_row.addWidget(copy)
            if opt.argv:
                run = QPushButton("Run it for me")
                run.clicked.connect(lambda: self.run_option(opt, run))
                cmd_row.addWidget(run)
            gl.addLayout(cmd_row)
            gl.addWidget(QLabel("Or:"))
        self.dl = QPushButton("Download Stockfish for me (official build, about 100 MB)")
        self.dl.clicked.connect(self.download)
        gl.addWidget(self.dl)
        self.bar = QProgressBar()
        self.bar.hide()
        gl.addWidget(self.bar)
        where = QLabel(f"Downloads go to {settings.ENGINES_DIR} and need no admin rights.")
        where.setWordWrap(True)
        where.setStyleSheet("color: gray")
        gl.addWidget(where)
        lay.addWidget(get_box)
        self._buttons(lay)

    def _buttons(self, lay: QVBoxLayout) -> None:
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.found.currentIndexChanged.connect(self._pick)
        self.resize(560, 0)
        self.scan()

    def _pick(self) -> None:
        p = self.found.currentData()
        if p:
            self.path = p

    def scan(self, select: str = "") -> None:
        self.status.setText("Looking…")
        current = select or self.path

        def done(found: list[kd.EngineInfo]) -> None:
            self.found.clear()
            for e in found:
                self.found.addItem(f"{e.name}  ({e.found_in}: {e.path})", e.path)
            if not found:
                self.status.setText("Not found. Install Stockfish, or choose its file.")
                return
            self.status.setText(f"{len(found)} found.")
            idx = next((i for i, e in enumerate(found) if e.path == current), 0)
            self.found.setCurrentIndex(idx)
            self.path = found[idx].path

        _bg(lambda: kd.discover(str(settings.ENGINES_DIR), self.path or None), done)

    def browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose the Stockfish program")
        if not path:
            return
        d = kd.probe_details(path)
        if not d:
            showWarning("That file doesn't answer as a UCI chess engine.", parent=self)
            return
        if not kd.is_stockfish(d[0]):
            showWarning(f"That is {d[0]}, not Stockfish. Knightly Review supports Stockfish only.", parent=self)
            return
        if not d[1]:
            showWarning(
                f"{d[0]} doesn't report win/draw/loss (the UCI_ShowWDL option), which Knightly "
                "Review needs to judge moves. Please update it.",
                parent=self,
            )
            return
        self.path = path
        self.scan(select=path)

    def run_option(self, opt: ki.InstallOption, button: QPushButton) -> None:
        button.setEnabled(False)
        self.status.setText(f"Running “{opt.command}”… this can take a few minutes.")

        def done(r):
            ok, out = r
            button.setEnabled(True)
            if not ok:
                showWarning(f"{opt.manager} failed:\n{out}", parent=self)
            self.scan()

        _bg(lambda: ki.run_option(opt), done)

    def download(self) -> None:
        self.dl.setEnabled(False)
        self.bar.show()
        self.bar.setRange(0, 0)

        def progress(done: int, total: int) -> None:
            def upd():
                if total:
                    self.bar.setRange(0, total)
                    self.bar.setValue(done)

            mw.taskman.run_on_main(upd)

        def ok(path: str) -> None:
            self.bar.hide()
            self.dl.setEnabled(True)
            tooltip("Stockfish is ready")
            self.path = path
            self.scan(select=path)

        def fail(e: Exception) -> None:
            self.bar.hide()
            self.dl.setEnabled(True)
            showWarning(f"Download failed:\n{e}", parent=self)

        _bg(lambda: ki.download_stockfish(str(settings.ENGINES_DIR), progress=progress, cancel=self.cancel), ok, fail)

    def reject(self) -> None:
        self.cancel.set()
        super().reject()


# ---------------------------------------------------------------- settings dialog


class AccountsTable(QWidget):
    """The accounts whose games are collected: site, name, and whether they are yours or another player's."""

    SOURCES: ClassVar = [("lichess", "Lichess"), ("chesscom", "Chess.com"), ("pgn", "Name in PGN files")]
    ROLES: ClassVar = [("me", "Mine"), ("other", "Another player")]

    def __init__(self, accounts: list[dict]):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Site", "Username", "Whose games"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setToolTip(
            "Puzzles are made from your games. Other players' games (masters, rivals, players to compare\n"
            "with) are downloaded and exported too. A name in PGN files is matched against the White and\n"
            "Black headers, as the files write it (for example “Carlsen, Magnus”)."
        )
        self.table.setMinimumHeight(110)
        lay.addWidget(self.table)
        row = QHBoxLayout()
        add = QPushButton("Add")
        add.clicked.connect(lambda: self.add_row())
        remove = QPushButton("Remove")
        remove.clicked.connect(self.remove_selected)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch()
        lay.addLayout(row)
        for a in accounts:
            self.add_row(a.get("source", "lichess"), a.get("name", ""), a.get("role", "me"))
        if not accounts:
            self.add_row("lichess")
            self.add_row("chesscom")

    def _combo(self, items, value: str) -> QComboBox:
        c = QComboBox()
        for data, label in items:
            c.addItem(label, data)
        c.setCurrentIndex(max(0, c.findData(value)))
        return c

    def add_row(self, source: str = "lichess", name: str = "", role: str = "me") -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setCellWidget(r, 0, self._combo(self.SOURCES, source))
        self.table.setItem(r, 1, QTableWidgetItem(name))
        self.table.setCellWidget(r, 2, self._combo(self.ROLES, role))

    def remove_selected(self) -> None:
        for r in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(r)

    def accounts(self) -> list[dict]:
        out = []
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 1)
            name = item.text().strip() if item else ""
            if name:
                out.append(
                    {
                        "source": self.table.cellWidget(r, 0).currentData(),
                        "name": name,
                        "role": self.table.cellWidget(r, 2).currentData(),
                    }
                )
        return out

    def add_if_missing(self, source: str, name: str) -> None:
        """After a Lichess login: the account becomes yours, in an empty row of that site or a new one."""
        if not name or any(a["source"] == source and a["name"].lower() == name.lower() for a in self.accounts()):
            return
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 1)
            if self.table.cellWidget(r, 0).currentData() == source and not (item and item.text().strip()):
                self.table.setItem(r, 1, QTableWidgetItem(name))
                return
        self.add_row(source, name)


class PathList(QWidget):
    """PGN files and folders (searched for *.pgn) to read games from."""

    def __init__(self, paths: list[str]):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.addItems(paths)
        self.list.setMaximumHeight(70)
        lay.addWidget(self.list, 1)
        col = QVBoxLayout()
        for label, fn in (("Add file…", self.add_file), ("Add folder…", self.add_folder), ("Remove", self.remove)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            col.addWidget(b)
        col.addStretch()
        lay.addLayout(col)

    def add_file(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "PGN files", "", "PGN (*.pgn *.PGN);;All files (*)")
        self.list.addItems([f for f in files if f not in self.paths()])

    def add_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "A folder with PGN files")
        if d and d not in self.paths():
            self.list.addItem(d)

    def remove(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))

    def paths(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]


class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget, first_run: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Knightly Review – settings")
        self.cfg = settings.load()
        self.login: LoginFlow | None = None
        lay = QVBoxLayout(self)
        if first_run:
            hello = QLabel(
                "<b>Welcome to Knightly Review.</b> Enter your usernames and check that Stockfish was found. "
                "New cards will be made from your recent games, and kept up to date in the background."
            )
            hello.setWordWrap(True)
            lay.addWidget(hello)

        acc = QGroupBox("Games")
        af = QFormLayout(acc)
        self.accounts = AccountsTable(self.cfg.get("accounts", []))
        af.addRow(self.accounts)
        self.login_btn = QPushButton()
        self.login_status = QLabel()
        self.login_status.setWordWrap(True)
        self.login_btn.clicked.connect(self.toggle_login)
        lrow = QHBoxLayout()
        lrow.addWidget(self.login_status, 1)
        lrow.addWidget(self.login_btn)
        af.addRow("Lichess login:", lrow)
        self.pgn = PathList(self.cfg.get("pgn_paths", []))
        af.addRow("PGN files:", self.pgn)
        self.export_dir = QLineEdit(self.cfg.get("export_dir", ""))
        self.export_dir.setPlaceholderText("off")
        self.export_dir.setToolTip("After new games arrive, every account's games are written here as PGN files.")
        browse = QPushButton("Choose…")
        browse.clicked.connect(self.pick_export_dir)
        erow = QHBoxLayout()
        erow.addWidget(self.export_dir, 1)
        erow.addWidget(browse)
        af.addRow("Export games to:", erow)
        self.contact = QLineEdit(self.cfg.get("contact_email", ""))
        self.contact.setPlaceholderText("optional")
        self.contact.setToolTip(
            "Chess.com asks programs that use its API for a way to contact whoever runs them.\n"
            "If you enter an email address, it is sent to Chess.com with each download, and nowhere else."
        )
        af.addRow("Email for Chess.com:", self.contact)
        days = int(self.cfg.get("history_days", 90))
        self.days = QSpinBox()
        self.days.setRange(1, 36500)
        self.days.setValue(days if days > 0 else 90)
        self.days.setSuffix(" days")
        self.all_time = QCheckBox("All time")
        self.all_time.setToolTip(
            "Download every game you ever played. Analysis then carries on over several\n"
            "updates, the positions you reach most often first."
        )
        self.all_time.toggled.connect(lambda on: self.days.setEnabled(not on))
        self.all_time.setChecked(days <= 0)
        self.days.setEnabled(days > 0)
        drow = QHBoxLayout()
        drow.addWidget(self.days)
        drow.addWidget(self.all_time)
        drow.addStretch()
        af.addRow("Look back:", drow)
        lay.addWidget(acc)

        sf = QGroupBox("Stockfish (required)")
        sff = QFormLayout(sf)
        self.engine = QLabel()
        self.engine.setWordWrap(True)
        change = QPushButton("Change…")
        change.clicked.connect(self.pick_engine)
        row = QHBoxLayout()
        row.addWidget(self.engine, 1)
        row.addWidget(change)
        sff.addRow(row)
        self.depth = QSpinBox()
        self.depth.setRange(8, 60)
        self.depth.setValue(int(self.cfg.get("sf_depth", 13)))
        self.depth.setToolTip("Every position is searched to this depth, with every move scored. Higher is slower.")
        sff.addRow("Depth:", self.depth)
        self.threads = QSpinBox()
        self.threads.setRange(0, 256)
        self.threads.setSpecialValueText("half the CPU cores")
        self.threads.setValue(int(self.cfg.get("threads", 0)))
        sff.addRow("Threads:", self.threads)
        self.hash = QSpinBox()
        self.hash.setRange(16, 65536)
        self.hash.setSingleStep(64)
        self.hash.setSuffix(" MB")
        self.hash.setValue(int(self.cfg.get("hash_mb", 64)))
        self.hash.setToolTip("Memory for Stockfish's hash table. More helps deeper searches.")
        sff.addRow("Hash (RAM):", self.hash)
        lay.addWidget(sf)

        work = QGroupBox("Cards and updates")
        wf = QFormLayout(work)
        self.deck = QLineEdit(self.cfg.get("deck", "Knightly Review"))
        wf.addRow("Deck:", self.deck)
        self.max_cards = QSpinBox()
        self.max_cards.setRange(1, 200)
        self.max_cards.setValue(int(self.cfg.get("max_new_cards", 10)))
        wf.addRow("New cards per update:", self.max_cards)
        self.minutes = QSpinBox()
        self.minutes.setRange(0, 24 * 60)
        self.minutes.setSuffix(" min")
        self.minutes.setSpecialValueText("until the cards are found")
        self.minutes.setValue(int(self.cfg.get("analysis_minutes", 10)))
        self.minutes.setToolTip("Engine time for confirming puzzles and making their cards, each time it runs.")
        wf.addRow("Making cards, per run:", self.minutes)
        self.annotate_minutes = QSpinBox()
        self.annotate_minutes.setRange(0, 24 * 60)
        self.annotate_minutes.setSuffix(" min")
        self.annotate_minutes.setSpecialValueText("until every position is annotated")
        self.annotate_minutes.setValue(int(self.cfg.get("annotate_minutes", 10)))
        self.annotate_minutes.setToolTip(
            "Engine time for the cheap look at every position of your games (how good each move was),\n"
            "each time new games arrive. It carries on where it stopped."
        )
        wf.addRow("Annotating, per run:", self.annotate_minutes)
        self.keep = QCheckBox("Keep annotating in the background, at low priority, while Anki is open")
        self.keep.setChecked(bool(self.cfg.get("keep_annotating", False)))
        self.keep.setToolTip(
            "Cards are still made only up to “New cards per update”; annotating ahead makes them come faster.\n"
            "Stop it with Cancel in the progress window or by closing the profile."
        )
        wf.addRow("", self.keep)
        self.keep_hours = QDoubleSpinBox()
        self.keep_hours.setRange(0, 72)
        self.keep_hours.setDecimals(1)
        self.keep_hours.setSuffix(" h")
        self.keep_hours.setSpecialValueText("until stopped or everything is annotated")
        self.keep_hours.setValue(float(self.cfg.get("keep_annotating_hours", 0)))
        wf.addRow("For at most:", self.keep_hours)
        self.opponent = QCheckBox("Also make cards from my opponents' mistakes (played from their side)")
        self.opponent.setChecked(bool(self.cfg.get("include_opponent", False)))
        wf.addRow("", self.opponent)
        self.on_start = QCheckBox("Update when Anki starts")
        self.on_start.setChecked(bool(self.cfg.get("update_on_startup", True)))
        wf.addRow("", self.on_start)
        self.every = QSpinBox()
        self.every.setRange(0, 168)
        self.every.setSuffix(" h")
        self.every.setSpecialValueText("never")
        self.every.setValue(int(self.cfg.get("update_every_hours", 6)))
        wf.addRow("Also update every:", self.every)
        lay.addWidget(work)

        manage = QGroupBox("Notes already in Anki")
        mf = QVBoxLayout(manage)
        self.update_notes = QCheckBox(
            "Keep them up to date (rebuild notes from older versions, refresh the Notes field)"
        )
        self.update_notes.setChecked(bool(self.cfg.get("update_notes", True)))
        self.update_tags = QCheckBox("Replace their knightly:: tags when they are written again")
        self.update_tags.setChecked(bool(self.cfg.get("update_tags", True)))
        self.suspend_outdated = QCheckBox("Suspend notes from older versions that can no longer be kept up to date")
        self.suspend_outdated.setChecked(bool(self.cfg.get("suspend_outdated", False)))
        self.suspend_outdated.setToolTip(
            "Notes made by versions before puzzle keys, and notes made under older rules whose game is no\n"
            "longer among your games. Unsuspend one in the browser to keep it; Undo reverts the update."
        )
        for w in (self.update_notes, self.update_tags, self.suspend_outdated):
            mf.addWidget(w)
        lay.addWidget(manage)

        cpu = QGroupBox("Judging moves")
        cf = QFormLayout(cpu)
        self.good = QDoubleSpinBox()
        self.good.setRange(0.1, 10)
        self.good.setSingleStep(0.5)
        self.good.setSuffix(" %")
        self.good.setValue(float(self.cfg.get("good_move", 1.0)))
        self.good.setToolTip("A move this close to the engine's best (in win rate) counts as a correct answer.")
        cf.addRow("Good move: within", self.good)
        lay.addWidget(cpu)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.engine_path = self.cfg.get("stockfish_path") or self.cfg.get("engine_path", "")
        self._show_engine()
        self._show_login()
        if not self.engine_path:
            # Pick a found Stockfish silently if there is one.
            _bg(lambda: kd.discover(str(settings.ENGINES_DIR)), self._auto_engines, lambda e: None)

    def _auto_engines(self, found) -> None:
        for e in found:
            if not self.engine_path:
                self.engine_path = e.path
                self._show_engine(e.name)

    def _show_engine(self, name: str = "") -> None:
        if self.engine_path:
            self.engine.setText(f"{name or 'Stockfish'}: {self.engine_path}")
        else:
            self.engine.setText("<b>Not found.</b> Press “Change…” to find or install Stockfish.")

    def pick_engine(self) -> None:
        d = EngineDialog(self, self.engine_path)
        if d.exec() and d.path:
            self.engine_path = d.path
            self._show_engine()

    def pick_export_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Export games to", self.export_dir.text())
        if d:
            self.export_dir.setText(d)

    # Lichess login
    def _show_login(self) -> None:
        sec = settings.secrets()
        if sec.get("lichess_token"):
            self.login_btn.setText("Log out")
            self.login_status.setText(f"Logged in as {sec.get('lichess_login', '?')} (faster downloads).")
        else:
            self.login_btn.setText("Log in…")
            self.login_status.setText(
                "Optional: logging in makes downloads faster. Your password stays on lichess.org."
            )
        self.login_status.setStyleSheet("color: gray")

    def toggle_login(self) -> None:
        sec = settings.secrets()
        if sec.get("lichess_token"):
            sec.pop("lichess_token", None)
            sec.pop("lichess_login", None)
            settings.save_secrets(sec)
            self._show_login()
            return
        flow = LoginFlow()
        self.login = flow
        url = flow.start()
        QDesktopServices.openUrl(QUrl(url))
        self.login_btn.setEnabled(False)
        self.login_status.setText("Waiting for you to approve in the browser…")

        def work():
            token = flow.wait(timeout=300)
            name = kl.account(token).get("username", "")
            return token, name

        def done(r):
            token, name = r
            sec = settings.secrets()
            sec.update(lichess_token=token, lichess_login=name)
            settings.save_secrets(sec)
            self.accounts.add_if_missing("lichess", name)
            self.login_btn.setEnabled(True)
            self._show_login()

        def fail(e):
            self.login_btn.setEnabled(True)
            self._show_login()
            showWarning(f"Lichess login failed: {e}", parent=self)

        _bg(work, done, fail)

    def save(self) -> None:
        accounts = self.accounts.accounts()
        if not (any(a["source"] != "pgn" for a in accounts) or self.pgn.paths()):
            showWarning("Add a Lichess or Chess.com account, or a PGN file.", parent=self)
            return
        if not self.engine_path:
            showWarning("Choose Stockfish first (press “Change…” in the Stockfish box).", parent=self)
            return
        keep = self.keep.isChecked()
        if (
            keep
            and not self.cfg.get("keep_annotating", False)
            and not askUser(
                "Annotating in the background uses the CPU, at low priority, for as long as "
                "you chose.\n\n• Laptops: the battery drains faster.\n• Heat and fan noise.\n"
                "• Lower “Threads” to leave room for other programs.\n\n"
                "It stops with Cancel in the progress "
                "window or when you close the profile.\n\nTurn it on?",
                parent=self,
            )
        ):
            return
        for old in ("engine_path", "use_lc0", "lc0_path", "keep_running", "keep_running_hours"):  # no longer used
            self.cfg.pop(old, None)
        self.cfg.update(
            accounts=accounts,
            pgn_paths=self.pgn.paths(),
            export_dir=self.export_dir.text().strip(),
            contact_email=self.contact.text().strip(),
            history_days=0 if self.all_time.isChecked() else self.days.value(),
            stockfish_path=self.engine_path,
            sf_depth=self.depth.value(),
            hash_mb=self.hash.value(),
            deck=self.deck.text().strip() or "Knightly Review",
            max_new_cards=self.max_cards.value(),
            analysis_minutes=self.minutes.value(),
            annotate_minutes=self.annotate_minutes.value(),
            keep_annotating=keep,
            keep_annotating_hours=round(self.keep_hours.value(), 1),
            include_opponent=self.opponent.isChecked(),
            update_on_startup=self.on_start.isChecked(),
            update_every_hours=self.every.value(),
            threads=self.threads.value(),
            good_move=round(self.good.value(), 2),
            update_notes=self.update_notes.isChecked(),
            update_tags=self.update_tags.isChecked(),
            suspend_outdated=self.suspend_outdated.isChecked(),
            setup_done=True,
        )
        settings.save(self.cfg)
        updater.reschedule()
        self.accept()
        if askUser(
            "Settings saved. Fetch and analyse your games now?\n\n"
            "The first update can take a few minutes; Anki stays usable meanwhile."
        ):
            start_update()

    def reject(self) -> None:
        if self.login:
            self.login.close()
        if not self.cfg.get("setup_done"):
            cfg = settings.load()
            cfg["setup_done"] = True  # don't nag on every start; Tools menu has Settings
            settings.save(cfg)
        super().reject()


# ---------------------------------------------------------------- progress window


class ProgressWindow(QDialog):
    """Current step, two bars (puzzles found, and the current analysis step), and a log of the whole run."""

    STAGES: ClassVar[dict[str, str]] = {
        "fetch": "Downloading",
        "analyse": "Puzzles",
        "cards": "Cards",
        "start": "Starting",
    }

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle("Knightly Review – update")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        lay = QVBoxLayout(self)
        self.label = QLabel()
        self.label.setWordWrap(True)
        f = self.label.font()
        f.setBold(True)
        self.label.setFont(f)
        lay.addWidget(self.label)

        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        self.stage_name = QLabel()
        self.bar = QProgressBar()
        self.bar.setTextVisible(True)
        self.step_label = QLabel("Step")
        self.step_bar = QProgressBar()
        self.step_bar.setTextVisible(True)
        self.step_bar.setFormat("%p %")
        self.step_info = QLabel()
        self.step_info.setStyleSheet("color: gray")
        grid.addWidget(self.stage_name, 0, 0)
        grid.addWidget(self.bar, 0, 1)
        grid.addWidget(self.step_label, 1, 0)
        grid.addWidget(self.step_bar, 1, 1)
        grid.addWidget(self.step_info, 2, 1)
        # The engine's row: live depth of the search in progress.
        self.engine_name, self.engine_bar, self.engine_info = QLabel(), QProgressBar(), QLabel()
        self.engine_bar.setRange(0, 1000)
        self.engine_bar.setTextVisible(False)
        self.engine_info.setStyleSheet("color: gray")
        grid.addWidget(self.engine_name, 3, 0)
        grid.addWidget(self.engine_bar, 3, 1)
        grid.addWidget(self.engine_info, 4, 1)
        self.position_info = QLabel()
        self.position_info.setStyleSheet("color: gray")
        grid.addWidget(self.position_info, 5, 1)
        lay.addLayout(grid)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log.setPlaceholderText("Each step of an update is logged here.")
        lay.addWidget(self.log, 1)
        self._seq = 0

        row = QHBoxLayout()
        self.cancel_btn = QPushButton("Cancel update")
        self.cancel_btn.clicked.connect(lambda: updater.stop())
        hide = QPushButton("Hide (keeps running)")
        hide.clicked.connect(self.hide)
        row.addWidget(self.cancel_btn)
        row.addStretch()
        row.addWidget(hide)
        lay.addLayout(row)
        self.resize(560, 380)
        updater.listeners.append(self.update_progress)
        self.update_progress(*updater.last)
        self._timer = QTimer(self)  # the numbers come from the run's tracker, a few times a second
        self._timer.timeout.connect(self._poll)
        self._timer.start(200)
        self._poll()

    def update_progress(self, stage: str, done: int, total: int, msg: str) -> None:
        self.label.setText(msg)
        self.stage_name.setText(self.STAGES.get(stage, "Update"))
        if total:
            self.bar.setRange(0, total)
            self.bar.setValue(done)
            self.bar.setFormat({"analyse": "%v / %m found"}.get(stage, "%v / %m"))
        elif stage in ("done", "error", "idle"):
            self.bar.setRange(0, 1)
            self.bar.setValue(1 if stage == "done" else 0)
            self.bar.setFormat({"done": "Done", "error": "Failed"}.get(stage, ""))
        else:
            self.bar.setRange(0, 0)  # busy
        self.cancel_btn.setEnabled(updater.keeping or (updater.running and stage not in ("done", "error")))
        self._update_log()

    def _poll(self) -> None:
        """Step, engines and position, from the run's tracker (see core/knightly/progress.py)."""
        if not self.isVisible():
            return
        from .vendor.knightly.progress import describe

        d = describe(updater.tracker.snapshot())
        show = d["stage_ratio"] is not None
        for w in (self.step_label, self.step_bar):
            w.setVisible(show)
        if show:
            self.step_bar.setRange(0, 1000)
            self.step_bar.setValue(int(1000 * d["stage_ratio"]))
            self.step_label.setText(d["step"] or "Step")
        self.step_info.setText(" · ".join(x for x in (d["stage"], d["ply"], d["budget"], d["elapsed"]) if x))
        e = d["engines"].get("stockfish")
        for w in (self.engine_name, self.engine_bar, self.engine_info):
            w.setVisible(bool(e))
        if e:
            self.engine_name.setText(e["label"])
            self.engine_bar.setValue(int(1000 * (e["ratio"] or 0)))
            self.engine_info.setText(f"{e['detail']} · {e['counts']}")
        self.position_info.setText(d["position"])
        self.position_info.setVisible(bool(d["position"]))

    def _update_log(self) -> None:
        new = updater.history_since(self._seq)
        if not new:
            return
        sb = self.log.verticalScrollBar()
        at_end = sb.value() >= sb.maximum() - 4
        for seq, line in new:
            self.log.appendPlainText(line)
            self._seq = seq
        if at_end:
            sb.setValue(sb.maximum())


_progress: ProgressWindow | None = None


def show_progress() -> None:
    global _progress
    if _progress is None:
        _progress = ProgressWindow(mw)
    _progress.show()
    _progress.raise_()
    _progress.activateWindow()


def start_update() -> None:
    if updater.running:
        show_progress()
        return
    if updater.start(manual=True):
        show_progress()


def export_games() -> None:
    """Every account's stored games, written as PGN to a folder the user picks."""
    cfg = settings.core_config()
    if not cfg.extract.accounts and not cfg.extract.pgn_paths:
        showWarning("Add an account or a PGN file in the settings first.")
        return
    folder = QFileDialog.getExistingDirectory(mw, "Export games to", cfg.extract.export_dir)
    if not folder:
        return

    def work() -> dict[str, int]:
        cache = SqliteCache(settings.CACHE_PATH)
        try:
            return export_all(cache, cfg.extract, folder)
        finally:
            cache.close()

    def done(files: dict[str, int]) -> None:
        tooltip(f"Exported {sum(files.values())} games to {len(files)} PGN files.", period=4000)

    _bg(work, done, lambda e: showWarning(f"Exporting the games failed: {e}"))


def open_settings(first_run: bool = False) -> None:
    SettingsDialog(mw, first_run=first_run).exec()


def setup_menu() -> None:
    menu = mw.form.menuTools.addMenu("Knightly Review")
    for label, fn in (
        ("Update now", start_update),
        ("Show update progress", show_progress),
        ("Export games…", export_games),
        ("Settings…", open_settings),
    ):
        a = menu.addAction(label)
        a.triggered.connect(lambda _=False, f=fn: f())
    mw.addonManager.setConfigAction(settings.ADDON, open_settings)
