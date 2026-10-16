"""Multi-connection HTTP(S) downloader with dynamic segmentation and resume.

How it works
------------
1. A probe request `GET Range: bytes=0-` tells us the size, whether the server
   honours ranges (206) and the real filename. The open response is reused as
   connection #1 so no bytes are wasted.
2. The file is pre-allocated as `<name>.part`. Each connection owns a
   `Segment(start, end)` and writes at its own offset with its own file handle.
3. Dynamic segmentation: whenever fewer than `max_connections` are active, the
   segment with the most bytes left is split in half and a new connection takes
   the second half. The original connection simply stops earlier. This keeps
   every connection busy until the very end (no slow tail).
4. Segment state is persisted, so a paused / crashed download resumes exactly
   where each connection left off.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path

import requests

from ..util import guess_filename, unique_path
from .base import BaseDownloader, DownloadError, make_session
from .models import Segment, Status

log = logging.getLogger(__name__)

CHUNK = 64 * 1024
_CR_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.I)
_FATAL_CODES = {400, 401, 403, 404, 405, 410, 451}


def _parse_content_range(value: str | None) -> tuple[int, int, int] | None:
    m = _CR_RE.match(value or "")
    if not m:
        return None
    total = -1 if m.group(3) == "*" else int(m.group(3))
    return int(m.group(1)), int(m.group(2)), total


class HttpDownloader(BaseDownloader):

    def download(self) -> None:
        item = self.item
        st = self.ctx.settings
        self.session = make_session(st, item)
        self.lock = threading.Lock()
        self.timeout = float(st.get("timeout_s", 30))
        self.max_retries = int(st.get("max_retries", 20))
        self.retry_delay = float(st.get("retry_delay_s", 5))
        self.min_split = max(256 * 1024, int(st.get("min_split_size_kb", 1024)) * 1024)
        self.max_conn = max(1, min(32, item.max_connections or int(st.get("max_connections", 8))))
        self.fatal: DownloadError | None = None
        self.workers: dict[int, threading.Thread] = {}   # id(segment) -> thread

        first_resp = None
        fresh = not item.segments or not item.part_path.exists() or not item.resumable
        if fresh:
            first_resp = self._probe()
        if self.stopping:
            if first_resp is not None:
                first_resp.close()
            return

        item.status = Status.DOWNLOADING
        try:
            self._run_loop(first_resp)
        finally:
            if self.fatal:
                self.stop_event.set()
            for t in list(self.workers.values()):
                t.join(timeout=self.timeout + 5)
            self.session.close()

        if self.fatal:
            raise self.fatal
        if self.stopping:
            return
        self._finalize()

    # ------------------------------------------------------------------
    def _probe(self) -> requests.Response:
        item = self.item
        item.segments = []
        item.downloaded = 0
        attempts = 0
        while True:
            try:
                resp = self.session.get(
                    item.url, headers={"Range": "bytes=0-"}, stream=True,
                    timeout=(self.timeout, self.timeout), allow_redirects=True,
                )
                break
            except requests.RequestException as e:
                attempts += 1
                if attempts > min(self.max_retries, 5) or self.stopping:
                    raise DownloadError(f"Connection failed: {e}") from e
                self.stop_event.wait(self.retry_delay)
        if resp.status_code == 416:  # empty file or server dislikes 0- ranges
            resp.close()
            resp = self.session.get(item.url, stream=True, timeout=(self.timeout, self.timeout))
        if resp.status_code >= 400:
            code = resp.status_code
            resp.close()
            raise DownloadError(f"Server returned HTTP {code}", fatal=code in _FATAL_CODES)

        cr = _parse_content_range(resp.headers.get("Content-Range"))
        if resp.status_code == 206 and cr and cr[2] > 0:
            total, item.resumable = cr[2], True
        else:
            item.resumable = False
            cl = resp.headers.get("Content-Length")
            enc = resp.headers.get("Content-Encoding", "identity").lower()
            total = int(cl) if cl and cl.isdigit() and enc in ("identity", "") else -1
        item.size = total
        item.etag = resp.headers.get("ETag", "")
        item.last_modified = resp.headers.get("Last-Modified", "")

        if not item.filename_locked:
            name = guess_filename(resp.url, resp.headers.get("Content-Disposition"),
                                  resp.headers.get("Content-Type"))
            item.filename = name
        Path(item.save_dir).mkdir(parents=True, exist_ok=True)
        if item.path.exists() and not item.part_path.exists():
            item.filename = unique_path(item.path).name
        # (re)create the .part file, pre-allocated when the size is known
        with open(item.part_path, "wb") as fh:
            if total > 0:
                fh.truncate(total)
        item.segments = [Segment(0, total - 1 if total > 0 else -1)] if total != 0 else []
        self.maybe_persist(force=True)
        return resp

    # ------------------------------------------------------------------
    def _run_loop(self, first_resp: requests.Response | None) -> None:
        item = self.item
        # start workers for unfinished segments (largest first)
        pending = sorted((s for s in item.segments if not s.done), key=lambda s: -(s.remaining or 0))
        for seg in pending:
            if len(self._alive()) >= (self.max_conn if item.resumable else 1):
                break
            resp = first_resp if (first_resp is not None and seg.pos == 0) else None
            if resp is not None:
                first_resp = None
            self._start_worker(seg, resp)
        if first_resp is not None:
            first_resp.close()

        while not self.stopping:
            with self.lock:
                item.downloaded = sum(s.downloaded for s in item.segments)
            item.active_connections = len(self._alive())
            self.update_speed()
            self.maybe_persist()
            if self.fatal:
                return
            if all(s.done for s in item.segments) and not self._alive():
                break
            if item.resumable and item.size > 0:
                # restart orphaned segments (worker ended without finishing)
                for seg in item.segments:
                    if not seg.done and id(seg) not in self.workers and len(self._alive()) < self.max_conn:
                        self._start_worker(seg)
                while len(self._alive()) < self.max_conn and self._split():
                    pass
                # Report newly started connections before short segments finish.
                item.active_connections = len(self._alive())
            elif not self._alive() and not all(s.done for s in item.segments):
                seg = item.segments[0]
                if id(seg) not in self.workers:
                    self._start_worker(seg)
                else:
                    raise DownloadError("Connection closed before the download finished")
            self.stop_event.wait(0.25)
        with self.lock:
            item.downloaded = sum(s.downloaded for s in item.segments)

    def _alive(self) -> list[threading.Thread]:
        dead = [k for k, t in self.workers.items() if not t.is_alive()]
        for k in dead:
            del self.workers[k]
        return list(self.workers.values())

    def _start_worker(self, seg: Segment, resp: requests.Response | None = None) -> None:
        t = threading.Thread(target=self._worker, args=(seg, resp), daemon=True,
                             name=f"dl-{self.item.id}-{seg.start}")
        self.workers[id(seg)] = t
        t.start()

    def _split(self) -> bool:
        """Split the active segment with the most remaining bytes in two."""
        with self.lock:
            active = [s for s in self.item.segments if id(s) in self.workers and not s.done]
            if not active:
                return False
            seg = max(active, key=lambda s: s.remaining)
            rem = seg.remaining
            if rem < 2 * self.min_split:
                return False
            half = rem // 2
            new_start = seg.end - half + 1
            if new_start - seg.pos < self.min_split:
                return False
            new = Segment(new_start, seg.end)
            seg.end = new_start - 1
            self.item.segments = sorted(self.item.segments + [new], key=lambda s: s.start)
        self._start_worker(new)
        return True

    # ------------------------------------------------------------------
    def _worker(self, seg: Segment, resp: requests.Response | None) -> None:
        item = self.item
        retries = 0
        fh = open(item.part_path, "r+b")
        try:
            while not self.stopping and not seg.done and not self.fatal:
                try:
                    if resp is None:
                        headers = {}
                        if item.resumable:
                            with self.lock:
                                headers["Range"] = f"bytes={seg.pos}-{seg.end}"
                            if item.etag and not item.etag.startswith("W/"):
                                headers["If-Range"] = item.etag
                        else:
                            with self.lock:
                                seg.downloaded = 0
                            fh.seek(0)
                            fh.truncate(item.size if item.size > 0 else 0)
                        resp = self.session.get(item.url, headers=headers, stream=True,
                                                timeout=(self.timeout, self.timeout))
                    self._check_response(resp, seg)
                    fh.seek(seg.pos)
                    eof = True
                    for chunk in resp.iter_content(CHUNK):
                        if self.stopping or self.fatal:
                            eof = False
                            break
                        if not chunk:
                            continue
                        with self.lock:
                            if seg.end >= 0:
                                limit = seg.end - seg.pos + 1
                                data = chunk[:limit] if limit < len(chunk) else chunk
                            else:
                                data = chunk
                        if data:
                            fh.write(data)
                            with self.lock:
                                seg.downloaded += len(data)
                        if len(data) < len(chunk) or seg.done:
                            eof = False
                            break
                        retries = 0
                        self.limiter.consume(len(data), self.stop_event)
                    resp.close()
                    resp = None
                    if eof and not seg.done:
                        if seg.end < 0:          # unknown length: EOF means complete
                            with self.lock:
                                item.size = seg.pos
                                if seg.pos == 0:
                                    item.segments = []
                                    return
                                seg.end = seg.pos - 1
                        elif not self.stopping:
                            raise requests.ConnectionError("connection closed early")
                except DownloadError as e:
                    if resp is not None:
                        resp.close()
                    self.fatal = e
                    return
                except (requests.RequestException, OSError) as e:
                    if resp is not None:
                        resp.close()
                        resp = None
                    retries += 1
                    log.info("segment %s retry %s: %s", seg.start, retries, e)
                    if retries > self.max_retries:
                        self.fatal = DownloadError(f"Too many retries: {e}")
                        return
                    self.stop_event.wait(min(self.retry_delay * retries, 30))
        finally:
            fh.close()
            if resp is not None:
                resp.close()

    def _check_response(self, resp: requests.Response, seg: Segment) -> None:
        item = self.item
        code = resp.status_code
        if code == 416 and item.resumable:
            # Range beyond EOF: treat segment as complete if file size matches
            raise requests.ConnectionError("HTTP 416")
        if code >= 400:
            if code in _FATAL_CODES:
                raise DownloadError(
                    f"HTTP {code}. The link may have expired - use 'Refresh download address'.",
                    fatal=True)
            raise requests.ConnectionError(f"HTTP {code}")
        if item.resumable:
            if code != 206:
                raise DownloadError(
                    "The file changed on the server or it no longer supports resuming. "
                    "Restart the download.", fatal=True)
            cr = _parse_content_range(resp.headers.get("Content-Range"))
            if cr and (cr[0] != seg.pos or (cr[2] > 0 and item.size > 0 and cr[2] != item.size)):
                raise DownloadError("Server returned an unexpected range (file changed?)", fatal=True)

    # ------------------------------------------------------------------
    def _finalize(self) -> None:
        item = self.item
        item.status = Status.PROCESSING
        actual = item.part_path.stat().st_size
        if item.size > 0 and actual != item.size:
            raise DownloadError(f"Size mismatch: expected {item.size}, got {actual}")
        item.size = actual
        item.downloaded = actual
        target = item.path
        if target.exists():
            target = unique_path(target)
            item.filename = target.name
        os.replace(item.part_path, target)
        item.segments = [Segment(0, actual - 1, actual)] if actual else []
