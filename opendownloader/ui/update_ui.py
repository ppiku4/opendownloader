"""Help → Check for updates: compare with the latest GitHub release and install it."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)

from .. import APP_NAME, __version__
from ..core import tools, updater
from ..util import human_size
from . import icons
from .threads import run_async

if TYPE_CHECKING:
    from .app import Controller


class _Relay(QObject):
    progress = Signal(int, int, str)
    finished = Signal(object, str)   # setup path or None, error text


class UpdateDialog(QDialog):
    def __init__(self, ctl: "Controller", parent: QWidget | None = None):
        super().__init__(parent)
        self.ctl = ctl
        self.info: updater.UpdateInfo | None = None
        self.stop = threading.Event()
        self.setWindowTitle("Check for updates")
        self.setWindowIcon(icons.icon("app"))
        self.label = QLabel("Checking for updates…")
        self.label.setWordWrap(True)
        self.label.setMinimumWidth(460)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.notes = QTextBrowser()
        self.notes.setOpenExternalLinks(True)
        self.notes.setMinimumHeight(180)
        self.notes.hide()
        self.action_btn = QPushButton()
        self.action_btn.clicked.connect(self._action)
        self.action_btn.hide()
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.action_btn)
        row.addWidget(self.close_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(self.label)
        lay.addWidget(self.bar)
        lay.addWidget(self.notes)
        lay.addLayout(row)
        self.relay = _Relay()
        self.relay.progress.connect(self._on_progress)
        self.relay.finished.connect(self._on_downloaded)
        run_async(lambda: updater.check(ctl.settings), self._on_checked)

    # ------------------------------------------------------------------ check
    def _on_checked(self, info: updater.UpdateInfo | None, exc: BaseException | None) -> None:
        self.bar.hide()
        if exc is not None:
            self.label.setText(f"Couldn't check for updates.\n\n{exc}")
            self._offer("Open release page", "page")
            return
        self.info = info
        if not info.newer:
            self.label.setText(f"You have the latest version ({__version__}).")
            return
        self.label.setText(f"<b>{APP_NAME} {info.version}</b> is available. You have {__version__}.")
        if info.notes.strip():
            self.notes.setMarkdown(info.notes)
            self.notes.show()
        if updater.can_self_install() and info.setup_url:
            size = f" ({human_size(info.setup_size)})" if info.setup_size else ""
            self._offer(f"Download and install{size}", "install")
        else:
            self._offer("Open download page", "page")
        self.close_btn.setText("Later")

    def _offer(self, text: str, kind: str) -> None:
        self.action_btn.setText(text)
        self.action_btn.setProperty("kind", kind)
        self.action_btn.show()
        self.action_btn.setDefault(True)

    def _action(self) -> None:
        if self.action_btn.property("kind") == "page":
            QDesktopServices.openUrl(QUrl(self.info.page_url if self.info else updater.RELEASES_PAGE))
            self.accept()
            return
        self.action_btn.setEnabled(False)
        self.close_btn.setText("Cancel")
        self.notes.hide()
        self.bar.setRange(0, 0)
        self.bar.show()
        self.label.setText("Downloading the update…")
        threading.Thread(target=self._work, daemon=True).start()

    # --------------------------------------------------------------- download
    def _work(self) -> None:
        last = [0.0]

        def progress(done: int, total: int, text: str) -> None:
            now = time.monotonic()
            if now - last[0] > 0.15 or done == total:
                last[0] = now
                self.relay.progress.emit(done, total, text)

        try:
            self.relay.finished.emit(updater.download(self.info, self.ctl.settings, progress, self.stop), "")
        except tools.Cancelled:
            self.relay.finished.emit(None, "")
        except Exception as e:  # noqa: BLE001
            self.relay.finished.emit(None, str(e))

    def _on_progress(self, done: int, total: int, text: str) -> None:
        if total > 1:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(done / total * 1000))
            self.label.setText(f"{text}  {human_size(done)} of {human_size(total)}")

    def _on_downloaded(self, setup: Path | None, err: str) -> None:
        if setup is None:
            if err:
                self.bar.hide()
                self.label.setText(f"The update couldn't be downloaded.\n\n{err}")
                self.close_btn.setText("Close")
                self._offer("Open download page", "page")
                self.action_btn.setEnabled(True)
            return
        self.label.setText(f"Installing {APP_NAME} {self.info.version}. It will restart by itself…")
        self.bar.setRange(0, 0)
        try:
            updater.launch_installer(setup)
        except OSError as e:
            self.label.setText(f"Couldn't start the installer:\n\n{e}")
            return
        # Running downloads are paused now and resumed by the updated app.
        self.ctl.quit(force=True, resume_later=True)

    def reject(self) -> None:
        self.stop.set()
        super().reject()


def check_for_updates(ctl: "Controller", parent: QWidget | None = None) -> None:
    dlg = UpdateDialog(ctl, parent)
    dlg.setWindowModality(Qt.WindowModal)
    dlg.exec()
