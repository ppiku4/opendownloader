"""Shared downloader infrastructure: errors, context, HTTP sessions, base thread."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

import requests
from requests.adapters import HTTPAdapter

from ..config import Settings
from .models import DownloadItem, Status
from .ratelimit import MultiLimiter, RateLimiter

log = logging.getLogger(__name__)

# Headers captured from the browser that must not be forwarded verbatim.
_DROP_HEADERS = {
    "range", "host", "content-length", "accept-encoding", "connection",
    "if-range", "if-modified-since", "if-none-match", "upgrade-insecure-requests",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user",
}


class DownloadError(Exception):
    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


class ProtectedContentError(DownloadError):
    """Raised for DRM-protected streams (Widevine/PlayReady/FairPlay/SAMPLE-AES).
    OpenDownloader does not circumvent DRM."""

    def __init__(self, what: str = "This stream is DRM-protected and cannot be downloaded."):
        super().__init__(what, fatal=True)


@dataclass
class EngineContext:
    settings: Settings
    global_limiter: RateLimiter
    persist: Callable[[DownloadItem], None]


def clean_headers(headers: dict[str, str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (headers or {}).items():
        if not k or k.lower() in _DROP_HEADERS or k.startswith(":"):
            continue
        out[k] = v
    return out


def make_session(settings: Settings, item: DownloadItem | None = None, pool: int = 32) -> requests.Session:
    s = requests.Session()
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=max(pool, 8), max_retries=0)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({
        "User-Agent": settings.get("user_agent"),
        "Accept": "*/*",
        "Accept-Encoding": "identity",   # byte offsets must match the file
    })
    proxy = settings.get("proxy")
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    s.verify = bool(settings.get("verify_tls", True))
    if item is not None:
        if item.referer:
            s.headers["Referer"] = item.referer
        s.headers.update(clean_headers(item.headers))
        if item.auth_user:
            s.auth = (item.auth_user, item.auth_pass)
    return s


class BaseDownloader(threading.Thread):
    """One thread per active download. Subclasses implement `download()`."""

    def __init__(self, item: DownloadItem, ctx: EngineContext):
        super().__init__(daemon=True, name=f"dl-{item.id}")
        self.item = item
        self.ctx = ctx
        self.stop_event = threading.Event()
        self.item_limiter = RateLimiter(item.speed_limit_kbps * 1024)
        self.limiter = MultiLimiter(ctx.global_limiter, self.item_limiter)
        self._speed_samples: list[tuple[float, int]] = []
        self._last_persist = 0.0
        self.finished_ok = False
        self.stopped_by_user = False
        self.finish_on_stop = False   # e.g. live-stream recording: stop == finish

    # --- control ----------------------------------------------------------
    def stop(self) -> None:
        self.stopped_by_user = True
        self.stop_event.set()

    def set_speed_limit(self, kbps: int) -> None:
        self.item.speed_limit_kbps = kbps
        self.item_limiter.set_rate(kbps * 1024)

    @property
    def stopping(self) -> bool:
        return self.stop_event.is_set()

    # --- helpers for subclasses ------------------------------------------
    def update_speed(self) -> None:
        now = time.monotonic()
        self._speed_samples.append((now, self.item.downloaded))
        cutoff = now - 4.0
        while len(self._speed_samples) > 2 and self._speed_samples[0][0] < cutoff:
            self._speed_samples.pop(0)
        t0, b0 = self._speed_samples[0]
        dt = now - t0
        self.item.speed = (self.item.downloaded - b0) / dt if dt > 0.3 else self.item.speed

    def maybe_persist(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_persist > 2.0:
            self._last_persist = now
            try:
                self.ctx.persist(self.item)
            except Exception:  # noqa: BLE001 - persistence must never kill a download
                log.exception("persist failed")

    # --- thread body ------------------------------------------------------
    def run(self) -> None:
        item = self.item
        item.last_try = time.time()
        item.error = ""
        item.status = Status.CONNECTING
        try:
            self.download()
            if self.stopping and not self.finish_on_stop:
                item.status = Status.PAUSED
            else:
                item.status = Status.COMPLETE
                item.completed_at = time.time()
                self.finished_ok = True
        except DownloadError as e:
            log.warning("download %s failed: %s", item.id, e)
            item.status = Status.PAUSED if (self.stopping and not e.fatal) else Status.ERROR
            item.error = str(e)
        except Exception as e:  # noqa: BLE001
            log.exception("download %s crashed", item.id)
            item.status = Status.PAUSED if self.stopping else Status.ERROR
            item.error = f"{type(e).__name__}: {e}"
        finally:
            item.speed = 0.0
            item.active_connections = 0
            self.maybe_persist(force=True)

    def download(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError
