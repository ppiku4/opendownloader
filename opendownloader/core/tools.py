"""Optional helper tools that are downloaded on demand (never bundled):

* ffmpeg - merges separate audio + video streams / remuxes .ts to .mp4
* yt-dlp - extracts videos from web pages that don't expose a plain stream

Downloads come from the projects' official GitHub releases and are verified
against the SHA-256 checksums published with each release before anything is
installed. Files go to <app data>/tools/<tool>/.
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from .. import paths

FFMPEG_RELEASE = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
YTDLP_RELEASE = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/"
EXE = ".exe" if sys.platform == "win32" else ""
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

Progress = Callable[[int, int, str], None]   # done_bytes, total_bytes, status text


class ToolError(Exception):
    pass


class Cancelled(ToolError):
    pass


@dataclass
class ToolInfo:
    key: str
    name: str
    purpose: str
    approx_mb: int
    supported: bool
    manual_hint: str = ""


def tools_dir() -> Path:
    d = paths.app_data_dir() / "tools"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _arch_ok() -> bool:
    return platform.machine().lower() in ("amd64", "x86_64")


def info(key: str) -> ToolInfo:
    if key == "ffmpeg":
        supported = _arch_ok() and sys.platform in ("win32", "linux")
        hint = ("Install it with Homebrew:  brew install ffmpeg" if sys.platform == "darwin"
                else "Install ffmpeg with your package manager (e.g. sudo apt install ffmpeg).")
        return ToolInfo("ffmpeg", "ffmpeg", "merges separate audio and video into one MP4",
                        80 if sys.platform == "win32" else 135, supported, hint)
    if key == "yt-dlp":
        supported = sys.platform in ("win32", "darwin") or (sys.platform == "linux" and _arch_ok())
        return ToolInfo("yt-dlp", "yt-dlp", "finds the video on web pages such as YouTube",
                        18 if sys.platform == "win32" else 40, supported, "pip install yt-dlp")
    raise KeyError(key)


# ------------------------------------------------------------------ lookup
def installed_path(key: str) -> Path | None:
    exe = {"ffmpeg": "ffmpeg" + EXE, "yt-dlp": "yt-dlp" + EXE}[key]
    p = tools_dir() / key / exe
    return p if p.is_file() else None


def ytdlp_module_available() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("yt_dlp") is not None
    except (ImportError, ValueError):
        return False


def find_ytdlp_binary() -> str | None:
    p = installed_path("yt-dlp")
    if p:
        return str(p)
    return shutil.which("yt-dlp")


def available(key: str, settings=None) -> bool:
    if key == "ffmpeg":
        from .ffmpeg import find_ffmpeg

        return find_ffmpeg(settings.get("ffmpeg_path", "") if settings else "") is not None
    if key == "yt-dlp":
        return ytdlp_module_available() or find_ytdlp_binary() is not None
    return False


def version(key: str, settings=None) -> str:
    """Short version string of the tool that will be used, or '' if none."""
    try:
        if key == "ffmpeg":
            from .ffmpeg import find_ffmpeg

            exe = find_ffmpeg(settings.get("ffmpeg_path", "") if settings else "")
            if not exe:
                return ""
            out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=15,
                                 creationflags=_NO_WINDOW).stdout
            m = re.search(r"ffmpeg version (\S+)", out)
            return m.group(1) if m else "installed"
        if key == "yt-dlp":
            exe = find_ytdlp_binary()
            if exe:
                return subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30,
                                      creationflags=_NO_WINDOW).stdout.strip() or "installed"
            if ytdlp_module_available():
                from yt_dlp.version import __version__  # type: ignore

                return f"{__version__} (Python module)"
    except (OSError, subprocess.SubprocessError):
        return ""
    return ""


# ------------------------------------------------------------------ download
def _session(settings=None) -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "OpenDownloader-tool-installer"
    if settings is not None and settings.get("proxy"):
        s.proxies = {"http": settings.get("proxy"), "https": settings.get("proxy")}
    return s


def _checksums(s: requests.Session, url: str) -> dict[str, str]:
    r = s.get(url, timeout=30)
    if r.status_code != 200:
        raise ToolError(f"Could not fetch checksums ({r.status_code}) from {url}")
    out = {}
    for line in r.text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and len(parts[0]) == 64:
            out[parts[1].lstrip("*")] = parts[0].lower()
    return out


def _pick_ffmpeg_asset(sums: dict[str, str]) -> str:
    """Newest stable release build (not the nightly 'master' build)."""
    if sys.platform == "win32":
        pat = re.compile(r"ffmpeg-n(\d+)\.(\d+)-latest-win64-lgpl-shared-\1\.\2\.zip$")
        fallback = "ffmpeg-master-latest-win64-lgpl-shared.zip"
    else:
        pat = re.compile(r"ffmpeg-n(\d+)\.(\d+)-latest-linux64-lgpl-\1\.\2\.tar\.xz$")
        fallback = "ffmpeg-master-latest-linux64-lgpl.tar.xz"
    best = max(((int(m.group(1)), int(m.group(2)), name) for name in sums if (m := pat.match(name))),
               default=None)
    if best:
        return best[2]
    if fallback in sums:
        return fallback
    raise ToolError("No suitable ffmpeg build found in the release")


def _ytdlp_asset() -> str:
    if sys.platform == "win32":
        return "yt-dlp.exe"
    if sys.platform == "darwin":
        return "yt-dlp_macos"
    return "yt-dlp_linux"


def _download(s: requests.Session, url: str, dest: Path, expected_sha: str, progress: Progress | None,
              stop: threading.Event | None, label: str) -> None:
    h = hashlib.sha256()
    with s.get(url, stream=True, timeout=(20, 60)) as r:
        if r.status_code != 200:
            raise ToolError(f"Download failed: HTTP {r.status_code}")
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(256 * 1024):
                if stop is not None and stop.is_set():
                    raise Cancelled("Cancelled")
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total, f"Downloading {label}…")
    if h.hexdigest() != expected_sha:
        raise ToolError("Checksum mismatch - the download was corrupted or tampered with. Nothing was installed.")


def install(key: str, settings=None, progress: Progress | None = None,
            stop: threading.Event | None = None) -> Path:
    """Download, verify and install a tool. Returns the executable path."""
    inf = info(key)
    if not inf.supported:
        raise ToolError(f"Automatic download of {inf.name} isn't available on this system. {inf.manual_hint}")
    s = _session(settings)
    work = tools_dir() / f".{key}-download"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    target = tools_dir() / key
    try:
        if progress:
            progress(0, 0, "Checking the latest version…")
        if key == "yt-dlp":
            sums = _checksums(s, YTDLP_RELEASE + "SHA2-256SUMS")
            asset = _ytdlp_asset()
            if asset not in sums:
                raise ToolError(f"{asset} not listed in the release checksums")
            tmp = work / ("yt-dlp" + EXE)
            _download(s, YTDLP_RELEASE + asset, tmp, sums[asset], progress, stop, "yt-dlp")
            if not EXE:
                tmp.chmod(0o755)
            staged = work / "out"
            staged.mkdir()
            shutil.move(str(tmp), staged / tmp.name)
        else:
            sums = _checksums(s, FFMPEG_RELEASE + "checksums.sha256")
            asset = _pick_ffmpeg_asset(sums)
            archive = work / asset
            _download(s, FFMPEG_RELEASE + asset, archive, sums[asset], progress, stop, "ffmpeg")
            if progress:
                progress(0, 0, "Unpacking ffmpeg…")
            staged = work / "out"
            staged.mkdir()
            _extract_ffmpeg(archive, staged)
        # atomic-ish swap into place
        old = tools_dir() / f".{key}-old"
        shutil.rmtree(old, ignore_errors=True)
        if target.exists():
            target.rename(old)
        staged.rename(target)
        shutil.rmtree(old, ignore_errors=True)
        exe = installed_path(key)
        if exe is None:
            raise ToolError("Installation finished but the program file is missing")
        if progress:
            progress(1, 1, "Installed")
        return exe
    except requests.RequestException as e:
        raise ToolError(f"Network error: {e}") from e
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _extract_ffmpeg(archive: Path, dest: Path) -> None:
    """Keep only ffmpeg(.exe) and, for shared builds, the DLLs it needs."""
    skip = {"ffplay" + EXE, "ffprobe" + EXE}
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            for m in z.infolist():
                parts = m.filename.split("/")
                if len(parts) >= 3 and parts[-2] == "bin" and parts[-1] and parts[-1] not in skip:
                    with z.open(m) as src, open(dest / parts[-1], "wb") as out:
                        shutil.copyfileobj(src, out, 1024 * 1024)
    else:
        with tarfile.open(archive, "r:*") as t:
            for m in t.getmembers():
                parts = m.name.split("/")
                if m.isfile() and len(parts) >= 3 and parts[-2] == "bin" and parts[-1] == "ffmpeg":
                    src = t.extractfile(m)
                    with src, open(dest / "ffmpeg", "wb") as out:  # type: ignore[union-attr]
                        shutil.copyfileobj(src, out, 1024 * 1024)
                    os.chmod(dest / "ffmpeg", 0o755)
    if not (dest / ("ffmpeg" + EXE)).exists():
        raise ToolError("ffmpeg not found inside the downloaded archive")


def remove(key: str) -> None:
    shutil.rmtree(tools_dir() / key, ignore_errors=True)
