"""Application controller: wires the manager, IPC, tray, clipboard and dialogs."""
from __future__ import annotations

import json
import logging
import sys
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QInputDialog, QMenu, QMessageBox, QSystemTrayIcon, QTabWidget

from .. import APP_NAME, __version__, integration, paths
from ..config import Settings
from ..core.manager import DownloadManager, system_action
from ..core.models import Kind
from ..ipc import IpcServer
from ..util import cookie_header, is_url
from . import icons
from .dialogs_download import (
    AddUrlDialog, BatchDialog, CompleteDialog, DownloadInfoDialog, LinksDialog, PageVideoDialog, ProgressDialog,
    QualityDialog,
)
from .dialogs_tools import GrabberDialog, OptionsDialog, SchedulerDialog
from .dialogs_torrent import TorrentDialog
from .main_window import MainWindow

log = logging.getLogger(__name__)
MEDIA_EXT = (".m3u8", ".mpd")


class _Bridge(QObject):
    message = Signal(dict)


class Controller:
    def __init__(self, app: QApplication, settings: Settings, minimized: bool = False):
        self.app = app
        self.settings = settings
        self.quitting = False
        self._dialogs: set[QDialog] = set()
        self._progress: dict[int, ProgressDialog] = {}
        self.tool_prompts: dict[str, list] = {}
        self._notified_once = False
        self._last_clip = ""
        self.build = integration.build_id()
        self.manager = DownloadManager(settings)
        self._resume_after_restart()
        self.window = MainWindow(self)
        self.tray: QSystemTrayIcon | None = None
        self._setup_tray()

        self.bridge = _Bridge()
        self.bridge.message.connect(self._on_message, Qt.QueuedConnection)
        self.ipc = IpcServer(self._ipc_handler)
        self.ipc.start()
        integration.write_launch_info()
        try:
            if not integration.native_host_registered():
                integration.register_native_host(settings.get("extra_extension_ids"))
        except Exception:  # noqa: BLE001
            log.exception("native host registration failed")

        QGuiApplication.clipboard().dataChanged.connect(self._clipboard_changed)
        self.timer = QTimer()
        self.timer.timeout.connect(self._tick)
        self.timer.start(500)
        if not (minimized or settings.get("start_minimized")) or self.tray is None:
            self.window.show()

    # ------------------------------------------------------------------ tray
    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(icons.icon("app"))
        self.tray.setToolTip(APP_NAME)
        m = QMenu()
        m.addAction("Open OpenDownloader", self.show_window)
        m.addAction("Add URL…", self.add_url_dialog)
        m.addSeparator()
        m.addAction("Stop all downloads", self.manager.pause_all)
        self._tray_limiter = m.addAction("Speed limiter")
        self._tray_limiter.setCheckable(True)
        self._tray_limiter.setChecked(bool(self.settings.get("speed_limit_enabled")))
        self._tray_limiter.toggled.connect(self.toggle_limiter)
        m.addSeparator()
        m.addAction("Exit", self.quit)
        self.tray.setContextMenu(m)
        self._tray_menu = m
        self.tray.activated.connect(
            lambda r: self.show_window() if r in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.tray.show()

    def notify(self, title: str, text: str, once: bool = False) -> None:
        if once:
            if self._notified_once:
                return
            self._notified_once = True
        if self.tray is not None and self.settings.get("show_notifications"):
            self.tray.showMessage(title, text, icons.icon("app"), 5000)

    def show_window(self) -> None:
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def toggle_limiter(self, on: bool) -> None:
        self.settings.update({"speed_limit_enabled": bool(on)})
        self.manager.apply_speed_limit()
        for a in (self.window.a_limiter, getattr(self, "_tray_limiter", None)):
            if a is not None and a.isChecked() != bool(on):
                a.blockSignals(True)
                a.setChecked(bool(on))
                a.blockSignals(False)

    # ------------------------------------------------------------------ dialogs
    def _show(self, dlg: QDialog) -> None:
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        self._dialogs.add(dlg)
        dlg.destroyed.connect(lambda *_: self._dialogs.discard(dlg))
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def add_url_dialog(self) -> None:
        from ..core import torrent_engine as te

        d = AddUrlDialog(self, self.window)
        if d.exec() == QDialog.Accepted:
            req = d.request()
            if te.is_magnet(req["url"]):
                self.request_torrent(req)
                return
            if not is_url(req["url"]):
                QMessageBox.warning(self.window, APP_NAME, "Please enter a valid http(s) or ftp address.")
                return
            self.request_download(req, ask=True)

    def page_video_dialog(self) -> None:
        url, ok = QInputDialog.getText(self.window, "Download video from web page",
                                       "Page address (e.g. a YouTube or Vimeo link):")
        if ok and is_url(url):
            self.request_page({"url": url.strip()})

    def request_page(self, req: dict[str, Any]) -> None:
        """A video on a web page (YouTube, Vimeo, ...): make sure yt-dlp is there,
        then let the user pick a quality."""
        from ..core import tools
        from .tools_ui import ensure_tool

        def ready(ok: bool) -> None:
            if ok:
                self._show(PageVideoDialog(self, req))

        if tools.available("yt-dlp", self.settings):
            ready(True)
        else:
            ensure_tool(self, "yt-dlp", "To download videos from pages like YouTube, OpenDownloader needs yt-dlp.",
                        ready, required=True, parent=self.window if self.window.isVisible() else None)

    # ------------------------------------------------------------------ torrents
    def add_torrent_file_dialog(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self.window, "Open torrent file", "", "Torrent files (*.torrent);;All files (*)")
        if f:
            self.request_torrent({"url": f})

    def request_torrent(self, req: dict[str, Any]) -> None:
        """A magnet link, a local .torrent file or a .torrent link: show the 'Add torrent' dialog."""
        from pathlib import Path

        from ..core import torrent_engine as te
        from ..core.base import make_session
        from ..core.models import DownloadItem
        from .threads import run_async

        parent = self.window if self.window.isVisible() else None
        if not te.available():
            QMessageBox.warning(parent, APP_NAME, te.UNAVAILABLE)
            return
        url = (req.get("url") or "").strip()

        def show(source: dict[str, Any]) -> None:
            try:
                self._show(TorrentDialog(self, source))
            except ValueError as e:
                QMessageBox.warning(parent, APP_NAME, str(e))

        if te.is_magnet(url):
            show({"magnet": url})
            return
        if not is_url(url):
            p = Path(url[8:] if url.lower().startswith("file:///") else url)
            try:
                data = p.read_bytes()[: te.MAX_TORRENT_FILE + 1]
            except OSError as e:
                QMessageBox.warning(parent, APP_NAME, f"Can't open the torrent file:\n{e}")
                return
            show({"data": data, "url": str(p)})
            return
        headers = self.headers_for(req)
        settings = self.settings

        def work() -> bytes:
            s = make_session(settings, DownloadItem(url=url, headers=headers, referer=req.get("referer", "")))
            try:
                r = s.get(url, timeout=30, stream=True)
                r.raise_for_status()
                buf = b""
                for chunk in r.iter_content(64 * 1024):
                    buf += chunk
                    if len(buf) > te.MAX_TORRENT_FILE:
                        raise ValueError("This file is too big to be a .torrent file.")
                return buf
            finally:
                s.close()

        def done(data, exc) -> None:
            if exc is not None:
                QMessageBox.warning(parent, APP_NAME, f"Couldn't get the torrent file:\n{exc}")
                return
            show({"data": data, "url": url})

        run_async(work, done)

    def add_torrent(self, *, url: str, data: bytes, name: str, save_dir: str, priorities: list[int] | None,
                    size: int, keys: list[str], start: bool, queue_id: int | None) -> None:
        from ..core import torrent_engine as te

        media: dict[str, Any] = {"info_hashes": list(keys)}
        if priorities is not None:
            media["priorities"] = priorities
        item = self.manager.add(url, filename=name, save_dir=save_dir, kind=Kind.TORRENT.value, media=media,
                                start=False, queue_id=queue_id, size=size)
        if data:
            try:
                te.torrent_path(item.id).write_bytes(data)  # type: ignore[arg-type]
            except OSError as e:
                QMessageBox.warning(self.window, APP_NAME, f"Couldn't save the torrent file:\n{e}")
        if start:
            self.manager.start(item.id)  # type: ignore[arg-type]
            if self.settings.get("show_progress_dialog", True):
                self.show_progress(item.id)  # type: ignore[arg-type]

    def stop_seeding(self, ids: list[int]) -> None:
        from ..core import torrent_engine as te

        for iid in ids:
            te.stop_seeding(iid)

    def batch_dialog(self) -> None:
        BatchDialog(self, self.window).exec()

    def grabber_dialog(self) -> None:
        self._show(GrabberDialog(self, self.window))

    def scheduler_dialog(self, qid: int | None = None) -> None:
        SchedulerDialog(self, self.window, qid).exec()
        self.refresh_tree()

    def options_dialog(self, tab: str | None = None) -> None:
        d = OptionsDialog(self, self.window)
        if tab:
            tw = d.findChild(QTabWidget)
            for i in range(tw.count()):
                if tw.tabText(i) == tab:
                    tw.setCurrentIndex(i)
        d.exec()

    def show_progress(self, item_id: int) -> None:
        dlg = self._progress.get(item_id)
        if dlg is None:
            dlg = ProgressDialog(self, item_id)
            self._progress[item_id] = dlg
            dlg.destroyed.connect(lambda *_: self._progress.pop(item_id, None))
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def refresh_tree(self) -> None:
        self.window.refresh_tree()

    def settings_changed(self) -> None:
        self.refresh_tree()

    # ------------------------------------------------------------------ adding
    @staticmethod
    def headers_for(req: dict[str, Any]) -> dict[str, str]:
        h = {str(k): str(v) for k, v in (req.get("headers") or {}).items()}
        ck = cookie_header(req.get("cookies"))
        if ck:
            h["Cookie"] = ck
        if req.get("userAgent"):
            h["User-Agent"] = req["userAgent"]
        return h

    def add_download(self, req: dict[str, Any], start: bool = True, queue_id: int | None = None):
        """Add a download. If it needs a helper tool that isn't installed yet
        (yt-dlp, ffmpeg), offer to download it first; the download is added
        once the user has answered (returns None in that case)."""
        from ..core import tools
        from .tools_ui import ensure_tool

        kind = req.get("kind") or Kind.HTTP.value
        media = req.get("media") or {}
        if kind == Kind.YTDLP.value and not tools.available("yt-dlp", self.settings):
            def after_ytdlp(ok: bool) -> None:
                if ok:
                    self.add_download(req, start, queue_id)
            ensure_tool(self, "yt-dlp", "To download videos from web pages like this one, OpenDownloader needs yt-dlp.",
                        after_ytdlp, required=True, parent=self.window if self.window.isVisible() else None)
            return None
        needs_merge = (kind == Kind.YTDLP.value
                       or (kind == Kind.DASH.value and media.get("video") != "" and media.get("audio") != "")
                       or (kind == Kind.HLS.value and bool(media.get("audio"))))
        if needs_merge and not req.get("_tools_checked") and not tools.available("ffmpeg", self.settings):
            reason = ("For the best quality, sites like YouTube send video and audio separately. ffmpeg joins them "
                      "into one MP4 file. Without it, a lower-quality single file is downloaded instead."
                      if kind == Kind.YTDLP.value else
                      "This video comes as separate video and audio streams. To combine them into one MP4 file, "
                      "OpenDownloader needs ffmpeg. Without it, video and audio are saved as two separate files.")
            ensure_tool(self, "ffmpeg", reason,
                        lambda _ok: self.add_download({**req, "_tools_checked": True}, start, queue_id),
                        required=False, parent=self.window if self.window.isVisible() else None)
            return None
        return self._add_now(req, start, queue_id)

    def _add_now(self, req: dict[str, Any], start: bool, queue_id: int | None):
        kind = req.get("kind") or Kind.HTTP.value
        auth = req.get("auth")
        if req.get("guessedExt") and kind == Kind.HTTP.value:
            req = {**req, "media": {**(req.get("media") or {}), "fix_ext": True}}
        item = self.manager.add(
            req["url"], filename=req.get("filename", "") if req.get("filename_locked", True) else "",
            save_dir=req.get("save_dir", ""), referer=req.get("referer", ""), headers=self.headers_for(req),
            kind=kind, media=req.get("media"), start=start, queue_id=queue_id,
            description=req.get("description", ""), category=req.get("category", ""),
            size=int(req.get("fileSize") or -1), auth=tuple(auth) if auth else None)
        if not req.get("filename_locked", True) and req.get("filename"):
            item.filename = req["filename"]
        if start and self.settings.get("show_progress_dialog", True):
            self.show_progress(item.id)  # type: ignore[arg-type]
        return item

    def request_download(self, req: dict[str, Any], ask: bool | None = None) -> None:
        from ..core import torrent_engine as te

        url = req.get("url", "")
        if te.is_magnet(url) or (te.available() and req.get("kind") in (None, "", "http")
                                 and te.is_torrent_link(url, req.get("filename", ""), req.get("mime", ""))):
            self.request_torrent(req)
            return
        low = url.split("?")[0].lower()
        kind = req.get("kind")
        if kind in ("hls", "dash") or low.endswith(MEDIA_EXT):
            if req.get("media"):
                self.add_download({**req, "filename": req.get("filename") or ("audio.m4a" if req.get("audio") else "video.mp4")},
                                  start=True)
            else:
                self._show(QualityDialog(self, req))
            return
        if kind == Kind.YTDLP.value and not req.get("media"):
            self.request_page(req)
            return
        if ask is None:
            ask = bool(self.settings.get("show_add_dialog"))
        if ask:
            self._show(DownloadInfoDialog(self, req))
        else:
            self.add_download({**req, "filename_locked": bool(req.get("filename"))})

    # ------------------------------------------------------------------ IPC
    def _ipc_handler(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Runs on an IPC thread. Keep it quick; UI work goes through the bridge."""
        t = msg.get("type")
        if t == "ping":
            return {"ok": True, "version": __version__, "app": APP_NAME, "build": self.build,
                    "captureExtensions": self.settings.get("capture_extensions")}
        if t == "quit":
            self.bridge.message.emit(msg)
            return {"ok": True}
        if t == "download":
            req = msg.get("data") or {}
            if not req.get("url"):
                return {"ok": False, "error": "no url"}
            item = self.manager.try_refresh(req["url"], int(req.get("fileSize") or -1),
                                            req.get("filename", ""), self.headers_for(req), req.get("referer", ""))
            if item is not None:
                self.bridge.message.emit({"type": "refreshed", "id": item.id})
                return {"ok": True, "refreshed": True}
        if t in ("download", "media", "links", "page", "show", "cli"):
            self.bridge.message.emit(msg)
            return {"ok": True}
        return {"ok": False, "error": f"unknown message type {t!r}"}

    def _on_message(self, msg: dict[str, Any]) -> None:
        t = msg.get("type")
        d = msg.get("data") or {}
        if t == "download":
            self.request_download(d)
        elif t == "media":
            self.request_download({**d, "kind": d.get("kind") if d.get("kind") in ("hls", "dash") else None})
        elif t == "links":
            self._show(LinksDialog(self, d.get("urls", []), d.get("referer", ""),
                                   self.headers_for({"cookies": d.get("cookies"), "userAgent": d.get("userAgent")}),
                                   title="Download all links"))
        elif t == "page":
            if d.get("url"):
                self.request_page({"url": d["url"], "referer": d.get("referer", ""), "title": d.get("title", ""),
                                   "preferHeight": d.get("preferHeight"), "preferAudio": bool(d.get("preferAudio"))})
        elif t == "refreshed":
            it = self.manager.items.get(msg.get("id"))
            if it:
                self.notify("Download address refreshed", it.filename)
        elif t == "show":
            self.show_window()
        elif t == "quit":
            self.quit(force=True, resume_later=bool(d.get("resume")))
        elif t == "cli":
            self.handle_cli(d)

    def handle_cli(self, args: dict[str, Any]) -> None:
        if args.get("url"):
            req = {"url": args["url"], "filename": args.get("filename") or "", "save_dir": args.get("dir") or "",
                   "referer": args.get("referer") or ""}
            if args.get("queue"):
                self.add_download({**req, "filename_locked": bool(req["filename"])}, start=False,
                                  queue_id=self.manager.main_queue.id)
            else:
                self.request_download(req, ask=not args.get("silent"))
        elif not args.get("minimized"):
            self.show_window()

    # ------------------------------------------------------------------ clipboard
    def _clipboard_changed(self) -> None:
        if not self.settings.get("monitor_clipboard"):
            return
        text = QGuiApplication.clipboard().text().strip()
        if not text or text == self._last_clip or "\n" in text:
            return
        if text.lower().startswith("magnet:?"):
            self._last_clip = text
            self.request_torrent({"url": text})
            return
        if not is_url(text):
            return
        self._last_clip = text
        path = text.split("?")[0].lower()
        ext = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
        if ext in self.settings.get("capture_extensions") or path.endswith(MEDIA_EXT):
            self.request_download({"url": text}, ask=True)

    # ------------------------------------------------------------------ tick
    def _tick(self) -> None:
        self.window.refresh()
        for dlg in list(self._progress.values()):
            if dlg.isVisible():
                dlg.refresh()
        tree_dirty = False
        while not self.manager.events.empty():
            ev, val = self.manager.events.get()
            if ev == "complete":
                it = self.manager.items.get(val)
                if it is None:
                    continue
                dlg = self._progress.get(val)
                if dlg is not None:
                    dlg.close()
                if self.settings.get("show_complete_dialog"):
                    self._show(CompleteDialog(self, it))
                else:
                    self.notify("Download complete", it.filename)
            elif ev == "error":
                it = self.manager.items.get(val)
                if it:
                    self.notify("Download failed", f"{it.filename}\n{it.error}")
            elif ev in ("queue_started", "queue_stopped"):
                tree_dirty = True
            elif ev == "queue_done":
                tree_dirty = True
                qid, action = val
                q = self.manager.queues.get(qid)
                self.notify("Queue finished", q.name if q else "")
                if action and action != "none":
                    self._after_queue(action)
        if tree_dirty:
            self.refresh_tree()
        if self.tray is not None:
            spd = self.manager.total_speed()
            from ..util import human_speed

            self.tray.setToolTip(f"{APP_NAME}\n{len(self.manager.workers)} active  {human_speed(spd)}")

    def _after_queue(self, action: str) -> None:
        box = QMessageBox(QMessageBox.Warning, APP_NAME,
                          f"All downloads in the queue are finished.\nAction: {action} in 30 seconds.",
                          QMessageBox.Cancel)
        box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        timer = QTimer(box)
        timer.setSingleShot(True)
        timer.timeout.connect(box.accept)
        timer.start(30_000)
        if box.exec() == QMessageBox.Cancel and timer.isActive():
            return
        if action == "exit":
            self.quit()
        else:
            system_action(action)

    # ------------------------------------------------------------------ quit
    def _resume_after_restart(self) -> None:
        """Restart downloads that were running when an older copy was replaced."""
        f = paths.app_data_dir() / "resume.json"
        try:
            ids = json.loads(f.read_text("utf-8"))
            f.unlink()
        except (OSError, ValueError):
            return
        for iid in ids:
            if iid in self.manager.items:
                self.manager.start(iid)

    def quit(self, force: bool = False, resume_later: bool = False) -> None:
        if self.quitting:
            return
        active = [i for i in self.manager.items.values() if i.status.active]
        if resume_later and active:
            try:
                (paths.app_data_dir() / "resume.json").write_text(json.dumps([i.id for i in active]), "utf-8")
            except OSError:
                pass
        if active and self.window.isVisible() and not force:
            if QMessageBox.question(self.window, APP_NAME,
                                    f"{len(active)} download(s) are running. They will be paused. Exit?") != QMessageBox.Yes:
                return
        self.quitting = True
        self.timer.stop()
        self.ipc.close()
        self.manager.shutdown()
        if self.tray is not None:
            self.tray.hide()
        self.settings.save()
        self.app.quit()


def apply_theme(app: QApplication, mode: str) -> None:
    app.setStyle("Fusion")
    dark = mode == "dark"
    if mode == "auto":
        try:
            dark = QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
        except AttributeError:
            dark = False
    if not dark:
        return
    p = QPalette()
    base, alt, text = QColor(32, 33, 36), QColor(45, 46, 50), QColor(230, 230, 230)
    p.setColor(QPalette.Window, alt)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, base)
    p.setColor(QPalette.AlternateBase, alt)
    p.setColor(QPalette.ToolTipBase, alt)
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, alt)
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.Highlight, QColor(31, 122, 224))
    p.setColor(QPalette.HighlightedText, QColor("white"))
    p.setColor(QPalette.Link, QColor(90, 160, 255))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor(120, 120, 120))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(120, 120, 120))
    app.setPalette(p)


def run_gui(args: dict[str, Any]) -> int:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("OpenDownloader.App")
        except Exception:  # noqa: BLE001
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(icons.icon("app"))
    settings = Settings()
    apply_theme(app, settings.get("dark_mode"))
    ctl = Controller(app, settings, minimized=bool(args.get("minimized")))
    if args.get("url"):
        QTimer.singleShot(300, lambda: ctl.handle_cli(args))
    app.aboutToQuit.connect(lambda: None if ctl.quitting else ctl.quit())
    return app.exec()

