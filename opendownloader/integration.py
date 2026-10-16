"""OS integration: browser native-messaging host registration, run-at-startup,
and the launch command the native host uses to start the app on demand."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import APP_NAME, NATIVE_HOST_NAME, paths

# Stable extension identities. The Chromium ID is derived from the public "key"
# in extension/manifest.chromium.json; the Firefox ID is set in its manifest.
CHROMIUM_EXTENSION_ID = "hkhijccfgjfdcidbdfgfbmghhjfnnldj"
FIREFOX_EXTENSION_ID = "{5a3cce4c-4077-450e-b1c5-baedeb0919c6}"

FROZEN = getattr(sys, "frozen", False)
PROJECT_ROOT = Path(__file__).resolve().parents[1]

_CHROMIUM_REG = [
    r"Software\Google\Chrome\NativeMessagingHosts",
    r"Software\Chromium\NativeMessagingHosts",
    r"Software\Microsoft\Edge\NativeMessagingHosts",
    r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts",
    r"Software\Vivaldi\NativeMessagingHosts",
]
_FIREFOX_REG = [r"Software\Mozilla\NativeMessagingHosts"]


def _unix_dirs() -> tuple[list[Path], list[Path]]:
    h = Path.home()
    if sys.platform == "darwin":
        base = h / "Library" / "Application Support"
        chromium = [base / p / "NativeMessagingHosts" for p in (
            "Google/Chrome", "Chromium", "Microsoft Edge", "BraveSoftware/Brave-Browser", "Vivaldi")]
        firefox = [base / "Mozilla" / "NativeMessagingHosts"]
    else:
        cfg = h / ".config"
        chromium = [cfg / p / "NativeMessagingHosts" for p in (
            "google-chrome", "chromium", "microsoft-edge", "BraveSoftware/Brave-Browser", "vivaldi")]
        firefox = [h / ".mozilla" / "native-messaging-hosts"]
    return chromium, firefox


def build_id() -> str:
    """Identifies this exact build, so a newly built copy can replace an older
    copy that is still running."""
    from . import __version__

    try:
        if FROZEN:
            exe = Path(sys.executable)
            return f"{__version__}|{exe.resolve()}|{int(exe.stat().st_mtime)}"
        pkg = Path(__file__).resolve().parent
        newest = max(int(f.stat().st_mtime) for f in pkg.rglob("*.py"))
        return f"{__version__}|{pkg}|{newest}"
    except OSError:
        return __version__


def app_launch_command(minimized: bool = True) -> list[str]:
    if FROZEN:
        cmd = [sys.executable]
    else:
        exe = Path(sys.executable)
        if sys.platform == "win32" and exe.name.lower() == "python.exe":
            pyw = exe.with_name("pythonw.exe")
            exe = pyw if pyw.exists() else exe
        cmd = [str(exe), str(PROJECT_ROOT / "OpenDownloader.pyw")]
    if minimized:
        cmd.append("--minimized")
    return cmd


def write_launch_info() -> None:
    """Called on app start so the native host knows how to launch us."""
    info = {"command": app_launch_command(True), "cwd": str(PROJECT_ROOT if not FROZEN else Path(sys.executable).parent)}
    (paths.app_data_dir() / "launch.json").write_text(json.dumps(info), "utf-8")


def _host_launcher() -> Path:
    """What the browser starts. For the packaged app that's OpenDownloader.exe
    itself (it detects browser launches). From source, a tiny .bat/.sh wrapper
    because browsers can only start an executable file."""
    if FROZEN:
        return Path(sys.executable)
    d = paths.app_data_dir() / "native_host"
    d.mkdir(parents=True, exist_ok=True)
    py = Path(sys.executable)
    if sys.platform == "win32" and py.name.lower() == "pythonw.exe":
        py = py.with_name("python.exe")
    if sys.platform == "win32":
        launcher = d / "opendownloader_host.bat"
        launcher.write_text(
            f'@echo off\r\ncd /d "{PROJECT_ROOT}"\r\n"{py}" -u -m opendownloader %*\r\n', "utf-8")
    else:
        launcher = d / "opendownloader_host.sh"
        launcher.write_text(
            f'#!/bin/sh\ncd "{PROJECT_ROOT}"\nexec "{py}" -u -m opendownloader "$@"\n', "utf-8")
        launcher.chmod(0o755)
    return launcher


def _manifests(extra_ids: list[str] | None = None) -> tuple[dict, dict]:
    launcher = str(_host_launcher())
    ids = [CHROMIUM_EXTENSION_ID, *(extra_ids or [])]
    chromium = {
        "name": NATIVE_HOST_NAME,
        "description": f"{APP_NAME} browser integration",
        "path": launcher,
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{i}/" for i in ids if i and not i.startswith("__")],
    }
    firefox = {
        "name": NATIVE_HOST_NAME,
        "description": f"{APP_NAME} browser integration",
        "path": launcher,
        "type": "stdio",
        "allowed_extensions": [FIREFOX_EXTENSION_ID],
    }
    return chromium, firefox


def register_native_host(extra_ids: list[str] | None = None) -> list[str]:
    """Register the native messaging host for all supported browsers.
    Returns a list of human-readable locations written."""
    chromium, firefox = _manifests(extra_ids)
    d = paths.app_data_dir() / "native_host"
    d.mkdir(parents=True, exist_ok=True)
    cfile = d / f"{NATIVE_HOST_NAME}.chromium.json"
    ffile = d / f"{NATIVE_HOST_NAME}.firefox.json"
    cfile.write_text(json.dumps(chromium, indent=2), "utf-8")
    ffile.write_text(json.dumps(firefox, indent=2), "utf-8")
    done: list[str] = []
    if sys.platform == "win32":
        import winreg

        for keys, f in ((_CHROMIUM_REG, cfile), (_FIREFOX_REG, ffile)):
            for k in keys:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{k}\\{NATIVE_HOST_NAME}") as key:
                    winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(f))
                done.append(f"HKCU\\{k}")
    else:
        cdirs, fdirs = _unix_dirs()
        for dirs, data in ((cdirs, chromium), (fdirs, firefox)):
            for dd in dirs:
                if not dd.parent.exists():
                    continue
                dd.mkdir(parents=True, exist_ok=True)
                (dd / f"{NATIVE_HOST_NAME}.json").write_text(json.dumps(data, indent=2), "utf-8")
                done.append(str(dd))
    return done


def unregister_native_host() -> None:
    if sys.platform == "win32":
        import winreg

        for k in _CHROMIUM_REG + _FIREFOX_REG:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, f"{k}\\{NATIVE_HOST_NAME}")
            except OSError:
                pass
    else:
        cdirs, fdirs = _unix_dirs()
        for dd in cdirs + fdirs:
            (dd / f"{NATIVE_HOST_NAME}.json").unlink(missing_ok=True)


def native_host_registered() -> bool:
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{_CHROMIUM_REG[0]}\\{NATIVE_HOST_NAME}"):
                return True
        except OSError:
            return False
    cdirs, fdirs = _unix_dirs()
    return any((dd / f"{NATIVE_HOST_NAME}.json").exists() for dd in cdirs + fdirs)


# --------------------------------------------------------------- autostart
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def set_autostart(enabled: bool) -> None:
    cmd = " ".join(f'"{c}"' if (" " in c or "\\" in c or "/" in c) else c for c in app_launch_command(True))
    if sys.platform == "win32":
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except OSError:
                    pass
    elif sys.platform.startswith("linux"):
        f = Path.home() / ".config" / "autostart" / "opendownloader.desktop"
        if enabled:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f"[Desktop Entry]\nType=Application\nName={APP_NAME}\n"
                         f"Exec={cmd}\n", "utf-8")
        else:
            f.unlink(missing_ok=True)


def autostart_enabled() -> bool:
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                winreg.QueryValueEx(key, APP_NAME)
                return True
        except OSError:
            return False
    return (Path.home() / ".config" / "autostart" / "opendownloader.desktop").exists()


# --------------------------------------------------------------- torrent files / magnet links
_TORRENT_PROGID = "OpenDownloader.torrent"


def _open_command() -> str:
    cmd = app_launch_command(False)
    return " ".join(f'"{c}"' for c in cmd) + ' "%1"'


def set_torrent_association(enabled: bool) -> None:
    """Open .torrent files and magnet: links with OpenDownloader (per user, Windows only)."""
    if sys.platform != "win32":
        return
    import winreg

    cu = winreg.HKEY_CURRENT_USER
    base = r"Software\Classes"
    if enabled:
        icon = f'"{app_launch_command(False)[0]}",0'
        with winreg.CreateKey(cu, rf"{base}\{_TORRENT_PROGID}") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "Torrent file")
        with winreg.CreateKey(cu, rf"{base}\{_TORRENT_PROGID}\DefaultIcon") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, icon)
        with winreg.CreateKey(cu, rf"{base}\{_TORRENT_PROGID}\shell\open\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, _open_command())
        with winreg.CreateKey(cu, rf"{base}\.torrent") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, _TORRENT_PROGID)
            winreg.SetValueEx(k, "Content Type", 0, winreg.REG_SZ, "application/x-bittorrent")
        with winreg.CreateKey(cu, rf"{base}\.torrent\OpenWithProgids") as k:
            winreg.SetValueEx(k, _TORRENT_PROGID, 0, winreg.REG_NONE, b"")
        with winreg.CreateKey(cu, rf"{base}\magnet") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:Magnet link")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(cu, rf"{base}\magnet\DefaultIcon") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, icon)
        with winreg.CreateKey(cu, rf"{base}\magnet\shell\open\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, _open_command())
    else:
        def delete_tree(path: str) -> None:
            try:
                with winreg.OpenKey(cu, path) as k:
                    while True:
                        try:
                            sub = winreg.EnumKey(k, 0)
                        except OSError:
                            break
                        delete_tree(rf"{path}\{sub}")
                winreg.DeleteKey(cu, path)
            except OSError:
                pass

        delete_tree(rf"{base}\{_TORRENT_PROGID}")
        if torrent_association_enabled(check_magnet_only=True):
            delete_tree(rf"{base}\magnet")
        try:
            with winreg.OpenKey(cu, rf"{base}\.torrent", 0, winreg.KEY_ALL_ACCESS) as k:
                if winreg.QueryValueEx(k, "")[0] == _TORRENT_PROGID:
                    winreg.DeleteValue(k, "")
        except OSError:
            pass
        try:
            with winreg.OpenKey(cu, rf"{base}\.torrent\OpenWithProgids", 0, winreg.KEY_ALL_ACCESS) as k:
                winreg.DeleteValue(k, _TORRENT_PROGID)
        except OSError:
            pass
    try:  # tell Explorer the associations changed
        import ctypes

        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
    except Exception:  # noqa: BLE001
        pass


def torrent_association_enabled(check_magnet_only: bool = False) -> bool:
    """True when magnet: links (and .torrent files) open with OpenDownloader."""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\magnet\shell\open\command") as k:
            if "opendownloader" not in str(winreg.QueryValueEx(k, "")[0]).lower():
                return False
        if check_magnet_only:
            return True
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{_TORRENT_PROGID}\shell\open\command"):
            return True
    except OSError:
        return False
