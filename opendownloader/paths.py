"""Filesystem locations used by the app (per-user, cross-platform)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_NAME


def app_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = Path(os.environ.get("OPENDOWNLOADER_HOME", base / APP_NAME))
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_download_dir() -> Path:
    if sys.platform == "win32":
        try:
            import winreg  # noqa: WPS433

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
            )
            val, _ = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
            return Path(val)
        except OSError:
            pass
    return Path.home() / "Downloads"


def settings_file() -> Path:
    return app_data_dir() / "settings.json"


def db_file() -> Path:
    return app_data_dir() / "downloads.db"


def ipc_file() -> Path:
    """Holds the local IPC port + secret token. Only readable by this user."""
    return app_data_dir() / "ipc.json"


def temp_dir() -> Path:
    d = app_data_dir() / "temp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_file() -> Path:
    return app_data_dir() / "opendownloader.log"
