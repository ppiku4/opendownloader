"""Persistent user settings (JSON)."""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

from . import paths

DEFAULT_CATEGORIES: dict[str, list[str]] = {
    "Compressed": ["zip", "rar", "7z", "gz", "bz2", "xz", "tar", "tgz", "arj", "lzh", "ace", "z", "sit", "sitx", "sea", "zst"],
    "Documents": ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "pps", "txt", "rtf", "odt", "ods", "odp", "epub", "csv"],
    "Music": ["mp3", "wav", "wma", "aac", "flac", "ogg", "m4a", "aif", "aiff", "opus", "mpa", "ra"],
    "Programs": ["exe", "msi", "msu", "apk", "dmg", "pkg", "deb", "rpm", "appimage", "bin", "iso", "img"],
    "Video": ["mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg", "mpe", "3gp", "ogv", "ts", "rm", "rmvb", "asf", "qt"],
}

# File types that trigger browser/clipboard capture by default.
DEFAULT_CAPTURE_EXTS = sorted(
    ({e for exts in DEFAULT_CATEGORIES.values() for e in exts} - {"txt", "csv", "ts"}) | {"torrent"}
)

DEFAULTS: dict[str, Any] = {
    "download_dir": "",               # empty => system Downloads folder
    "use_category_folders": True,
    "categories": DEFAULT_CATEGORIES,
    "capture_extensions": DEFAULT_CAPTURE_EXTS,
    "max_connections": 8,
    "min_split_size_kb": 1024,        # don't create segments smaller than this
    "max_retries": 20,
    "retry_delay_s": 5,
    "timeout_s": 30,
    "speed_limit_enabled": False,
    "speed_limit_kbps": 1024,
    "proxy": "",                      # e.g. http://127.0.0.1:8080 or socks5://...
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "verify_tls": True,
    "monitor_clipboard": True,
    "show_add_dialog": True,          # show "Download File Info" before starting
    "show_progress_dialog": True,     # show the progress window when a download starts
    "show_complete_dialog": True,
    "show_notifications": True,
    "minimize_to_tray": True,
    "start_minimized": False,
    "ffmpeg_path": "",
    "media_connections": 6,
    "prefer_mp4_remux": True,
    "default_queue_concurrency": 2,
    "dark_mode": "auto",              # auto | light | dark
    "window_geometry": "",
    "column_widths": [],
    "extra_extension_ids": [],
    # BitTorrent
    "torrent_seed_mode": "none",      # none (stop when done) | ratio | time
    "torrent_seed_ratio": 1.0,
    "torrent_seed_minutes": 60,
    "torrent_port": 0,                # 0 => a random free port
    "torrent_upload_limit_kbps": 0,   # 0 => unlimited
    "torrent_max_connections": 200,
    "torrent_dht": True,              # DHT + local peer discovery (needed for most magnet links)
    "torrent_upnp": True,             # UPnP / NAT-PMP port forwarding
}


class Settings:
    """Thread-safe dict-like settings store persisted to settings.json."""

    def __init__(self, file: Path | None = None):
        self._file = file or paths.settings_file()
        self._lock = threading.RLock()
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        if self._file.exists():
            try:
                stored = json.loads(self._file.read_text("utf-8"))
                with self._lock:
                    self._data.update(stored)
                    # 1.1: .torrent links are captured too (once; the user may remove it again)
                    if not self._data.get("_migrated_torrent_ext"):
                        exts = list(self._data.get("capture_extensions") or [])
                        if "torrent" not in exts:
                            self._data["capture_extensions"] = sorted(exts + ["torrent"])
                        self._data["_migrated_torrent_ext"] = True
            except (OSError, ValueError):
                pass

    def save(self) -> None:
        with self._lock:
            tmp = self._file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), "utf-8")
            tmp.replace(self._file)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key, default))

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def __setitem__(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def update(self, values: dict[str, Any]) -> None:
        with self._lock:
            self._data.update(values)
        self.save()

    # ---- derived helpers -------------------------------------------------
    def download_dir(self) -> Path:
        d = self.get("download_dir")
        return Path(d) if d else paths.default_download_dir()

    def category_for(self, filename: str) -> str:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        for cat, exts in self.get("categories").items():
            if ext in exts:
                return cat
        return "General"

    def folder_for(self, filename: str, category: str | None = None) -> Path:
        base = self.download_dir()
        if not self.get("use_category_folders"):
            return base
        cat = category or self.category_for(filename)
        return base if cat == "General" else base / cat
