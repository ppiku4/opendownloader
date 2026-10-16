"""Offer to download optional tools (ffmpeg, yt-dlp) the first time a feature needs them."""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from ..core import tools
from ..util import human_size
from . import icons

if TYPE_CHECKING:
    from .app import Controller


class _Relay(QObject):
    progress = Signal(int, int, str)
    finished = Signal(bool, str)


class ToolDownloadDialog(QDialog):
    """Downloads + verifies + installs one tool, with progress and Cancel."""

    def __init__(self, ctl: "Controller", key: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.ctl = ctl
        self.key = key
        self.ok = False
        self.error = ""
        inf = tools.info(key)
        self.setWindowTitle(f"Downloading {inf.name}")
        self.setWindowIcon(icons.icon("app"))
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.label = QLabel(f"Preparing to download {inf.name}…")
        self.label.setMinimumWidth(420)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.cancel_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(self.label)
        lay.addWidget(self.bar)
        note = QLabel("<small>From the official release page; verified with SHA-256 before installing.</small>")
        lay.addWidget(note)
        lay.addLayout(row)
        self.stop = threading.Event()
        self.relay = _Relay()
        self.relay.progress.connect(self._on_progress)
        self.relay.finished.connect(self._on_finished)
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self) -> None:
        last = [0.0]

        def progress(done: int, total: int, text: str) -> None:
            now = time.monotonic()
            if now - last[0] > 0.15 or done == total:
                last[0] = now
                self.relay.progress.emit(done, total, text)

        try:
            tools.install(self.key, self.ctl.settings, progress, self.stop)
            self.relay.finished.emit(True, "")
        except tools.Cancelled:
            self.relay.finished.emit(False, "")
        except Exception as e:  # noqa: BLE001
            self.relay.finished.emit(False, str(e))

    def _on_progress(self, done: int, total: int, text: str) -> None:
        if total > 1:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(done / total * 1000))
            self.label.setText(f"{text}  {human_size(done)} of {human_size(total)}")
        else:
            self.bar.setRange(0, 0)
            self.label.setText(text)

    def _on_finished(self, ok: bool, err: str) -> None:
        self.ok, self.error = ok, err
        self.accept() if ok else self.reject()

    def _cancel(self) -> None:
        self.cancel_btn.setEnabled(False)
        self.label.setText("Cancelling…")
        self.stop.set()

    def closeEvent(self, e) -> None:  # noqa: N802
        self.stop.set()
        super().closeEvent(e)


def download_tool(ctl: "Controller", key: str, parent: QWidget | None = None) -> bool:
    """Run the download dialog. Returns True if the tool is now installed."""
    dlg = ToolDownloadDialog(ctl, key, parent)
    dlg.exec()
    if not dlg.ok and dlg.error:
        QMessageBox.warning(parent, f"{tools.info(key).name}", f"Couldn't install {tools.info(key).name}:\n\n{dlg.error}")
    return dlg.ok


def ensure_tool(ctl: "Controller", key: str, reason: str, on_done: Callable[[bool], None],
                required: bool, parent: QWidget | None = None) -> None:
    """If `key` is missing, offer to download it; then call on_done(available)."""
    if tools.available(key, ctl.settings):
        on_done(True)
        return
    pending = ctl.tool_prompts.get(key)
    if pending is not None:          # an offer for this tool is already open
        pending.append(on_done)
        return
    if not required and not ctl.settings.get(f"ask_{key.replace('-', '')}", True):
        on_done(False)
        return
    callbacks = [on_done]
    ctl.tool_prompts[key] = callbacks
    ok = False
    try:
        inf = tools.info(key)
        box = QMessageBox(parent)
        box.setWindowTitle(f"Download {inf.name}?")
        box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        box.setIconPixmap(icons.pixmap("video", 48))
        if not inf.supported:
            box.setText(f"{reason}<br><br>{inf.name} can't be downloaded automatically on this system.<br>"
                        f"{inf.manual_hint}")
            box.setStandardButtons(QMessageBox.Ok)
            box.exec()
        else:
            box.setText(
                f"{reason}<br><br><b>{inf.name}</b> is a free, open-source tool that {inf.purpose}. "
                f"OpenDownloader can download it now from its official release page "
                f"(about {inf.approx_mb} MB, one time) and verify it before installing.")
            dl = box.addButton(f"Download {inf.name}", QMessageBox.AcceptRole)
            box.addButton("Cancel" if required else "Not now", QMessageBox.RejectRole)
            box.setDefaultButton(dl)
            dont = None
            if not required:
                dont = QCheckBox("Don't ask again (you can download it later in Options → Video)")
                box.setCheckBox(dont)
            box.exec()
            if dont is not None and dont.isChecked():
                ctl.settings.update({f"ask_{key.replace('-', '')}": False})
            if box.clickedButton() is dl:
                ok = download_tool(ctl, key, parent)
                if ok:
                    ctl.notify(f"{inf.name} installed", f"{inf.name} is ready to use.")
    finally:
        ctl.tool_prompts.pop(key, None)
    for cb in callbacks:
        cb(ok)
