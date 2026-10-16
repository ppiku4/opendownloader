"""Single-connection FTP downloader with REST-based resume."""
from __future__ import annotations

import ftplib
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ..util import sanitize_filename, unique_path
from .base import BaseDownloader, DownloadError
from .models import Segment, Status


class _Stop(Exception):
    pass


class FtpDownloader(BaseDownloader):
    def download(self) -> None:
        item = self.item
        u = urlsplit(item.url)
        user = item.auth_user or unquote(u.username or "anonymous")
        pwd = item.auth_pass or unquote(u.password or "anonymous@")
        timeout = float(self.ctx.settings.get("timeout_s", 30))
        try:
            ftp = ftplib.FTP()
            ftp.connect(u.hostname, u.port or 21, timeout=timeout)
            ftp.login(user, pwd)
            ftp.voidcmd("TYPE I")
        except ftplib.all_errors as e:
            raise DownloadError(f"FTP connection failed: {e}") from e
        path = unquote(u.path)
        try:
            item.size = ftp.size(path) or -1
            item.resumable = True
        except ftplib.all_errors:
            item.size, item.resumable = -1, False
        if not item.filename:
            item.filename = sanitize_filename(path.rsplit("/", 1)[-1])
        Path(item.save_dir).mkdir(parents=True, exist_ok=True)
        offset = item.part_path.stat().st_size if (item.part_path.exists() and item.resumable) else 0
        item.downloaded = offset
        item.status = Status.DOWNLOADING
        item.active_connections = 1
        fh = open(item.part_path, "ab" if offset else "wb")

        def on_data(data: bytes) -> None:
            if self.stopping:
                raise _Stop()
            fh.write(data)
            item.downloaded += len(data)
            item.segments = [Segment(0, max(item.size, 0) - 1, item.downloaded)]
            self.update_speed()
            self.maybe_persist()
            self.limiter.consume(len(data), self.stop_event)

        try:
            ftp.retrbinary(f"RETR {path}", on_data, blocksize=64 * 1024, rest=offset or None)
        except _Stop:
            pass
        except ftplib.all_errors as e:
            raise DownloadError(f"FTP error: {e}") from e
        finally:
            fh.close()
            try:
                ftp.close()
            except Exception:  # noqa: BLE001
                pass
        if self.stopping:
            return
        target = item.path if not item.path.exists() else unique_path(item.path)
        item.filename = target.name
        os.replace(item.part_path, target)
        item.size = item.downloaded
