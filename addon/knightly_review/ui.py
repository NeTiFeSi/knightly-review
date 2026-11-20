"""Qt dialogs: settings (first-run setup), engine finder/installer, and update progress."""

from __future__ import annotations

import threading
from typing import Optional

from aqt import mw
from aqt.qt import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDesktopServices,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    Qt,
    QUrl,
    QVBoxLayout,
    QWidget,
)
from aqt.utils import askUser, showWarning, tooltip

from . import settings
from .runner import updater
from .vendor.knightly.engine import discover as kd
from .vendor.knightly.engine import install as ki
from .vendor.knightly.sources import lichess as kl
from .vendor.knightly.sources.lichess_login import LoginFlow


def _bg(work, done, fail=None) -> None:
    """Run `work()` in a thread, then `done(result)` or `fail(exc)` on the main thread."""

    def run():
        try:
            r = work()
        except Exception as e:  # noqa: BLE001
            err = e  # `e` is unbound once the except block ends; the callback runs later
            mw.taskman.run_on_main(lambda: (fail or (lambda x: showWarning(str(x))))(err))
            return
        mw.taskman.run_on_main(lambda: done(r))

    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- engine dialog


LABEL = {kd.STOCKFISH: "Stockfish", kd.LC0: "Lc0"}


class EngineDialog(QDialog):
    """Find Stockfish or Lc0, or help install it. `self.path` is the choice after accept()."""

    def __init__(self, parent: QWidget, current: str = "", kind: str = kd.STOCKFISH):
        super().__init__(parent)
        self.kind = kind
        name = LABEL[kind]
        self.setWindowTitle(f"Knightly Review – {name}")
        self.path = current
        self.cancel = threading.Event()
        lay = QVBoxLayout(self)
        intro = QLabel(
            "Knightly Review analyses your games with Stockfish on this computer."
            if kind == kd.STOCKFISH else
            "Lc0 is optional. When it's on, it analyses alongside Stockfish and its judgement is used, "
            "except in mate positions. It is much faster with a graphics card (GPU)."
        )
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self.found = QComboBox()
        self.status = QLabel("Looking…")
        self.status.setWordWrap(True)
        found_box = QGroupBox(f"{name} found on this computer")
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
        if kind == kd.LC0:
            get = QLabel('Don\'t have it? Download Lc0 (it includes a network) from '
                         '<a href="https://lczero.org/play/download/">lczero.org</a>, then press “Check again” '
                         'or choose the file.')
            get.setWordWrap(True)
            get.setOpenExternalLinks(True)
            lay.addWidget(get)
            self.dl = None
            self._buttons(lay)
            return

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
                self.status.setText(f"Not found. Install {LABEL[self.kind]}, or choose its file.")
                return
            self.status.setText(f"{len(found)} found.")
            idx = next((i for i, e in enumerate(found) if e.path == current), 0)
            self.found.setCurrentIndex(idx)
            self.path = found[idx].path

        _bg(lambda: kd.discover(str(settings.ENGINES_DIR), self.path or None, kind=self.kind), done)

    def browse(self) -> None:
        name = LABEL[self.kind]
        path, _ = QFileDialog.getOpenFileName(self, f"Choose the {name} program")
        if not path:
            return
        d = kd.probe_details(path)
        if not d:
            showWarning("That file doesn't answer as a UCI chess engine.", parent=self)
            return
        if kd.kind_of(d[0]) != self.kind:
            showWarning(f"That is {d[0]}, not {name}. Knightly Review supports Stockfish and Lc0 only.", parent=self)
            return
        if not d[1]:
            showWarning(f"{d[0]} doesn't report win/draw/loss (the UCI_ShowWDL option), which Knightly "
                        "Review needs to judge moves. Please update it.", parent=self)
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


class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget, first_run: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Knightly Review – settings")
        self.cfg = settings.load()
        self.login: Optional[LoginFlow] = None
        lay = QVBoxLayout(self)
        if first_run:
            hello = QLabel(
                "<b>Welcome to Knightly Review.</b> Enter your usernames and check that Stockfish was found. "
                "New cards will be made from your recent games, and kept up to date in the background."
            )
            hello.setWordWrap(True)
            lay.addWidget(hello)

        acc = QGroupBox("Your games")
        af = QFormLayout(acc)
        self.lichess = QLineEdit(self.cfg.get("lichess_user", ""))
        self.lichess.setPlaceholderText("Lichess username (optional)")
        self.login_btn = QPushButton()
        self.login_status = QLabel()
        self.login_btn.clicked.connect(self.toggle_login)
        lrow = QHBoxLayout()
        lrow.addWidget(self.lichess)
        lrow.addWidget(self.login_btn)
        af.addRow("Lichess:", lrow)
        af.addRow("", self.login_status)
        self.chesscom = QLineEdit(self.cfg.get("chesscom_user", ""))
        self.chesscom.setPlaceholderText("Chess.com username (optional)")
        af.addRow("Chess.com:", self.chesscom)
        self.days = QSpinBox()
        self.days.setRange(7, 3650)
        self.days.setValue(int(self.cfg.get("history_days", 90)))
        self.days.setSuffix(" days")
        af.addRow("Look back:", self.days)
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
        self.depth.setToolTip("Every position is searched to this depth. 20 or more gives better judgements, "
                              "but each update takes longer.")
        drow = QHBoxLayout()
        drow.addWidget(self.depth)
        hint = QLabel("20+ recommended if your computer can take it")
        hint.setStyleSheet("color: gray")
        drow.addWidget(hint, 1)
        sff.addRow("Depth:", drow)
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

        lc = QGroupBox("Lc0 (optional, best with a graphics card)")
        lcf = QFormLayout(lc)
        self.use_lc0 = QCheckBox("Also analyse with Lc0, in parallel; its judgement is used except in mate positions")
        self.use_lc0.setChecked(bool(self.cfg.get("use_lc0", False)))
        lcf.addRow(self.use_lc0)
        self.lc0_label = QLabel()
        self.lc0_label.setWordWrap(True)
        lc0_change = QPushButton("Change…")
        lc0_change.clicked.connect(self.pick_lc0)
        lrow2 = QHBoxLayout()
        lrow2.addWidget(self.lc0_label, 1)
        lrow2.addWidget(lc0_change)
        lcf.addRow(lrow2)
        self.lc0_time = QDoubleSpinBox()
        self.lc0_time.setRange(0.1, 60)
        self.lc0_time.setSingleStep(0.1)
        self.lc0_time.setSuffix(" s")
        self.lc0_time.setValue(float(self.cfg.get("lc0_time", 0.5)))
        lcf.addRow("Time per position:", self.lc0_time)
        lay.addWidget(lc)

        work = QGroupBox("Cards and updates")
        wf = QFormLayout(work)
        self.deck = QLineEdit(self.cfg.get("deck", "Knightly Review"))
        wf.addRow("Deck:", self.deck)
        self.max_cards = QSpinBox()
        self.max_cards.setRange(1, 200)
        self.max_cards.setValue(int(self.cfg.get("max_new_cards", 10)))
        wf.addRow("New cards per update:", self.max_cards)
        self.grouping = QComboBox()
        self.grouping.addItem("One note per game, newest games first", "game")
        self.grouping.addItem("One note per game, biggest mistakes first (all games)", "ranked")
        self.grouping.addItem("By opening and move, costliest openings first", "opening")
        self.grouping.setCurrentIndex(max(0, self.grouping.findData(self.cfg.get("note_grouping", "game"))))
        self.grouping.setToolTip(
            "Biggest mistakes first: mistakes from all games ranked by win rate lost, more for games you\n"
            "didn't win and for positions you keep reaching.\n"
            "By opening: one note per opening (and your colour) and move number. Openings where you lost\n"
            "the most points (4 per loss, 2 per draw, 1 per win) come first, from the first move on, and\n"
            "new cards are studied in that order.")
        wf.addRow("Cards:", self.grouping)
        self.opening_loss = QDoubleSpinBox()
        self.opening_loss.setRange(0.1, 50)
        self.opening_loss.setSingleStep(0.5)
        self.opening_loss.setSuffix(" %")
        self.opening_loss.setValue(float(self.cfg.get("opening_min_loss", 2.0)))
        self.opening_loss.setToolTip("By opening: a move you played becomes a card when it loses more than this\n"
                                     "(in win rate). The default, 2 %, matches \"good move\": every ?!, ? and ??.")
        wf.addRow("By opening, card moves losing over:", self.opening_loss)
        self.grouping.currentIndexChanged.connect(
            lambda _i: self.opening_loss.setEnabled(self.grouping.currentData() == "opening"))
        self.opening_loss.setEnabled(self.grouping.currentData() == "opening")
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

        cpu = QGroupBox("Judging moves")
        cf = QFormLayout(cpu)
        self.good = QDoubleSpinBox()
        self.good.setRange(0.1, 10)
        self.good.setSingleStep(0.5)
        self.good.setSuffix(" %")
        self.good.setValue(float(self.cfg.get("good_move", 2.0)))
        self.good.setToolTip("A move this close to the engine's best (in win rate) counts as a correct answer.")
        cf.addRow("Good move: within", self.good)
        lay.addWidget(cpu)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.engine_path = self.cfg.get("stockfish_path") or self.cfg.get("engine_path", "")
        self.lc0_path = self.cfg.get("lc0_path", "")
        self._show_engine()
        self._show_lc0()
        self._show_login()
        if not self.engine_path or not self.lc0_path:
            # Pick found engines silently if there are some.
            _bg(lambda: kd.discover(str(settings.ENGINES_DIR)), self._auto_engines, lambda e: None)

    def _auto_engines(self, found) -> None:
        for e in found:
            if e.kind == kd.STOCKFISH and not self.engine_path:
                self.engine_path = e.path
                self._show_engine(e.name)
            if e.kind == kd.LC0 and not self.lc0_path:
                self.lc0_path = e.path
                self._show_lc0(e.name)

    def _show_engine(self, name: str = "") -> None:
        if self.engine_path:
            self.engine.setText(f"{name or 'Stockfish'}: {self.engine_path}")
        else:
            self.engine.setText("<b>Not found.</b> Press “Change…” to find or install Stockfish.")

    def _show_lc0(self, name: str = "") -> None:
        self.lc0_label.setText(f"{name or 'Lc0'}: {self.lc0_path}" if self.lc0_path else "Not found.")
        self.use_lc0.setEnabled(bool(self.lc0_path))
        if not self.lc0_path:
            self.use_lc0.setChecked(False)

    def pick_engine(self) -> None:
        d = EngineDialog(self, self.engine_path, kd.STOCKFISH)
        if d.exec() and d.path:
            self.engine_path = d.path
            self._show_engine()

    def pick_lc0(self) -> None:
        d = EngineDialog(self, self.lc0_path, kd.LC0)
        if d.exec() and d.path:
            self.lc0_path = d.path
            self._show_lc0()

    # Lichess login
    def _show_login(self) -> None:
        sec = settings.secrets()
        if sec.get("lichess_token"):
            self.login_btn.setText("Log out")
            self.login_status.setText(f"Logged in as {sec.get('lichess_login', '?')} (faster downloads).")
        else:
            self.login_btn.setText("Log in…")
            self.login_status.setText("Optional: logging in makes downloads faster. Your password stays on lichess.org.")
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
            if not self.lichess.text().strip():
                self.lichess.setText(name)
            self.login_btn.setEnabled(True)
            self._show_login()

        def fail(e):
            self.login_btn.setEnabled(True)
            self._show_login()
            showWarning(f"Lichess login failed: {e}", parent=self)

        _bg(work, done, fail)

    def save(self) -> None:
        if not (self.lichess.text().strip() or self.chesscom.text().strip()):
            showWarning("Enter a Lichess or Chess.com username.", parent=self)
            return
        if not self.engine_path:
            showWarning("Choose Stockfish first (press “Change…” in the Stockfish box).", parent=self)
            return
        self.cfg.pop("engine_path", None)  # replaced by stockfish_path
        self.cfg.update(
            lichess_user=self.lichess.text().strip(),
            chesscom_user=self.chesscom.text().strip(),
            history_days=self.days.value(),
            stockfish_path=self.engine_path,
            sf_depth=self.depth.value(),
            hash_mb=self.hash.value(),
            use_lc0=self.use_lc0.isChecked() and bool(self.lc0_path),
            lc0_path=self.lc0_path,
            lc0_time=round(self.lc0_time.value(), 2),
            deck=self.deck.text().strip() or "Knightly Review",
            max_new_cards=self.max_cards.value(),
            include_opponent=self.opponent.isChecked(),
            note_grouping=self.grouping.currentData() or "game",
            opening_min_loss=round(self.opening_loss.value(), 2),
            update_on_startup=self.on_start.isChecked(),
            update_every_hours=self.every.value(),
            threads=self.threads.value(),
            good_move=round(self.good.value(), 2),
            setup_done=True,
        )
        settings.save(self.cfg)
        updater.reschedule()
        self.accept()
        if askUser("Settings saved. Fetch and analyse your games now?\n\n"
                   "The first update can take a few minutes; Anki stays usable meanwhile."):
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
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle("Knightly Review – update")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        lay = QVBoxLayout(self)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.bar = QProgressBar()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        lay.addWidget(self.label)
        lay.addWidget(self.bar)
        lay.addWidget(self.log)
        row = QHBoxLayout()
        self.cancel_btn = QPushButton("Cancel update")
        self.cancel_btn.clicked.connect(lambda: updater.stop())
        hide = QPushButton("Hide (keeps running)")
        hide.clicked.connect(self.hide)
        row.addWidget(self.cancel_btn)
        row.addStretch()
        row.addWidget(hide)
        lay.addLayout(row)
        self.resize(520, 300)
        updater.listeners.append(self.update_progress)
        self._last_msg = ""
        self.update_progress(*updater.last)

    def update_progress(self, stage: str, done: int, total: int, msg: str) -> None:
        self.label.setText(msg)
        if total:
            self.bar.setRange(0, total)
            self.bar.setValue(done)
        elif stage in ("done", "error", "idle"):
            self.bar.setRange(0, 1)
            self.bar.setValue(1 if stage == "done" else 0)
        else:
            self.bar.setRange(0, 0)
        self.cancel_btn.setEnabled(updater.running and stage not in ("done", "error"))
        if msg != self._last_msg and stage != "analyse":
            self.log.appendPlainText(msg)
            self._last_msg = msg


_progress: Optional[ProgressWindow] = None


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


def open_settings(first_run: bool = False) -> None:
    SettingsDialog(mw, first_run=first_run).exec()


def setup_menu() -> None:
    menu = mw.form.menuTools.addMenu("Knightly Review")
    for label, fn in (("Update now", start_update), ("Show update progress", show_progress), ("Settings…", open_settings)):
        a = menu.addAction(label)
        a.triggered.connect(lambda _=False, f=fn: f())
    mw.addonManager.setConfigAction(settings.ADDON, open_settings)

