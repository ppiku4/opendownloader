"""Options, Scheduler and Site Grabber dialogs."""
from __future__ import annotations

import sys
import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, QTime, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
    QPushButton, QRadioButton, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QTimeEdit, QToolButton,
    QVBoxLayout, QWidget,
)

from .. import integration
from ..config import DEFAULT_CATEGORIES
from ..core.base import make_session
from ..core import tools
from ..core.grabber import GrabOptions, grab
from ..core.models import Schedule
from . import icons

if TYPE_CHECKING:
    from .app import Controller

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _path_row(parent: QWidget, line: QLineEdit, file: bool = False) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.addWidget(line, 1)
    b = QToolButton()
    b.setText("…")

    def pick() -> None:
        if file:
            f, _ = QFileDialog.getOpenFileName(parent, "Choose file", line.text())
        else:
            f = QFileDialog.getExistingDirectory(parent, "Choose folder", line.text())
        if f:
            line.setText(f)

    b.clicked.connect(pick)
    h.addWidget(b)
    return w


# ---------------------------------------------------------------------------
class OptionsDialog(QDialog):
    def __init__(self, ctl: "Controller", parent: QWidget | None = None):
        super().__init__(parent)
        self.ctl = ctl
        st = ctl.settings
        self.setWindowTitle("Options")
        self.setWindowIcon(icons.icon("options"))
        tabs = QTabWidget()

        # --- General
        g = QWidget()
        gl = QFormLayout(g)
        self.cb = {}
        for key, text in (
            ("monitor_clipboard", "Monitor clipboard for downloadable links"),
            ("show_add_dialog", "Show 'Download File Info' dialog before starting"),
            ("show_progress_dialog", "Show a progress window when a download starts"),
            ("show_complete_dialog", "Show 'Download complete' dialog"),
            ("show_notifications", "Show tray notifications"),
            ("minimize_to_tray", "Close button minimizes to tray"),
            ("start_minimized", "Start minimized"),
        ):
            c = QCheckBox(text)
            c.setChecked(bool(st.get(key)))
            self.cb[key] = c
            gl.addRow(c)
        self.autostart = QCheckBox("Launch OpenDownloader on system startup")
        self.autostart.setChecked(integration.autostart_enabled())
        gl.addRow(self.autostart)
        self.theme = QComboBox()
        self.theme.addItems(["auto", "light", "dark"])
        self.theme.setCurrentText(st.get("dark_mode"))
        gl.addRow("Theme (restart to apply):", self.theme)
        tabs.addTab(g, "General")

        # --- File types
        ft = QWidget()
        fl = QVBoxLayout(ft)
        fl.addWidget(QLabel("Automatically start downloading the following file types "
                            "(browser + clipboard capture):"))
        self.exts = QPlainTextEdit(" ".join(x.upper() for x in st.get("capture_extensions")))
        fl.addWidget(self.exts)
        fl.addWidget(QLabel("Categories (name → extensions):"))
        self.cats = QTableWidget(0, 2)
        self.cats.setHorizontalHeaderLabels(["Category", "Extensions"])
        self.cats.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.cats.verticalHeader().hide()
        for name, exts in st.get("categories").items():
            r = self.cats.rowCount()
            self.cats.insertRow(r)
            self.cats.setItem(r, 0, QTableWidgetItem(name))
            self.cats.setItem(r, 1, QTableWidgetItem(" ".join(exts)))
        fl.addWidget(self.cats)
        row = QHBoxLayout()
        add = QPushButton("Add category")
        add.clicked.connect(lambda: (self.cats.insertRow(self.cats.rowCount())))
        rm = QPushButton("Remove")
        rm.clicked.connect(lambda: self.cats.removeRow(self.cats.currentRow()))
        reset = QPushButton("Defaults")
        reset.clicked.connect(self._reset_categories)
        for b in (add, rm, reset):
            row.addWidget(b)
        row.addStretch(1)
        fl.addLayout(row)
        tabs.addTab(ft, "File types")

        # --- Save to
        sv = QWidget()
        sl = QFormLayout(sv)
        self.dl_dir = QLineEdit(str(st.download_dir()))
        sl.addRow("Default download folder:", _path_row(self, self.dl_dir))
        self.use_cat = QCheckBox("Put files into category sub-folders (Video, Music, Documents…)")
        self.use_cat.setChecked(bool(st.get("use_category_folders")))
        sl.addRow(self.use_cat)
        tabs.addTab(sv, "Save to")

        # --- Connection
        cn = QWidget()
        cl = QFormLayout(cn)
        self.spins = {}
        for key, label, lo, hi, suffix in (
            ("max_connections", "Max connections per download:", 1, 32, ""),
            ("media_connections", "Parallel segments for video streams:", 1, 16, ""),
            ("default_queue_concurrency", "Default downloads at once in a queue:", 1, 20, ""),
            ("timeout_s", "Connection timeout:", 5, 300, " s"),
            ("max_retries", "Retries per connection:", 0, 1000, ""),
            ("retry_delay_s", "Delay between retries:", 1, 600, " s"),
            ("min_split_size_kb", "Minimum segment size:", 256, 1024 * 1024, " KB"),
        ):
            sp = QSpinBox()
            sp.setRange(lo, hi)
            sp.setSuffix(suffix)
            sp.setValue(int(st.get(key)))
            self.spins[key] = sp
            cl.addRow(label, sp)
        self.limit_on = QCheckBox("Enable global speed limiter")
        self.limit_on.setChecked(bool(st.get("speed_limit_enabled")))
        self.limit = QSpinBox()
        self.limit.setRange(1, 10_000_000)
        self.limit.setSuffix(" KB/s")
        self.limit.setValue(int(st.get("speed_limit_kbps")))
        cl.addRow(self.limit_on)
        cl.addRow("Maximum download speed:", self.limit)
        tabs.addTab(cn, "Connection")

        # --- Proxy / network
        px = QWidget()
        pl = QFormLayout(px)
        self.proxy = QLineEdit(st.get("proxy"))
        self.proxy.setPlaceholderText("http://host:port  or  socks5://user:pass@host:port (empty = system/none)")
        self.ua = QLineEdit(st.get("user_agent"))
        self.tls = QCheckBox("Verify TLS certificates")
        self.tls.setChecked(bool(st.get("verify_tls")))
        pl.addRow("Proxy:", self.proxy)
        pl.addRow("User-Agent:", self.ua)
        pl.addRow(self.tls)
        pl.addRow(QLabel("<small>SOCKS proxies need: pip install requests[socks]</small>"))
        tabs.addTab(px, "Proxy / Network")

        # --- Video
        vd = QWidget()
        vl = QFormLayout(vd)
        tbox = QGroupBox("Helper tools (downloaded on demand)")
        tl = QFormLayout(tbox)
        self.tool_rows: dict[str, tuple[QLabel, QPushButton, QPushButton]] = {}
        for key in ("ffmpeg", "yt-dlp"):
            inf = tools.info(key)
            status = QLabel("Checking…")
            get = QPushButton("Download")
            get.clicked.connect(lambda _=False, k=key: self._tool_download(k))
            rm = QPushButton("Remove")
            rm.clicked.connect(lambda _=False, k=key: self._tool_remove(k))
            row = QHBoxLayout()
            row.addWidget(status, 1)
            row.addWidget(get)
            row.addWidget(rm)
            tl.addRow(f"<b>{inf.name}</b>", row)
            tl.addRow("", QLabel(f"<small>{inf.purpose[0].upper() + inf.purpose[1:]}.</small>"))
            self.tool_rows[key] = (status, get, rm)
        vl.addRow(tbox)
        self.ffmpeg = QLineEdit(st.get("ffmpeg_path"))
        self.ffmpeg.setPlaceholderText("Automatic (downloaded copy, next to the app, or on PATH)")
        vl.addRow("Use a specific ffmpeg:", _path_row(self, self.ffmpeg, file=True))
        self.remux = QCheckBox("Convert downloaded HLS (.ts) streams to .mp4 (no re-encode)")
        self.remux.setChecked(bool(st.get("prefer_mp4_remux")))
        vl.addRow(self.remux)
        vl.addRow(QLabel("<small>DRM-protected streams (Widevine / PlayReady / FairPlay) are not supported.</small>"))
        tabs.addTab(vd, "Video")
        self._refresh_tools()

        # --- Torrents
        from ..core import torrent_engine as te

        tr = QWidget()
        trl = QFormLayout(tr)
        if not te.available():
            trl.addRow(QLabel(f"<i>{te.UNAVAILABLE}</i>"))
        sbox = QGroupBox("When a torrent has finished downloading")
        sgl = QFormLayout(sbox)
        self.seed_none = QRadioButton("Stop right away (don't seed)")
        self.seed_ratio = QRadioButton("Keep sharing until the upload ratio reaches")
        self.seed_time = QRadioButton("Keep sharing for")
        self.seed_ratio_v = QDoubleSpinBox()
        self.seed_ratio_v.setRange(0.1, 100.0)
        self.seed_ratio_v.setSingleStep(0.1)
        self.seed_ratio_v.setValue(float(st.get("torrent_seed_ratio") or 1.0))
        self.seed_min_v = QSpinBox()
        self.seed_min_v.setRange(1, 7 * 24 * 60)
        self.seed_min_v.setSuffix(" minutes")
        self.seed_min_v.setValue(int(st.get("torrent_seed_minutes") or 60))
        mode = st.get("torrent_seed_mode", "none")
        {"ratio": self.seed_ratio, "time": self.seed_time}.get(mode, self.seed_none).setChecked(True)
        sgl.addRow(self.seed_none)
        sgl.addRow(self.seed_ratio, self.seed_ratio_v)
        sgl.addRow(self.seed_time, self.seed_min_v)
        trl.addRow(sbox)
        self.t_up = QSpinBox()
        self.t_up.setRange(0, 10_000_000)
        self.t_up.setSuffix(" KB/s")
        self.t_up.setSpecialValueText("Unlimited")
        self.t_up.setValue(int(st.get("torrent_upload_limit_kbps") or 0))
        trl.addRow("Upload speed limit:", self.t_up)
        self.t_port = QSpinBox()
        self.t_port.setRange(0, 65535)
        self.t_port.setSpecialValueText("Random")
        self.t_port.setValue(int(st.get("torrent_port") or 0))
        trl.addRow("Listening port:", self.t_port)
        self.t_conns = QSpinBox()
        self.t_conns.setRange(10, 2000)
        self.t_conns.setValue(int(st.get("torrent_max_connections") or 200))
        trl.addRow("Maximum connections:", self.t_conns)
        self.t_dht = QCheckBox("Find peers with DHT and local discovery (needed for most magnet links)")
        self.t_dht.setChecked(bool(st.get("torrent_dht", True)))
        trl.addRow(self.t_dht)
        self.t_upnp = QCheckBox("Open the port on my router automatically (UPnP / NAT-PMP)")
        self.t_upnp.setChecked(bool(st.get("torrent_upnp", True)))
        trl.addRow(self.t_upnp)
        self.t_assoc = QCheckBox("Open .torrent files and magnet links with OpenDownloader")
        self._assoc_was = integration.torrent_association_enabled()
        self.t_assoc.setChecked(self._assoc_was)
        self.t_assoc.setVisible(sys.platform == "win32")
        trl.addRow(self.t_assoc)
        trl.addRow(QLabel("<small>While a torrent downloads, other people in the swarm receive pieces from you "
                          "and can see your IP address. That is how BitTorrent works.</small>"))
        tabs.addTab(tr, "Torrents")

        # --- Browser integration
        bi = QWidget()
        bl = QVBoxLayout(bi)
        self.host_status = QLabel()
        bl.addWidget(self.host_status)
        reg = QPushButton("Register browser integration (native messaging host)")
        reg.clicked.connect(self._register)
        bl.addWidget(reg)
        self.extra_ids = QLineEdit(" ".join(st.get("extra_extension_ids", []) or []))
        self.extra_ids.setPlaceholderText("Extra Chromium extension IDs (space separated) - only if you changed the key")
        bl.addWidget(self.extra_ids)
        bl.addWidget(QLabel(
            "<b>Install the extension</b><ul>"
            "<li><b>Chrome / Edge / Brave / Opera / Vivaldi:</b> open <code>chrome://extensions</code>, enable "
            "<i>Developer mode</i>, click <i>Load unpacked</i> and select the <code>extension/dist/chromium</code> folder.</li>"
            "<li><b>Firefox:</b> open <code>about:debugging#/runtime/this-firefox</code> → <i>Load Temporary Add-on</i> → "
            "select <code>extension/dist/firefox/manifest.json</code> (or install the signed .xpi).</li></ul>"
            "Then click <i>Register</i> above once."))
        bl.addStretch(1)
        tabs.addTab(bi, "Browser")
        self._update_host_status()

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(bb)
        self.resize(680, 520)

    def _refresh_tools(self) -> None:
        from .threads import run_async

        st = self.ctl.settings

        def work() -> dict:
            return {k: (tools.version(k, st), tools.installed_path(k) is not None) for k in self.tool_rows}

        def done(res, exc) -> None:
            if exc is not None or not res:
                return
            for key, (ver, managed) in res.items():
                status, get, rm = self.tool_rows[key]
                status.setText(f"✔ Installed ({ver})" if ver else "Not installed")
                get.setText("Update" if managed else "Download")
                get.setEnabled(tools.info(key).supported or not ver)
                rm.setEnabled(managed)

        run_async(work, done)

    def _tool_download(self, key: str) -> None:
        from .tools_ui import download_tool

        if download_tool(self.ctl, key, self):
            self.ctl.settings.update({f"ask_{key.replace('-', '')}": True})
        self._refresh_tools()

    def _tool_remove(self, key: str) -> None:
        tools.remove(key)
        self._refresh_tools()

    def _reset_categories(self) -> None:
        self.cats.setRowCount(0)
        for name, exts in DEFAULT_CATEGORIES.items():
            r = self.cats.rowCount()
            self.cats.insertRow(r)
            self.cats.setItem(r, 0, QTableWidgetItem(name))
            self.cats.setItem(r, 1, QTableWidgetItem(" ".join(exts)))

    def _update_host_status(self) -> None:
        ok = integration.native_host_registered()
        self.host_status.setText("✔ Browser integration is registered." if ok else
                                 "✘ Browser integration is NOT registered yet.")

    def _register(self) -> None:
        try:
            ids = self.extra_ids.text().split()
            where = integration.register_native_host(ids)
            self.ctl.settings["extra_extension_ids"] = ids
            QMessageBox.information(self, "Browser integration",
                                    "Registered for:\n" + "\n".join(where) +
                                    "\n\nRestart your browser if the extension still says it can't connect.")
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Browser integration", f"Failed: {e}")
        self._update_host_status()

    def _save(self) -> None:
        st = self.ctl.settings
        vals = {k: c.isChecked() for k, c in self.cb.items()}
        vals.update({k: sp.value() for k, sp in self.spins.items()})
        cats = {}
        for r in range(self.cats.rowCount()):
            n = self.cats.item(r, 0)
            e = self.cats.item(r, 1)
            if n and n.text().strip():
                cats[n.text().strip()] = [x.lower().lstrip(".") for x in (e.text() if e else "").split()]
        vals.update({
            "capture_extensions": sorted({x.lower().lstrip(".") for x in self.exts.toPlainText().split()}),
            "categories": cats,
            "download_dir": self.dl_dir.text().strip(),
            "use_category_folders": self.use_cat.isChecked(),
            "speed_limit_enabled": self.limit_on.isChecked(),
            "speed_limit_kbps": self.limit.value(),
            "proxy": self.proxy.text().strip(),
            "user_agent": self.ua.text().strip() or st.get("user_agent"),
            "verify_tls": self.tls.isChecked(),
            "ffmpeg_path": self.ffmpeg.text().strip(),
            "prefer_mp4_remux": self.remux.isChecked(),
            "dark_mode": self.theme.currentText(),
            "extra_extension_ids": self.extra_ids.text().split(),
            "torrent_seed_mode": "ratio" if self.seed_ratio.isChecked() else "time" if self.seed_time.isChecked() else "none",
            "torrent_seed_ratio": round(self.seed_ratio_v.value(), 2),
            "torrent_seed_minutes": self.seed_min_v.value(),
            "torrent_upload_limit_kbps": self.t_up.value(),
            "torrent_port": self.t_port.value(),
            "torrent_max_connections": self.t_conns.value(),
            "torrent_dht": self.t_dht.isChecked(),
            "torrent_upnp": self.t_upnp.isChecked(),
        })
        st.update(vals)
        try:
            if self.autostart.isChecked() != integration.autostart_enabled():
                integration.set_autostart(self.autostart.isChecked())
        except OSError as e:
            QMessageBox.warning(self, "Startup", str(e))
        if self.t_assoc.isChecked() != self._assoc_was:
            try:
                integration.set_torrent_association(self.t_assoc.isChecked())
            except OSError as e:
                QMessageBox.warning(self, "Torrents", str(e))
        self.ctl.manager.apply_speed_limit()
        self.ctl.settings_changed()
        self.accept()


# ---------------------------------------------------------------------------
class SchedulerDialog(QDialog):
    def __init__(self, ctl: "Controller", parent: QWidget | None = None, select_qid: int | None = None):
        super().__init__(parent)
        self.ctl = ctl
        self.setWindowTitle("Scheduler")
        self.setWindowIcon(icons.icon("scheduler"))
        self.queues = QListWidget()
        self.queues.currentRowChanged.connect(self._load)
        newq = QPushButton("New queue")
        newq.clicked.connect(self._new)
        delq = QPushButton("Delete")
        delq.clicked.connect(self._delete)
        left = QVBoxLayout()
        left.addWidget(self.queues, 1)
        lr = QHBoxLayout()
        lr.addWidget(newq)
        lr.addWidget(delq)
        left.addLayout(lr)

        # schedule tab
        sch = QWidget()
        sl = QFormLayout(sch)
        self.start_on = QCheckBox("Start download at")
        self.start_time = QTimeEdit()
        self.start_time.setDisplayFormat("HH:mm")
        self.once = QRadioButton("Once")
        self.daily = QRadioButton("Daily on:")
        self.day_cb = [QCheckBox(d) for d in DAYS]
        dayrow = QHBoxLayout()
        for c in self.day_cb:
            dayrow.addWidget(c)
        self.stop_on = QCheckBox("Stop download at")
        self.stop_time = QTimeEdit()
        self.stop_time.setDisplayFormat("HH:mm")
        self.after = QComboBox()
        for key, text in (("none", "Do nothing"), ("exit", "Exit OpenDownloader"),
                          ("sleep", "Put computer to sleep"), ("hibernate", "Hibernate computer"),
                          ("shutdown", "Turn off computer")):
            self.after.addItem(text, key)
        self.concurrent = QSpinBox()
        self.concurrent.setRange(1, 20)
        self.retry = QCheckBox("Retry failed downloads (up to 3 times)")
        r1 = QHBoxLayout()
        r1.addWidget(self.start_on)
        r1.addWidget(self.start_time)
        r1.addStretch(1)
        sl.addRow(r1)
        r2 = QHBoxLayout()
        r2.addWidget(self.once)
        r2.addWidget(self.daily)
        r2.addStretch(1)
        sl.addRow(r2)
        sl.addRow(dayrow)
        r3 = QHBoxLayout()
        r3.addWidget(self.stop_on)
        r3.addWidget(self.stop_time)
        r3.addStretch(1)
        sl.addRow(r3)
        sl.addRow("Download files at the same time:", self.concurrent)
        sl.addRow(self.retry)
        sl.addRow("When done:", self.after)

        # files tab
        fw = QWidget()
        fl = QVBoxLayout(fw)
        self.files = QListWidget()
        fl.addWidget(self.files, 1)
        fr = QHBoxLayout()
        up = QPushButton("Move up")
        up.clicked.connect(lambda: self._move(-1))
        down = QPushButton("Move down")
        down.clicked.connect(lambda: self._move(1))
        rm = QPushButton("Remove from queue")
        rm.clicked.connect(self._remove)
        for b in (up, down, rm):
            fr.addWidget(b)
        fr.addStretch(1)
        fl.addLayout(fr)

        tabs = QTabWidget()
        tabs.addTab(sch, "Schedule")
        tabs.addTab(fw, "Files in the queue")
        start = QPushButton("Start now")
        start.setIcon(icons.icon("startq"))
        start.clicked.connect(self._start_now)
        stop = QPushButton("Stop")
        stop.setIcon(icons.icon("stopq"))
        stop.clicked.connect(self._stop_now)
        apply = QPushButton("Apply")
        apply.clicked.connect(self._apply)
        close = QPushButton("Close")
        close.clicked.connect(self._close)
        btns = QHBoxLayout()
        for b in (start, stop):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(apply)
        btns.addWidget(close)
        right = QVBoxLayout()
        right.addWidget(tabs, 1)
        right.addLayout(btns)
        lay = QHBoxLayout(self)
        lay.addLayout(left, 1)
        lay.addLayout(right, 3)
        self.resize(780, 440)
        self._fill(select_qid)

    def _fill(self, select_qid: int | None = None) -> None:
        self.queues.blockSignals(True)
        self.queues.clear()
        row = 0
        for i, q in enumerate(self.ctl.manager.queues.values()):
            it = QListWidgetItem(icons.icon("folder-queue"), q.name + ("  (running)" if q.running else ""))
            it.setData(Qt.UserRole, q.id)
            self.queues.addItem(it)
            if q.id == select_qid:
                row = i
        self.queues.blockSignals(False)
        self.queues.setCurrentRow(row)
        self._load(row)

    def _q(self):
        it = self.queues.currentItem()
        return self.ctl.manager.queues.get(it.data(Qt.UserRole)) if it else None

    def _load(self, _row: int) -> None:
        q = self._q()
        if q is None:
            return
        s = q.schedule
        self.start_on.setChecked(s.enabled)
        self.start_time.setTime(QTime.fromString(s.start_time, "HH:mm"))
        self.once.setChecked(s.once)
        self.daily.setChecked(not s.once)
        for i, c in enumerate(self.day_cb):
            c.setChecked(i in s.days)
        self.stop_on.setChecked(s.stop_enabled)
        self.stop_time.setTime(QTime.fromString(s.stop_time, "HH:mm"))
        self.after.setCurrentIndex(max(0, self.after.findData(s.after_done)))
        self.concurrent.setValue(q.max_concurrent)
        self.retry.setChecked(q.retry_failed)
        self.files.clear()
        for iid in q.order:
            item = self.ctl.manager.items.get(iid)
            if item:
                li = QListWidgetItem(f"{item.filename}   [{item.status.value}]")
                li.setData(Qt.UserRole, iid)
                self.files.addItem(li)

    def _apply(self) -> None:
        q = self._q()
        if q is None:
            return
        q.schedule = Schedule(
            enabled=self.start_on.isChecked(), start_time=self.start_time.time().toString("HH:mm"),
            stop_enabled=self.stop_on.isChecked(), stop_time=self.stop_time.time().toString("HH:mm"),
            days=[i for i, c in enumerate(self.day_cb) if c.isChecked()], once=self.once.isChecked(),
            after_done=self.after.currentData(), last_started=q.schedule.last_started)
        q.max_concurrent = self.concurrent.value()
        q.retry_failed = self.retry.isChecked()
        self.ctl.manager.save_queue(q)

    def _close(self) -> None:
        self._apply()
        self.accept()

    def _new(self) -> None:
        name, ok = QInputDialog.getText(self, "New queue", "Queue name:")
        if ok and name.strip():
            q = self.ctl.manager.create_queue(name.strip())
            self._fill(q.id)
            self.ctl.refresh_tree()

    def _delete(self) -> None:
        q = self._q()
        if q is None or q.id == self.ctl.manager.main_queue.id:
            QMessageBox.information(self, "Scheduler", "The main queue can't be deleted.")
            return
        self.ctl.manager.delete_queue(q.id)
        self._fill()
        self.ctl.refresh_tree()

    def _move(self, delta: int) -> None:
        it = self.files.currentItem()
        if it is None:
            return
        iid = it.data(Qt.UserRole)
        self.ctl.manager.move_in_queue(iid, delta)
        row = self.files.currentRow()
        self._load(self.queues.currentRow())
        self.files.setCurrentRow(max(0, min(self.files.count() - 1, row + delta)))

    def _remove(self) -> None:
        it = self.files.currentItem()
        if it:
            self.ctl.manager.remove_from_queue([it.data(Qt.UserRole)])
            self._load(self.queues.currentRow())

    def _start_now(self) -> None:
        q = self._q()
        if q:
            self._apply()
            self.ctl.manager.start_queue(q.id)
            self._fill(q.id)

    def _stop_now(self) -> None:
        q = self._q()
        if q:
            self.ctl.manager.stop_queue(q.id)
            self._fill(q.id)


# ---------------------------------------------------------------------------
class _GrabRelay(QObject):
    progress = Signal(int, int, str)
    finished = Signal(list)


class GrabberDialog(QDialog):
    def __init__(self, ctl: "Controller", parent: QWidget | None = None):
        super().__init__(parent)
        self.ctl = ctl
        self.setWindowTitle("Site Grabber")
        self.setWindowIcon(icons.icon("grabber"))
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://example.com/gallery/")
        self.depth = QSpinBox()
        self.depth.setRange(0, 10)
        self.depth.setValue(1)
        self.same = QCheckBox("Stay on the same site")
        self.same.setChecked(True)
        self.exts = QLineEdit("jpg jpeg png gif webp mp4 webm mp3 zip rar 7z pdf")
        self.inc = QLineEdit()
        self.inc.setPlaceholderText("optional regex the file URL must match")
        self.exc = QLineEdit()
        self.exc.setPlaceholderText("optional regex to skip")
        self.maxp = QSpinBox()
        self.maxp.setRange(1, 100000)
        self.maxp.setValue(300)
        form = QFormLayout()
        form.addRow("Start page:", self.url)
        form.addRow("Explore links depth:", self.depth)
        form.addRow(self.same)
        form.addRow("File types:", self.exts)
        form.addRow("Include filter:", self.inc)
        form.addRow("Exclude filter:", self.exc)
        form.addRow("Max pages to explore:", self.maxp)
        self.status = QLabel("")
        self.go = QPushButton("Start exploring")
        self.go.setIcon(icons.icon("grabber"))
        self.go.clicked.connect(self._start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        btns = QHBoxLayout()
        btns.addWidget(self.status, 1)
        btns.addWidget(self.go)
        btns.addWidget(self.stop_btn)
        btns.addWidget(close)
        box = QGroupBox("Project")
        box.setLayout(form)
        lay = QVBoxLayout(self)
        lay.addWidget(box)
        lay.addLayout(btns)
        self.resize(620, 330)
        self.stop_ev = threading.Event()
        self.relay = _GrabRelay()
        self.relay.progress.connect(lambda p, f, u: self.status.setText(f"Pages: {p}   files found: {f}\n{u[:80]}"))
        self.relay.finished.connect(self._done)
        self.stop_btn.clicked.connect(self.stop_ev.set)

    def _start(self) -> None:
        url = self.url.text().strip()
        if not url.startswith(("http://", "https://")):
            QMessageBox.warning(self, "Site Grabber", "Enter a http(s) address.")
            return
        opts = GrabOptions(start_url=url, depth=self.depth.value(), same_domain=self.same.isChecked(),
                           extensions=tuple(self.exts.text().split()), include_regex=self.inc.text().strip(),
                           exclude_regex=self.exc.text().strip(), max_pages=self.maxp.value())
        self.stop_ev.clear()
        self.go.setEnabled(False)
        self.stop_btn.setEnabled(True)
        session = make_session(self.ctl.settings)

        def work() -> None:
            try:
                res = grab(opts, session, self.stop_ev, self.relay.progress.emit)
            except Exception as e:  # noqa: BLE001
                res = [("__error__", str(e))]
            self.relay.finished.emit(res)

        threading.Thread(target=work, daemon=True).start()

    def _done(self, res: list) -> None:
        self.go.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if res and res[0][0] == "__error__":
            QMessageBox.warning(self, "Site Grabber", res[0][1])
            return
        if not res:
            self.status.setText("No matching files found.")
            return
        from .dialogs_download import LinksDialog

        self.status.setText(f"Found {len(res)} files.")
        LinksDialog(self.ctl, [u for u, _ in res], referer=self.url.text().strip(), parent=self,
                    title=f"Site Grabber - {len(res)} files").exec()
