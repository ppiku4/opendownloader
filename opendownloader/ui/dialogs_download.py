"""Dialogs related to adding and monitoring downloads."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QProgressBar, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from ..core import batch
from ..core.base import make_session
from ..core.manager import open_path, reveal_in_folder
from ..core.media_engine import probe_media
from ..core.models import DownloadItem, Kind, Status
from ..util import guess_filename, human_eta, human_size, human_speed, is_url, parse_content_disposition, sanitize_filename
from . import icons
from .threads import run_async
from .widgets import SegmentBar

if TYPE_CHECKING:
    from .app import Controller


def _browse_dir(parent: QWidget, line: QLineEdit) -> None:
    d = QFileDialog.getExistingDirectory(parent, "Choose folder", line.text())
    if d:
        line.setText(d)


def _dir_row(parent: QWidget, line: QLineEdit) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.addWidget(line, 1)
    b = QToolButton()
    b.setText("…")
    b.clicked.connect(lambda: _browse_dir(parent, line))
    h.addWidget(b)
    return w


# ---------------------------------------------------------------------------
class AddUrlDialog(QDialog):
    def __init__(self, ctl: "Controller", parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Enter new address to download")
        self.setWindowIcon(icons.icon("add"))
        self.url = QLineEdit()
        clip = QGuiApplication.clipboard().text().strip()
        if is_url(clip) or clip.lower().startswith("magnet:?"):
            self.url.setText(clip)
        self.url.setPlaceholderText("https://…, ftp://… or a magnet: link")
        self.url.setMinimumWidth(520)
        self.use_auth = QCheckBox("Use authorization")
        self.user = QLineEdit()
        self.pwd = QLineEdit()
        self.pwd.setEchoMode(QLineEdit.Password)
        self.user.setEnabled(False)
        self.pwd.setEnabled(False)
        self.use_auth.toggled.connect(self.user.setEnabled)
        self.use_auth.toggled.connect(self.pwd.setEnabled)
        self.page_mode = QCheckBox("This is a web page with a video (YouTube, Vimeo, …)")
        form = QFormLayout()
        form.addRow("Address:", self.url)
        form.addRow("", self.use_auth)
        form.addRow("Login:", self.user)
        form.addRow("Password:", self.pwd)
        form.addRow("", self.page_mode)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(bb)

    def request(self) -> dict[str, Any]:
        req: dict[str, Any] = {"url": self.url.text().strip()}
        if self.use_auth.isChecked():
            req["auth"] = (self.user.text(), self.pwd.text())
        if self.page_mode.isChecked():
            req["kind"] = Kind.YTDLP.value
        return req


# ---------------------------------------------------------------------------
class DownloadInfoDialog(QDialog):
    """'Download File Info' - confirm name/category/folder before starting."""

    def __init__(self, ctl: "Controller", req: dict[str, Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.ctl = ctl
        self.req = req
        st = ctl.settings
        self.setWindowTitle("Download File Info")
        self.setWindowIcon(icons.icon("app"))
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self._name_edited = bool(req.get("filename"))
        name = sanitize_filename(req.get("filename") or guess_filename(req["url"], mime=req.get("mime")))
        self.url = QLineEdit(req["url"])
        self.category = QComboBox()
        self.category.addItems(["General", *st.get("categories").keys()])
        self.category.setCurrentText(st.category_for(name))
        self.path = QLineEdit(str(st.folder_for(name) / name))
        self.path.textEdited.connect(self._edited)
        browse = QToolButton()
        browse.setText("…")
        browse.clicked.connect(self._browse)
        self.desc = QLineEdit(req.get("description", ""))
        size = int(req.get("fileSize") or -1)
        self.size_lbl = QLabel(human_size(size) if size > 0 else "Checking…")
        self.category.currentTextChanged.connect(self._category_changed)

        form = QFormLayout()
        form.addRow("URL:", self.url)
        form.addRow("Category:", self.category)
        prow = QHBoxLayout()
        prow.addWidget(self.path, 1)
        prow.addWidget(browse)
        form.addRow("Save As:", prow)
        form.addRow("Description:", self.desc)
        form.addRow("File size:", self.size_lbl)

        self.later = QPushButton("Download Later")
        menu = QMenu(self.later)
        for q in ctl.manager.queues.values():
            menu.addAction(f"Add to '{q.name}'", lambda qid=q.id: self._go(start=False, queue_id=qid))
        menu.addAction("Just add to list", lambda: self._go(start=False))
        self.later.setMenu(menu)
        start = QPushButton("Start Download")
        start.setDefault(True)
        start.setIcon(icons.icon("resume"))
        start.clicked.connect(lambda: self._go(start=True))
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns = QHBoxLayout()
        btns.addStretch(1)
        for b in (self.later, start, cancel):
            btns.addWidget(b)
        top = QHBoxLayout()
        ic = QLabel()
        ic.setPixmap(icons.pixmap("app", 48))
        top.addWidget(ic, 0, Qt.AlignTop)
        top.addLayout(form, 1)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(btns)
        self.resize(620, 230)
        if size <= 0 or not req.get("filename"):
            self._probe()

    def _edited(self) -> None:
        self._name_edited = True

    def _browse(self) -> None:
        f, _ = QFileDialog.getSaveFileName(self, "Save As", self.path.text())
        if f:
            self.path.setText(f)
            self._name_edited = True

    def _category_changed(self, cat: str) -> None:
        name = Path(self.path.text()).name
        self.path.setText(str(self.ctl.settings.folder_for(name, cat) / name))

    def _probe(self) -> None:
        req = self.req
        st = self.ctl.settings

        def work() -> tuple[int, str | None, str | None]:
            item = DownloadItem(url=req["url"], headers=req.get("headers") or {}, referer=req.get("referer", ""))
            s = make_session(st, item)
            try:
                r = s.get(req["url"], headers={"Range": "bytes=0-0"}, stream=True, timeout=10)
                cr = r.headers.get("Content-Range", "")
                size = int(cr.rsplit("/", 1)[-1]) if "/" in cr and cr.rsplit("/", 1)[-1].isdigit() else int(
                    r.headers.get("Content-Length") or -1) if r.status_code == 200 else -1
                name = parse_content_disposition(r.headers.get("Content-Disposition"))
                if not name:
                    name = guess_filename(r.url, None, r.headers.get("Content-Type"))
                r.close()
                return size, name, r.headers.get("Content-Type")
            finally:
                s.close()

        def done(res, exc) -> None:
            if exc is not None:
                self.size_lbl.setText("Unknown")
                return
            size, name, _ = res
            self.size_lbl.setText(human_size(size) if size > 0 else "Unknown")
            if size > 0:
                self.req["fileSize"] = size
            if name and not self._name_edited:
                name = sanitize_filename(name)
                self.category.blockSignals(True)
                self.category.setCurrentText(self.ctl.settings.category_for(name))
                self.category.blockSignals(False)
                self.path.setText(str(self.ctl.settings.folder_for(name) / name))

        run_async(work, done)

    def _go(self, start: bool, queue_id: int | None = None) -> None:
        p = Path(self.path.text())
        self.ctl.add_download({
            **self.req, "url": self.url.text().strip(), "filename": p.name, "save_dir": str(p.parent),
            "category": self.category.currentText(), "description": self.desc.text(),
            "filename_locked": self._name_edited,
        }, start=start, queue_id=queue_id)
        self.accept()


# ---------------------------------------------------------------------------
class QualityDialog(QDialog):
    """Choose resolution / bitrate for an HLS or DASH stream."""

    def __init__(self, ctl: "Controller", req: dict[str, Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.ctl = ctl
        self.req = req
        self.setWindowTitle("Download video")
        self.setWindowIcon(icons.icon("video"))
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        title = sanitize_filename(req.get("title") or "video", "video")
        self.name = QLineEdit(Path(title).stem[:150])
        self.audio = bool(req.get("audio"))
        self.dir = QLineEdit(str(ctl.settings.folder_for("x.m4a", "Music") if self.audio
                                 else ctl.settings.folder_for("x.mp4", "Video")))
        self.list = QListWidget()
        self.status = QLabel("Detecting available qualities…")
        self.status.setWordWrap(True)
        form = QFormLayout()
        form.addRow("File name:", self.name)
        form.addRow("Save to:", _dir_row(self, self.dir))
        self.go = QPushButton("Download")
        self.go.setDefault(True)
        self.go.setEnabled(False)
        self.go.clicked.connect(lambda: self._go(True))
        later = QPushButton("Download Later")
        later.clicked.connect(lambda: self._go(False))
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns = QHBoxLayout()
        btns.addWidget(self.status, 1)
        for b in (later, self.go, cancel):
            btns.addWidget(b)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(QLabel("Quality:"))
        lay.addWidget(self.list, 1)
        lay.addLayout(btns)
        self.resize(560, 360)
        self.list.itemDoubleClicked.connect(lambda _i: self._go(True))
        run_async(lambda: probe_media(ctl.settings, req["url"], ctl.headers_for(req), req.get("referer", "")),
                  self._loaded)

    def _loaded(self, info, exc) -> None:
        if exc is not None:
            self.status.setText(f"Error: {exc}")
            return
        if info.get("error") and not info.get("options"):
            self.status.setText(("🔒 " if info.get("protected") else "") + info["error"])
            return
        if info["kind"] == "http":
            # Not a manifest - treat it as a normal file download.
            self.hide()
            self.ctl.request_download({**self.req, "filename": self.name.text() + Path(self.req["url"].split("?")[0]).suffix})
            self.accept()
            return
        self.info = info
        for opt in info["options"]:
            it = QListWidgetItem(opt["label"])
            it.setData(Qt.UserRole, opt)
            if opt.get("protected"):
                it.setFlags(it.flags() & ~Qt.ItemIsEnabled)
            self.list.addItem(it)
        want = self.req.get("preferHeight")
        for i in range(self.list.count()):
            o = self.list.item(i).data(Qt.UserRole)
            if not o.get("protected") and (not want or o.get("height") == want):
                self.list.setCurrentRow(i)
                break
        kind = "HLS" if info["kind"] == "hls" else "DASH"
        self.status.setText(f"{kind} stream - {self.list.count()} option(s)" + (f"  ⚠ {info['error']}" if info.get("error") else ""))
        self.go.setEnabled(True)

    def _go(self, start: bool) -> None:
        cur = self.list.currentItem()
        if cur is None:
            return
        opt = cur.data(Qt.UserRole)
        ext = ".m4a" if opt.get("audio_only") or self.audio else ".mp4"
        self.ctl.add_download({
            **self.req, "kind": opt["media"]["type"], "media": opt["media"],
            "filename": sanitize_filename(self.name.text()) + ext, "save_dir": self.dir.text(),
            "filename_locked": True, "category": "Music" if ext == ".m4a" else "Video",
        }, start=start, queue_id=None if start else self.ctl.manager.main_queue.id)
        self.accept()


# ---------------------------------------------------------------------------
class PageVideoDialog(QDialog):
    """Video on a web page (YouTube, Vimeo, ...): pick a quality via yt-dlp."""

    def __init__(self, ctl: "Controller", req: dict[str, Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.ctl = ctl
        self.req = req
        audio = bool(req.get("preferAudio"))
        self.setWindowTitle("Download audio" if audio else "Download video")
        self.setWindowIcon(icons.icon("video"))
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.name = QLineEdit(sanitize_filename(req.get("title") or "", "audio" if audio else "video")[:150])
        self._video_dir = str(ctl.settings.folder_for("x.mp4", "Video"))
        self._audio_dir = str(ctl.settings.folder_for("x.m4a", "Music"))
        self.dir = QLineEdit(self._audio_dir if audio else self._video_dir)
        self.list = QListWidget()
        self.status = QLabel("Getting audio information…" if audio else "Getting video information…")
        self.status.setWordWrap(True)
        form = QFormLayout()
        form.addRow("File name:", self.name)
        form.addRow("Save to:", _dir_row(self, self.dir))
        self.go = QPushButton("Download")
        self.go.setDefault(True)
        self.go.setEnabled(False)
        self.go.clicked.connect(lambda: self._go(True))
        self.later = QPushButton("Download Later")
        self.later.setEnabled(False)
        self.later.clicked.connect(lambda: self._go(False))
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns = QHBoxLayout()
        btns.addWidget(self.status, 1)
        for b in (self.later, self.go, cancel):
            btns.addWidget(b)
        lay = QVBoxLayout(self)
        url = QLabel(f"<small>{req['url']}</small>")
        url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(url)
        lay.addLayout(form)
        lay.addWidget(QLabel("Quality:"))
        lay.addWidget(self.list, 1)
        lay.addLayout(btns)
        self.resize(560, 380)
        self.list.itemDoubleClicked.connect(lambda _i: self._go(True))
        from ..core.media_engine import probe_page

        run_async(lambda: probe_page(ctl.settings, req["url"], req.get("referer", "")), self._loaded)

    def _loaded(self, info, exc) -> None:
        if exc is not None or not info or (info.get("error") and not info.get("options")):
            err = str(exc) if exc else (info or {}).get("error", "unknown error")
            self.status.setText(f"Couldn't read this video: {err[:300]}")
            return
        if info.get("title") and not self.req.get("title"):
            self.name.setText(sanitize_filename(info["title"], "video")[:150])
        for opt in info["options"]:
            it = QListWidgetItem(opt["label"])
            it.setData(Qt.UserRole, opt)
            self.list.addItem(it)
        want = int(self.req.get("preferHeight") or 1080)
        rows = [i for i in range(self.list.count()) if 0 < self.list.item(i).data(Qt.UserRole)["height"] <= want]
        if self.req.get("preferAudio"):
            rows = [i for i in range(self.list.count()) if self.list.item(i).data(Qt.UserRole).get("audio_only")] or rows
        self.list.setCurrentRow(rows[0] if rows else 0)
        mins = int(info.get("duration") or 0) // 60
        self.status.setText(f"{self.list.count()} option(s)" + (f" · {mins} min" if mins else ""))
        self.go.setEnabled(True)
        self.later.setEnabled(True)

    def _go(self, start: bool) -> None:
        cur = self.list.currentItem()
        if cur is None:
            return
        opt = cur.data(Qt.UserRole)
        audio = bool(opt.get("audio_only"))
        ext = ".m4a" if audio else ".mp4"          # yt-dlp replaces it with the real extension
        folder = self.dir.text()
        if audio and folder == self._video_dir:
            folder = self._audio_dir
        elif not audio and folder == self._audio_dir:
            folder = self._video_dir
        self.ctl.add_download({
            "url": self.req["url"], "referer": self.req.get("referer", ""), "kind": "ytdlp",
            "media": opt["media"], "filename": sanitize_filename(self.name.text(), "video") + ext,
            "filename_locked": True, "save_dir": folder, "category": "Music" if audio else "Video",
        }, start=start, queue_id=None if start else self.ctl.manager.main_queue.id)
        self.accept()


class LinksDialog(QDialog):
    """Choose which of many links (from a page / grabber / batch) to download."""

    def __init__(self, ctl: "Controller", urls: list[str], referer: str = "", headers: dict | None = None,
                 parent: QWidget | None = None, title: str = "Download all links"):
        super().__init__(parent)
        self.ctl = ctl
        self.referer = referer
        self.headers = headers or {}
        self.setWindowTitle(title)
        self.setWindowIcon(icons.icon("batch"))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter (e.g. .zip .pdf or any text)")
        self.filter.textChanged.connect(self._apply_filter)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["File", "Type", "Address"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        seen = set()
        for u in urls:
            if u in seen:
                continue
            seen.add(u)
            r = self.table.rowCount()
            self.table.insertRow(r)
            name = guess_filename(u)
            c = QTableWidgetItem(name)
            c.setFlags(c.flags() | Qt.ItemIsUserCheckable)
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            capture = ext in ctl.settings.get("capture_extensions")
            c.setCheckState(Qt.Checked if capture else Qt.Unchecked)
            c.setData(Qt.UserRole, u)
            self.table.setItem(r, 0, c)
            self.table.setItem(r, 1, QTableWidgetItem(ext))
            self.table.setItem(r, 2, QTableWidgetItem(u))
        self.table.resizeColumnsToContents()
        self.dir = QLineEdit("")
        self.dir.setPlaceholderText("Default (category folders)")
        all_b = QPushButton("Check all")
        all_b.clicked.connect(lambda: self._check(True))
        none_b = QPushButton("Uncheck all")
        none_b.clicked.connect(lambda: self._check(False))
        self.queue = QComboBox()
        for q in ctl.manager.queues.values():
            self.queue.addItem(q.name, q.id)
        self.start_q = QCheckBox("Start queue now")
        self.start_q.setChecked(True)
        ok = QPushButton("Add selected")
        ok.setDefault(True)
        ok.clicked.connect(self._go)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(all_b)
        row.addWidget(none_b)
        row.addStretch(1)
        row.addWidget(QLabel("Queue:"))
        row.addWidget(self.queue)
        row.addWidget(self.start_q)
        form = QFormLayout()
        form.addRow("Save to:", _dir_row(self, self.dir))
        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        lay = QVBoxLayout(self)
        lay.addWidget(self.filter)
        lay.addWidget(self.table, 1)
        lay.addLayout(row)
        lay.addLayout(form)
        lay.addLayout(btns)
        self.resize(820, 520)

    def _check(self, on: bool) -> None:
        for r in range(self.table.rowCount()):
            if not self.table.isRowHidden(r):
                self.table.item(r, 0).setCheckState(Qt.Checked if on else Qt.Unchecked)

    def _apply_filter(self, text: str) -> None:
        terms = text.lower().split()
        for r in range(self.table.rowCount()):
            url = self.table.item(r, 2).text().lower()
            self.table.setRowHidden(r, bool(terms) and not any(t in url for t in terms))

    def _go(self) -> None:
        qid = self.queue.currentData()
        n = 0
        for r in range(self.table.rowCount()):
            c = self.table.item(r, 0)
            if c.checkState() == Qt.Checked and not self.table.isRowHidden(r):
                self.ctl.add_download({"url": c.data(Qt.UserRole), "referer": self.referer,
                                       "headers": self.headers, "save_dir": self.dir.text().strip()},
                                      start=False, queue_id=qid)
                n += 1
        if n and self.start_q.isChecked():
            self.ctl.manager.start_queue(qid)
        self.accept()


# ---------------------------------------------------------------------------
class BatchDialog(QDialog):
    def __init__(self, ctl: "Controller", parent: QWidget | None = None):
        super().__init__(parent)
        self.ctl = ctl
        self.setWindowTitle("Add batch download")
        self.setWindowIcon(icons.icon("batch"))
        self.pattern = QLineEdit("https://example.com/images/photo_*.jpg")
        self.start = QLineEdit("1")
        self.end = QLineEdit("10")
        self.width = QSpinBox()
        self.width.setRange(0, 10)
        self.width.setValue(0)
        self.preview = QLabel()
        self.preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        for w in (self.pattern, self.start, self.end):
            w.textChanged.connect(self._update)
        self.width.valueChanged.connect(self._update)
        form = QFormLayout()
        form.addRow("Address with wildcard (*):", self.pattern)
        form.addRow("From:", self.start)
        form.addRow("To:", self.end)
        form.addRow("Wildcard size (zero padding):", self.width)
        form.addRow("Preview:", self.preview)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._go)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(bb)
        self.resize(560, 200)
        self._update()

    def _urls(self) -> list[str]:
        return batch.expand(self.pattern.text().strip(), self.start.text().strip(), self.end.text().strip(),
                            self.width.value())

    def _update(self) -> None:
        try:
            u = self._urls()
            self.preview.setText(f"{u[0]}\n…\n{u[-1]}\n({len(u)} files)")
        except (ValueError, IndexError) as e:
            self.preview.setText(str(e))

    def _go(self) -> None:
        try:
            urls = self._urls()
        except ValueError as e:
            QMessageBox.warning(self, "Batch download", str(e))
            return
        self.accept()
        LinksDialog(self.ctl, urls, parent=self.parent(), title="Batch download").exec()


# ---------------------------------------------------------------------------
class ProgressDialog(QDialog):
    """Per-download window with speed, ETA, connection map and speed limiter."""

    def __init__(self, ctl: "Controller", item_id: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.ctl = ctl
        self.item_id = item_id
        self.setWindowIcon(icons.icon("app"))
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.url = QLabel()
        self.url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.url.setWordWrap(True)
        self.lbl = {k: QLabel() for k in ("status", "stage", "size", "downloaded", "rate", "eta", "resume", "conns")}
        form = QFormLayout()
        self.form = form
        form.addRow("Address:", self.url)
        form.addRow("Status:", self.lbl["status"])
        form.addRow("Stage:", self.lbl["stage"])
        form.addRow("File size:", self.lbl["size"])
        form.addRow("Downloaded:", self.lbl["downloaded"])
        form.addRow("Transfer rate:", self.lbl["rate"])
        form.addRow("Time left:", self.lbl["eta"])
        form.addRow("Resume capability:", self.lbl["resume"])
        form.addRow("Connections:", self.lbl["conns"])
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.segbar = SegmentBar()
        self.details = QTableWidget(0, 3)
        self.details.setHorizontalHeaderLabels(["N.", "Downloaded", "Info"])
        self.details.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.details.verticalHeader().hide()
        self.details.setEditTriggers(QTableWidget.NoEditTriggers)
        self.details.setMaximumHeight(170)
        self.details.setMinimumHeight(110)
        self.limit_cb = QCheckBox("Limit speed to")
        self.limit_sp = QSpinBox()
        self.limit_sp.setRange(1, 1_000_000)
        self.limit_sp.setSuffix(" KB/s")
        item = ctl.manager.items[item_id]
        self.limit_sp.setValue(item.speed_limit_kbps or 500)
        self.limit_cb.setChecked(item.speed_limit_kbps > 0)
        self.limit_cb.toggled.connect(self._limit)
        self.limit_sp.editingFinished.connect(self._limit)
        self.toggle_btn = QPushButton()
        self.toggle_btn.clicked.connect(self._toggle)
        self.folder_btn = QPushButton("Open folder")
        self.folder_btn.clicked.connect(lambda: reveal_in_folder(self.ctl.manager.items[self.item_id].path))
        self.details_btn = QPushButton("<< Hide details")
        self.details_btn.clicked.connect(self._toggle_details)
        close = QPushButton("Hide")
        close.clicked.connect(self.close)
        lim = QHBoxLayout()
        lim.addWidget(self.limit_cb)
        lim.addWidget(self.limit_sp)
        self._compact = None
        lim.addStretch(1)
        btns = QHBoxLayout()
        btns.addWidget(self.details_btn)
        btns.addStretch(1)
        btns.addWidget(self.folder_btn)
        btns.addWidget(self.toggle_btn)
        btns.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.bar)
        lay.addWidget(self.segbar)
        lay.addWidget(self.details, 1)
        lay.addLayout(lim)
        lay.addLayout(btns)
        self.resize(560, 470)
        self.refresh()

    def _toggle_details(self) -> None:
        vis = not self.details.isVisible()
        self.details.setVisible(vis)
        self.details_btn.setText("<< Hide details" if vis else "Show details >>")
        self.adjustSize()

    def _limit(self) -> None:
        kbps = self.limit_sp.value() if self.limit_cb.isChecked() else 0
        self.ctl.manager.set_item_speed_limit(self.item_id, kbps)

    def _toggle(self) -> None:
        it = self.ctl.manager.items.get(self.item_id)
        if it is None:
            return
        if it.status.active:
            self.ctl.manager.pause(self.item_id)
        elif it.status == Status.COMPLETE:
            open_path(it.path)
        else:
            self.ctl.manager.start(self.item_id)

    def refresh(self) -> None:
        it = self.ctl.manager.items.get(self.item_id)
        if it is None:
            self.close()
            return
        pct = int(it.progress * 100)
        self.setWindowTitle(f"{pct}% {it.filename}")
        self.url.setText(it.url if len(it.url) < 200 else it.url[:200] + "…")
        status = it.status.value
        if it.status == Status.ERROR:
            status += f": {it.error}"
        elif it.kind in ("hls", "dash") and it.media.get("segments_total"):
            status += f"  (segment {it.media.get('segments_done', 0)}/{it.media['segments_total']})"
        self.lbl["status"].setText(status)
        media_kind = it.kind in ("hls", "dash", "ytdlp", "torrent")
        est = " (estimated)" if media_kind and it.kind != "torrent" and it.status.active else ""
        self.lbl["size"].setText(human_size(it.size) + est if it.size > 0 else "Getting information…" if it.status.active else "—")
        self.lbl["downloaded"].setText(f"{human_size(it.downloaded)} ({pct}%)" if it.size > 0 else human_size(it.downloaded))
        self.lbl["rate"].setText(human_speed(it.speed) or "—")
        self.lbl["eta"].setText(human_eta(it.eta) or "—")
        self.lbl["resume"].setText("Yes" if it.resumable or media_kind else "No / unknown")
        if it.kind == "torrent":
            self.lbl["conns"].setText(f"{it.active_connections} peers" if it.status.active else "—")
        else:
            self.lbl["conns"].setText(str(it.active_connections) if it.kind in ("http", "hls", "dash") else "—")
        stage = it.stage if it.status.active or it.stage.startswith("Seeding") else ""
        self.form.setRowVisible(self.lbl["stage"], bool(stage))
        self.lbl["stage"].setText(stage)
        # the per-connection map/table only means something for multi-connection file downloads
        has_segments = bool(it.segments)
        self.segbar.setVisible(has_segments)
        self.details_btn.setVisible(has_segments)
        # yt-dlp does its own networking: its speed is capped when it starts (global limit),
        # a per-download change can't be applied mid-way, so don't offer it
        own_net = it.kind == "ytdlp"
        self.limit_cb.setVisible(not own_net)
        self.limit_sp.setVisible(not own_net)
        compact = not has_segments
        if compact:
            self.details.setVisible(False)
        if compact != self._compact:
            self._compact = compact
            self.layout().activate()
            self.resize(self.width(), self.sizeHint().height() if compact else max(self.height(), 470))
        if it.status.active and it.size <= 0:
            self.bar.setRange(0, 0)            # busy indicator until the size is known
        else:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(it.progress * 1000))
        self.segbar.set_item(it)
        segs = list(it.segments)
        self.details.setRowCount(len(segs))
        for i, s in enumerate(segs):
            info = "Complete" if s.done else ("Receiving data…" if it.status.active else "Paused")
            vals = (str(i + 1), human_size(s.downloaded), f"{info}   [{s.start:,} – {s.end:,}]")
            for c, v in enumerate(vals):
                cell = self.details.item(i, c)
                if cell is None:
                    self.details.setItem(i, c, QTableWidgetItem(v))
                elif cell.text() != v:
                    cell.setText(v)
        if it.status.active:
            self.toggle_btn.setText("Pause")
            self.toggle_btn.setIcon(icons.icon("pause"))
        elif it.status == Status.COMPLETE:
            self.toggle_btn.setText("Open")
            self.toggle_btn.setIcon(icons.icon("resume"))
        else:
            self.toggle_btn.setText("Resume")
            self.toggle_btn.setIcon(icons.icon("resume"))


# ---------------------------------------------------------------------------
class CompleteDialog(QDialog):
    def __init__(self, ctl: "Controller", item: DownloadItem, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Download complete")
        self.setWindowIcon(icons.icon("app"))
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        lbl = QLabel(f"<b>{item.filename}</b><br>{human_size(item.size)}<br><small>{item.path}</small>"
                     + (f"<br><i>{item.description}</i>" if item.description else ""))
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lbl.setWordWrap(True)
        dont = QCheckBox("Don't show this dialog again")
        dont.toggled.connect(lambda v: ctl.settings.update({"show_complete_dialog": not v}))
        o = QPushButton("Open")
        o.setDefault(True)
        o.clicked.connect(lambda: (open_path(item.path), self.close()))
        f = QPushButton("Open folder")
        f.clicked.connect(lambda: (reveal_in_folder(item.path), self.close()))
        c = QPushButton("Close")
        c.clicked.connect(self.close)
        top = QHBoxLayout()
        ic = QLabel()
        ic.setPixmap(icons.pixmap("clean", 48))
        top.addWidget(ic, 0, Qt.AlignTop)
        top.addWidget(lbl, 1)
        btns = QHBoxLayout()
        btns.addWidget(dont)
        btns.addStretch(1)
        for b in (o, f, c):
            btns.addWidget(b)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(btns)
        self.resize(480, 150)


# ---------------------------------------------------------------------------
class PropertiesDialog(QDialog):
    def __init__(self, ctl: "Controller", item_id: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.ctl = ctl
        self.item_id = item_id
        it = ctl.manager.items[item_id]
        self.setWindowTitle(f"Properties - {it.filename}")
        self.setWindowIcon(icons.icon("options"))
        self.name = QLineEdit(it.filename)
        self.name.setEnabled(it.status != Status.COMPLETE and not it.status.active)
        self.url = QLineEdit(it.url)
        self.referer = QLineEdit(it.referer)
        self.desc = QLineEdit(it.description)
        self.dir = QLineEdit(it.save_dir)
        self.dir.setEnabled(not it.status.active and it.status != Status.COMPLETE)
        self.conns = QSpinBox()
        self.conns.setRange(0, 32)
        self.conns.setSpecialValueText("Default")
        self.conns.setValue(it.max_connections)
        info = QLabel(f"Status: {it.status.value}   Size: {human_size(it.size)}   Kind: {it.kind}"
                      + (f"<br>Error: {it.error}" if it.error else ""))
        info.setWordWrap(True)
        form = QFormLayout()
        form.addRow("File name:", self.name)
        form.addRow("Address:", self.url)
        form.addRow("Referer:", self.referer)
        form.addRow("Description:", self.desc)
        form.addRow("Save to:", _dir_row(self, self.dir))
        form.addRow("Max connections:", self.conns)
        form.addRow(info)

        ck = QGroupBox("Verify checksum")
        self.algo = QComboBox()
        self.algo.addItems(["sha256", "md5", "sha1", "sha512"])
        self.expected = QLineEdit()
        self.expected.setPlaceholderText("Paste expected hash (optional)")
        self.hash_out = QLineEdit()
        self.hash_out.setReadOnly(True)
        calc = QPushButton("Calculate")
        calc.setEnabled(it.status == Status.COMPLETE)
        calc.clicked.connect(self._hash)
        ckl = QFormLayout(ck)
        row = QHBoxLayout()
        row.addWidget(self.algo)
        row.addWidget(calc)
        ckl.addRow(row)
        ckl.addRow("Result:", self.hash_out)
        ckl.addRow("Expected:", self.expected)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(ck)
        lay.addWidget(bb)
        self.resize(640, 400)

    def _hash(self) -> None:
        it = self.ctl.manager.items[self.item_id]
        algo = self.algo.currentText()
        self.hash_out.setText("Calculating…")

        def work() -> str:
            h = hashlib.new(algo)
            with open(it.path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            return h.hexdigest()

        def done(res, exc) -> None:
            if exc:
                self.hash_out.setText(f"Error: {exc}")
                return
            exp = self.expected.text().strip().lower()
            mark = "" if not exp else ("   ✔ MATCH" if exp == res else "   ✘ DOES NOT MATCH")
            self.hash_out.setText(res + mark)

        run_async(work, done)

    def _save(self) -> None:
        m = self.ctl.manager
        it = m.items[self.item_id]
        new_url = self.url.text().strip()
        fields: dict[str, Any] = {"referer": self.referer.text().strip(), "description": self.desc.text(),
                                  "max_connections": self.conns.value()}
        if self.name.isEnabled() and self.name.text().strip() and self.name.text() != it.filename:
            if it.part_path.exists():
                new_part = Path(it.save_dir) / (sanitize_filename(self.name.text()) + ".part")
                it.part_path.rename(new_part)
            fields["filename"] = sanitize_filename(self.name.text())
            fields["filename_locked"] = True
        if self.dir.isEnabled() and self.dir.text() != it.save_dir and not it.part_path.exists():
            fields["save_dir"] = self.dir.text()
        m.update_item(self.item_id, **fields)
        if new_url != it.url:
            was_active = it.status.active
            m.update_item(self.item_id, url=new_url)
            if was_active:
                m.refresh_url(self.item_id, new_url)
        self.accept()


def ask_refresh_address(ctl: "Controller", item_id: int, parent: QWidget) -> None:
    """'Refresh download address' - wait for the browser to provide a new link."""
    it = ctl.manager.items[item_id]
    box = QMessageBox(parent)
    box.setWindowTitle("Refresh download address")
    box.setIcon(QMessageBox.Information)
    box.setText(
        f"<b>{it.filename}</b><br><br>The download link may have expired. OpenDownloader will now "
        "wait for a fresh link.<br><br>Open the page you downloaded this file from in your browser and "
        "click the download link again - OpenDownloader will pick up the new address automatically "
        "(matched by file size) and resume from where it stopped.<br><br>You can also paste a new address:")
    le = QLineEdit()
    box.layout().addWidget(le, box.layout().rowCount(), 0, 1, box.layout().columnCount())
    box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
    if box.exec() != QMessageBox.Ok:
        return
    if is_url(le.text()):
        ctl.manager.refresh_url(item_id, le.text().strip())
    else:
        ctl.manager.request_refresh(item_id)
        # wait for the browser: nothing else to do here
        QTimer.singleShot(0, lambda: None)

