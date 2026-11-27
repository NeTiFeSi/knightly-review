"""Qt dialogs: settings (first-run setup), engine finder/installer, and update progress."""

from __future__ import annotations

import logging
import threading
from typing import ClassVar

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
    QFontDatabase,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    Qt,
    QTimer,
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
        self.minutes.setToolTip("Engine time one update may spend. Analysis carries on in the next update.")
        wf.addRow("Analysis per update:", self.minutes)
        self.keep = QComboBox()
        self.keep.addItem("Stop when the update is done", "off")
        self.keep.addItem("Keep analysing, to have cards ready for later updates", "precalculate")
        self.keep.addItem("Keep adding cards, with no limit per update", "unlimited")
        self.keep.setCurrentIndex(max(0, self.keep.findData(self.cfg.get("keep_running", "off"))))
        self.keep.setToolTip(
            "After an update has made its cards. Stop it with Cancel in the progress window or by closing the profile."
        )
        wf.addRow("After the update:", self.keep)
        self.keep_hours = QDoubleSpinBox()
        self.keep_hours.setRange(0, 72)
        self.keep_hours.setDecimals(1)
        self.keep_hours.setSuffix(" h")
        self.keep_hours.setSpecialValueText("until stopped or everything is analysed")
        self.keep_hours.setValue(float(self.cfg.get("keep_running_hours", 0)))
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
        keep = self.keep.currentData() or "off"
        if (
            keep != "off"
            and self.cfg.get("keep_running", "off") == "off"
            and not askUser(
                "Keeping the analysis running uses the CPU at full load, for as long as "
                "you chose.\n\n• Laptops: the battery drains fast.\n• Heat, fan noise, and possible thermal "
                "throttling.\n• Other programs get slower; lower “Threads” to leave room.\n\n"
                "It stops with Cancel in the progress "
                "window or when you close the profile.\n\nTurn it on?",
                parent=self,
            )
        ):
            return
        for old in ("engine_path", "use_lc0", "lc0_path"):  # replaced by stockfish_path / no longer used
            self.cfg.pop(old, None)
        self.cfg.update(
            lichess_user=self.lichess.text().strip(),
            chesscom_user=self.chesscom.text().strip(),
            history_days=0 if self.all_time.isChecked() else self.days.value(),
            stockfish_path=self.engine_path,
            sf_depth=self.depth.value(),
            hash_mb=self.hash.value(),
            deck=self.deck.text().strip() or "Knightly Review",
            max_new_cards=self.max_cards.value(),
            analysis_minutes=self.minutes.value(),
            keep_running=keep,
            keep_running_hours=round(self.keep_hours.value(), 1),
            include_opponent=self.opponent.isChecked(),
            update_on_startup=self.on_start.isChecked(),
            update_every_hours=self.every.value(),
            threads=self.threads.value(),
            good_move=round(self.good.value(), 2),
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


def open_settings(first_run: bool = False) -> None:
    SettingsDialog(mw, first_run=first_run).exec()


def setup_menu() -> None:
    menu = mw.form.menuTools.addMenu("Knightly Review")
    for label, fn in (
        ("Update now", start_update),
        ("Show update progress", show_progress),
        ("Settings…", open_settings),
    ):
        a = menu.addAction(label)
        a.triggered.connect(lambda _=False, f=fn: f())
    mw.addonManager.setConfigAction(settings.ADDON, open_settings)
