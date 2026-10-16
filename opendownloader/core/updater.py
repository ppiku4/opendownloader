"""Check GitHub for a newer release and install it with the release's own Setup.exe."""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from .. import __version__
from . import tools

REPO = "ppiku4/opendownloader"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
SETUP_ASSET = "OpenDownloader-Setup.exe"
SUMS_ASSET = "SHA256SUMS.txt"


class UpdateError(Exception):
    pass


@dataclass
class UpdateInfo:
    version: str
    notes: str
    page_url: str
    setup_url: str | None
    setup_sha256: str | None
    setup_size: int

    @property
    def newer(self) -> bool:
        return parse_version(self.version) > parse_version(__version__)


def parse_version(v: str) -> tuple[int, ...]:
    """'v1.2.0' -> (1, 2, 0). Anything after the numbers (-beta, +build) is ignored."""
    m = re.match(r"v?(\d+(?:\.\d+)*)", v.strip())
    if not m:
        return (0,)
    parts = [int(p) for p in m.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def can_self_install() -> bool:
    """True for a copy installed by Setup.exe (it can be replaced in place by a newer Setup.exe).

    Portable copies and runs from source get the release page instead.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    return any(Path(sys.executable).parent.glob("unins*.exe"))


def check(settings=None) -> UpdateInfo:
    s = tools._session(settings)
    s.headers["Accept"] = "application/vnd.github+json"
    try:
        r = s.get(LATEST_API, timeout=20)
    except OSError as e:
        raise UpdateError(f"Couldn't reach GitHub: {e}") from e
    if r.status_code == 404:
        raise UpdateError("No releases have been published yet.")
    if r.status_code != 200:
        raise UpdateError(f"GitHub answered HTTP {r.status_code}. Try again later.")
    rel = r.json()
    assets = {a.get("name"): a for a in rel.get("assets", [])}
    setup = assets.get(SETUP_ASSET)
    sha = None
    if setup is not None:
        digest = setup.get("digest") or ""
        if digest.startswith("sha256:"):
            sha = digest.split(":", 1)[1].lower()
        elif SUMS_ASSET in assets:
            sha = tools._checksums(s, assets[SUMS_ASSET]["browser_download_url"]).get(SETUP_ASSET)
    return UpdateInfo(
        version=(rel.get("tag_name") or "").lstrip("v"),
        notes=rel.get("body") or "",
        page_url=rel.get("html_url") or RELEASES_PAGE,
        setup_url=setup.get("browser_download_url") if setup else None,
        setup_sha256=sha,
        setup_size=int(setup.get("size") or 0) if setup else 0,
    )


def download(info: UpdateInfo, settings=None, progress: tools.Progress | None = None,
             stop: threading.Event | None = None) -> Path:
    """Download the new Setup.exe and verify its SHA-256. Returns its path."""
    if not info.setup_url:
        raise UpdateError(f"The release has no {SETUP_ASSET}.")
    if not info.setup_sha256:
        raise UpdateError("The release has no checksum for the installer, so it can't be verified.")
    dest = Path(tempfile.mkdtemp(prefix="opendownloader-update-")) / SETUP_ASSET
    tools._download(tools._session(settings), info.setup_url, dest, info.setup_sha256, progress, stop,
                    f"OpenDownloader {info.version}")
    return dest


def launch_installer(setup: Path) -> None:
    """Start Setup.exe in the background. It closes this app, updates it and starts it again."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(setup), "/SILENT", "/SP-", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH=1"],
                     close_fds=True, creationflags=flags)
