"""DownloadManager: owns all downloads, worker threads, queues and the scheduler.

It is UI-agnostic; the Qt layer polls `items` and drains `events`.
"""
from __future__ import annotations

import logging
import os
import queue
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from .. import paths
from ..config import Settings
from ..util import guess_filename, sanitize_filename, unique_path
from .base import BaseDownloader, EngineContext
from .ftp_engine import FtpDownloader
from .http_engine import HttpDownloader
from .media_engine import MediaDownloader, YtDlpDownloader
from .models import DownloadItem, DownloadQueue, Kind, Status
from .ratelimit import RateLimiter
from .store import Store
from . import torrent_engine

log = logging.getLogger(__name__)


class DownloadManager:
    def __init__(self, settings: Settings, store: Store | None = None, autostart_thread: bool = True):
        self.settings = settings
        self.store = store or Store()
        self.lock = threading.RLock()
        self.items: dict[int, DownloadItem] = self.store.load_items()
        for it in self.items.values():
            if it.status.active:
                it.status = Status.PAUSED
        self.queues: dict[int, DownloadQueue] = self.store.load_queues()
        if not self.queues:
            q = DownloadQueue(name="Main queue", max_concurrent=int(settings.get("default_queue_concurrency", 2)))
            self.store.save_queue(q)
            self.queues[q.id] = q  # type: ignore[index]
        for q in self.queues.values():
            q.running = False
            q.order = [i for i in q.order if i in self.items]
        self.workers: dict[int, BaseDownloader] = {}
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.limiter = RateLimiter()
        self.apply_speed_limit()
        self.ctx = EngineContext(settings=settings, global_limiter=self.limiter, persist=self._persist)
        self._queue_retries: dict[int, int] = {}
        self._last_sched_minute = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if autostart_thread:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="manager")
            self._thread.start()

    # ------------------------------------------------------------------ misc
    def apply_speed_limit(self) -> None:
        st = self.settings
        rate = int(st.get("speed_limit_kbps", 0)) * 1024 if st.get("speed_limit_enabled") else 0
        self.limiter.set_rate(rate)
        torrent_engine.apply_settings(st)

    def _persist(self, item: DownloadItem) -> None:
        if item.id is not None and item.id in self.items:
            self.store.save_item(item)

    @property
    def main_queue(self) -> DownloadQueue:
        return next(iter(self.queues.values()))

    # ------------------------------------------------------------------ add
    def add(self, url: str, *, filename: str = "", save_dir: str = "", referer: str = "",
            headers: dict[str, str] | None = None, kind: str = Kind.HTTP.value,
            media: dict[str, Any] | None = None, start: bool = True, queue_id: int | None = None,
            description: str = "", category: str = "", size: int = -1,
            auth: tuple[str, str] | None = None, max_connections: int = 0) -> DownloadItem:
        filename_locked = bool(filename)
        is_torrent = kind == Kind.TORRENT.value
        if not filename:
            filename = ("torrent" if is_torrent else
                        guess_filename(url) if kind == Kind.HTTP.value else "video.mp4")
        filename = sanitize_filename(filename)
        category = category or self.settings.category_for(filename)
        if not save_dir:
            save_dir = str(self.settings.folder_for(filename, category))
        Path(save_dir).mkdir(parents=True, exist_ok=True)
        # A torrent writes to its own name inside save_dir; existing data there is re-checked and reused.
        if not is_torrent and (Path(save_dir) / filename).exists():
            filename = unique_path(Path(save_dir) / filename).name
        item = DownloadItem(url=url, filename=filename, save_dir=save_dir, kind=kind,
                            referer=referer, headers=dict(headers or {}), media=dict(media or {}),
                            category=category, description=description, size=size,
                            filename_locked=filename_locked, max_connections=max_connections)
        if auth:
            item.auth_user, item.auth_pass = auth
        with self.lock:
            self.store.insert_item(item)
            if is_torrent:
                torrent_engine.forget(item.id)   # leftovers of an older download with the same number
            self.items[item.id] = item  # type: ignore[index]
            if queue_id is not None and queue_id in self.queues:
                self.add_to_queue([item.id], queue_id)  # type: ignore[list-item]
        self.events.put(("added", item.id))
        if start:
            self.start(item.id)  # type: ignore[arg-type]
        return item

    # ------------------------------------------------------------------ control
    def _make_worker(self, item: DownloadItem) -> BaseDownloader:
        if item.kind in (Kind.HLS.value, Kind.DASH.value):
            return MediaDownloader(item, self.ctx)
        if item.kind == Kind.YTDLP.value:
            return YtDlpDownloader(item, self.ctx)
        if item.kind == Kind.TORRENT.value:
            return torrent_engine.TorrentDownloader(item, self.ctx)
        if urlsplit(item.url).scheme == "ftp":
            return FtpDownloader(item, self.ctx)
        return HttpDownloader(item, self.ctx)

    def start(self, item_id: int) -> bool:
        with self.lock:
            item = self.items.get(item_id)
            if item is None or item.status == Status.COMPLETE:
                return False
            old = self.workers.get(item_id)
            if old is not None:
                if old.is_alive() and not old.stopping:
                    return False
                old.join(5)
                if old.is_alive():
                    return False
                del self.workers[item_id]
            if item.awaiting_refresh:
                return False
            w = self._make_worker(item)
            self.workers[item_id] = w
            item.status = Status.CONNECTING
            w.start()
        self.events.put(("started", item_id))
        return True

    def pause(self, item_id: int) -> None:
        with self.lock:
            w = self.workers.get(item_id)
            item = self.items.get(item_id)
        if w is not None:
            w.stop()
        elif item is not None and item.status == Status.QUEUED:
            item.status = Status.PAUSED
            self._persist(item)

    def pause_all(self) -> None:
        for q in self.queues.values():
            q.running = False
        for iid in list(self.workers):
            self.pause(iid)

    def resume_all(self) -> None:
        for iid, it in list(self.items.items()):
            if it.status in (Status.PAUSED, Status.QUEUED, Status.ERROR):
                self.start(iid)

    def restart(self, item_id: int) -> None:
        """Discard partial data and download again from zero."""
        self.pause(item_id)
        self._wait_worker(item_id)
        item = self.items[item_id]
        if item.kind == Kind.TORRENT.value:
            torrent_engine.stop_seeding(item_id)
            torrent_engine.delete_data(item)
            try:
                torrent_engine.resume_path(item_id).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            item.part_path.unlink(missing_ok=True)
        except OSError:
            pass
        item.segments, item.downloaded, item.resumable = [], 0, False
        item.status, item.error = Status.QUEUED, ""
        shutil.rmtree(paths.temp_dir() / f"media_{item_id}", ignore_errors=True)
        self.start(item_id)

    def _wait_worker(self, item_id: int, timeout: float = 10) -> None:
        w = self.workers.get(item_id)
        if w is not None:
            w.join(timeout)
            with self.lock:
                self.workers.pop(item_id, None)

    def remove(self, ids: Iterable[int], delete_files: bool = False) -> None:
        for iid in list(ids):
            self.pause(iid)
            self._wait_worker(iid, 5)
            with self.lock:
                item = self.items.pop(iid, None)
                for q in self.queues.values():
                    if iid in q.order:
                        q.order.remove(iid)
                        self.store.save_queue(q)
            if item is None:
                continue
            self.store.delete_item(iid)
            if item.kind == Kind.TORRENT.value:
                torrent_engine.stop_seeding(iid)
                # unfinished torrent data is always removed (like a .part file); finished files only on request
                if delete_files or item.status != Status.COMPLETE:
                    torrent_engine.delete_data(item)
                torrent_engine.forget(iid)
                self.events.put(("removed", iid))
                continue
            try:
                item.part_path.unlink(missing_ok=True)
                shutil.rmtree(paths.temp_dir() / f"media_{iid}", ignore_errors=True)
                if delete_files and item.status == Status.COMPLETE:
                    item.path.unlink(missing_ok=True)
            except OSError:
                log.warning("could not delete files for %s", iid)
            self.events.put(("removed", iid))

    def set_item_speed_limit(self, item_id: int, kbps: int) -> None:
        item = self.items[item_id]
        item.speed_limit_kbps = kbps
        w = self.workers.get(item_id)
        if w:
            w.set_speed_limit(kbps)
        self._persist(item)

    def update_item(self, item_id: int, **fields: Any) -> None:
        item = self.items[item_id]
        for k, v in fields.items():
            setattr(item, k, v)
        self._persist(item)

    # ----------------------------------------------------- refresh address
    def refresh_url(self, item_id: int, new_url: str, headers: dict[str, str] | None = None,
                    referer: str | None = None) -> None:
        item = self.items[item_id]
        self.pause(item_id)
        self._wait_worker(item_id)
        item.url = new_url
        if headers is not None:
            item.headers = dict(headers)
        if referer is not None:
            item.referer = referer
        item.awaiting_refresh = False
        item.error = ""
        self._persist(item)
        self.start(item_id)

    def request_refresh(self, item_id: int) -> None:
        """Wait for the next matching browser download to supply a fresh link."""
        self.pause(item_id)
        item = self.items[item_id]
        item.awaiting_refresh = True
        self._persist(item)

    def try_refresh(self, url: str, size: int, filename: str, headers: dict[str, str] | None = None,
                    referer: str = "") -> DownloadItem | None:
        for item in self.items.values():
            if not item.awaiting_refresh:
                continue
            same_size = size > 0 and item.size > 0 and size == item.size
            same_name = filename and Path(filename).stem.lower() in item.filename.lower()
            if same_size or (item.size <= 0 and same_name):
                self.refresh_url(item.id, url, headers, referer)  # type: ignore[arg-type]
                return item
        return None

    # ------------------------------------------------------------------ queues
    def create_queue(self, name: str) -> DownloadQueue:
        q = DownloadQueue(name=name, max_concurrent=int(self.settings.get("default_queue_concurrency", 2)))
        self.store.save_queue(q)
        self.queues[q.id] = q  # type: ignore[index]
        return q

    def delete_queue(self, qid: int) -> None:
        if qid == self.main_queue.id:
            return
        q = self.queues.pop(qid, None)
        if q:
            for iid in q.order:
                if iid in self.items:
                    self.items[iid].queue_id = None
            self.store.delete_queue(qid)

    def save_queue(self, q: DownloadQueue) -> None:
        self.store.save_queue(q)

    def add_to_queue(self, ids: Iterable[int], qid: int) -> None:
        with self.lock:
            for iid in ids:
                item = self.items.get(iid)
                if item is None:
                    continue
                for q in self.queues.values():
                    if iid in q.order:
                        q.order.remove(iid)
                self.queues[qid].order.append(iid)
                item.queue_id = qid
                if item.status in (Status.PAUSED, Status.ERROR) and iid not in self.workers:
                    item.status = Status.QUEUED
                self._persist(item)
            for q in self.queues.values():
                self.store.save_queue(q)

    def remove_from_queue(self, ids: Iterable[int]) -> None:
        with self.lock:
            for iid in ids:
                for q in self.queues.values():
                    if iid in q.order:
                        q.order.remove(iid)
                        self.store.save_queue(q)
                if iid in self.items:
                    self.items[iid].queue_id = None
                    self._persist(self.items[iid])

    def move_in_queue(self, iid: int, delta: int) -> None:
        item = self.items.get(iid)
        if not item or item.queue_id not in self.queues:
            return
        q = self.queues[item.queue_id]
        i = q.order.index(iid)
        j = max(0, min(len(q.order) - 1, i + delta))
        q.order.insert(j, q.order.pop(i))
        self.store.save_queue(q)

    def start_queue(self, qid: int) -> None:
        q = self.queues[qid]
        q.running = True
        for iid in q.order:
            self._queue_retries.pop(iid, None)
            it = self.items.get(iid)
            if it is not None and it.status in (Status.PAUSED, Status.ERROR) and iid not in self.workers:
                it.status = Status.QUEUED
        self.events.put(("queue_started", qid))

    def stop_queue(self, qid: int) -> None:
        q = self.queues[qid]
        q.running = False
        for iid in q.order:
            if iid in self.workers:
                self.pause(iid)
        self.events.put(("queue_stopped", qid))

    def _process_queues(self) -> None:
        for q in list(self.queues.values()):
            if not q.running:
                continue
            active = [i for i in q.order if i in self.workers]
            slots = q.max_concurrent - len(active)
            pending = []
            for iid in q.order:
                it = self.items.get(iid)
                if it is None or iid in self.workers or it.awaiting_refresh:
                    continue
                if it.status == Status.QUEUED:
                    pending.append(iid)
                elif it.status == Status.ERROR and q.retry_failed and self._queue_retries.get(iid, 0) < 3:
                    pending.append(iid)
            for iid in pending[:max(0, slots)]:
                if self.items[iid].status == Status.ERROR:
                    self._queue_retries[iid] = self._queue_retries.get(iid, 0) + 1
                self.start(iid)
            if not active and not pending:
                q.running = False
                self.events.put(("queue_done", (q.id, q.schedule.after_done)))

    # ------------------------------------------------------------------ scheduler
    def _check_schedules(self) -> None:
        now = time.localtime()
        minute = time.strftime("%H:%M", now)
        if minute == self._last_sched_minute:
            return
        self._last_sched_minute = minute
        today = time.strftime("%Y-%m-%d", now)
        for q in self.queues.values():
            s = q.schedule
            if not s.enabled:
                continue
            if minute == s.start_time and (s.once or now.tm_wday in s.days) and s.last_started != today:
                s.last_started = today
                if s.once:
                    s.enabled = False
                self.store.save_queue(q)
                self.start_queue(q.id)  # type: ignore[arg-type]
            if s.stop_enabled and minute == s.stop_time and q.running:
                self.stop_queue(q.id)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ loop
    def tick(self) -> None:
        with self.lock:
            for iid, w in list(self.workers.items()):
                if w.is_alive():
                    continue
                del self.workers[iid]
                item = self.items.get(iid)
                if item is None:
                    continue
                if item.status == Status.COMPLETE:
                    if (item.media or {}).get("fix_ext"):
                        self._fix_extension(item)
                    self.events.put(("complete", iid))
                elif item.status == Status.ERROR:
                    self.events.put(("error", iid))
                else:
                    self.events.put(("paused", iid))
            try:
                self._process_queues()
                self._check_schedules()
            except Exception:  # noqa: BLE001
                log.exception("queue/scheduler failure")

    def _fix_extension(self, item: DownloadItem) -> None:
        """The browser couldn't tell the file type (e.g. audio served as application/octet-stream
        from an address without an extension): name the file after what it really is."""
        from ..util import sniff_media_ext

        item.media = {k: v for k, v in item.media.items() if k != "fix_ext"}
        real = sniff_media_ext(item.path)
        cur = item.path.suffix.lower()
        if real == ".mp4" and cur in (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac"):
            real = ".m4a"                      # audio in an MP4 container
        if real and real != cur:
            new = unique_path(item.path.with_suffix(real))
            try:
                item.path.rename(new)
                item.filename = new.name
            except OSError:
                log.warning("could not rename %s", item.path)
        self._persist(item)

    def _loop(self) -> None:
        while not self._stop.wait(0.5):
            self.tick()

    def shutdown(self) -> None:
        self._stop.set()
        self.pause_all()
        for iid in list(self.workers):
            self._wait_worker(iid, 5)
        torrent_engine.shutdown()
        for it in self.items.values():
            self._persist(it)
        for q in self.queues.values():
            self.store.save_queue(q)

    # ------------------------------------------------------------------ stats
    def total_speed(self) -> float:
        return sum(self.items[i].speed for i in list(self.workers) if i in self.items)

    def export_list(self, file: Path) -> None:
        import json
        file.write_text(json.dumps([{"url": i.url, "filename": i.filename, "save_dir": i.save_dir,
                                     "referer": i.referer, "kind": i.kind, "media": i.media}
                                    for i in self.items.values()], indent=2), "utf-8")

    def import_list(self, file: Path) -> int:
        import json
        n = 0
        for e in json.loads(file.read_text("utf-8")):
            if not e.get("url"):
                continue
            self.add(e["url"], filename=e.get("filename", ""), save_dir=e.get("save_dir", ""),
                     referer=e.get("referer", ""), kind=e.get("kind", "http"), media=e.get("media"),
                     start=False)
            n += 1
        return n


def open_path(path: Path) -> None:
    """Open a file/folder with the OS default handler."""
    import subprocess
    import sys

    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def reveal_in_folder(path: Path) -> None:
    import subprocess
    import sys

    if sys.platform == "win32":
        if path.exists():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(path.parent))  # type: ignore[attr-defined]  # noqa: S606
    else:
        open_path(path.parent)


def system_action(action: str) -> None:
    """Run an after-queue action: shutdown | sleep | hibernate."""
    import subprocess
    import sys

    if sys.platform == "win32":
        cmds = {
            "shutdown": ["shutdown", "/s", "/t", "30"],
            "hibernate": ["shutdown", "/h"],
            "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
        }
    elif sys.platform == "darwin":
        cmds = {"shutdown": ["osascript", "-e", 'tell app "System Events" to shut down'],
                "sleep": ["pmset", "sleepnow"], "hibernate": ["pmset", "sleepnow"]}
    else:
        cmds = {"shutdown": ["systemctl", "poweroff"], "sleep": ["systemctl", "suspend"],
                "hibernate": ["systemctl", "hibernate"]}
    if action in cmds:
        subprocess.Popen(cmds[action])

