"""Entry point.

    python -m opendownloader                       start the app
    python -m opendownloader URL [-o NAME] [-d DIR] [--silent] [--queue]
    python -m opendownloader --register-host       register browser integration

The same program is also the browser's native-messaging host: when Chrome/Edge/
Firefox start it (recognised by the arguments browsers pass), it bridges the
extension to the running app instead of opening a window.
"""
from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler

from . import APP_NAME, __version__, ipc, paths


def _setup_logging() -> None:
    h = RotatingFileHandler(paths.log_file(), maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(h)
    root.setLevel(logging.INFO)


def _stop_running(timeout: float) -> None:
    """Ask the running copy to exit (it saves its state and remembers which
    downloads were active). Older builds that don't understand the request are
    terminated; their progress is saved every few seconds anyway."""
    import json
    import os
    import time

    info_file = paths.ipc_file()
    try:
        pid = int(json.loads(info_file.read_text("utf-8")).get("pid", 0))
    except (OSError, ValueError):
        pid = 0
    resp = ipc.send({"type": "quit", "data": {"resume": True}}, timeout=5)
    deadline = time.time() + timeout
    if resp and resp.get("ok"):
        while time.time() < deadline and ipc.send({"type": "ping"}, timeout=2) is not None:
            time.sleep(0.3)
        time.sleep(0.5)
    if ipc.send({"type": "ping"}, timeout=2) is not None and pid and pid != os.getpid():
        try:
            if sys.platform == "win32":
                import subprocess

                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                               creationflags=0x08000000)
            else:
                import signal

                os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        while time.time() < deadline and ipc.send({"type": "ping"}, timeout=1) is not None:
            time.sleep(0.3)
        try:
            info_file.unlink()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    from .native_host.__main__ import is_browser_launch

    if is_browser_launch(argv):
        from .native_host.__main__ import main as host_main

        return host_main()

    p = argparse.ArgumentParser(prog="opendownloader", description=f"{APP_NAME} {__version__}")
    p.add_argument("url", nargs="?", help="address to download")
    p.add_argument("-o", "--filename", help="save as this file name")
    p.add_argument("-d", "--dir", help="save into this folder")
    p.add_argument("-r", "--referer", help="HTTP referer")
    p.add_argument("-s", "--silent", action="store_true", help="don't ask, start immediately")
    p.add_argument("-q", "--queue", action="store_true", help="add to the main queue without starting")
    p.add_argument("--minimized", action="store_true", help="start hidden in the tray")
    p.add_argument("--register-host", action="store_true", help="register the browser native-messaging host")
    p.add_argument("--unregister-host", action="store_true", help="remove the browser native-messaging host")
    p.add_argument("--associate-torrents", action="store_true",
                   help="open .torrent files and magnet links with OpenDownloader")
    p.add_argument("--quit", action="store_true", help="close the running OpenDownloader")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--smoke-test", metavar="REPORT", help=argparse.SUPPRESS)
    a, _unknown = p.parse_known_args(argv)

    if a.smoke_test:
        from .diagnostics import smoke_test

        return smoke_test(a.smoke_test)

    if a.associate_torrents:
        from . import integration

        try:
            integration.set_torrent_association(True)
        except Exception:  # noqa: BLE001 - runs from the installer: never show an error
            _setup_logging()
            logging.getLogger(__name__).exception("torrent association failed")
            return 1
        return 0

    if a.register_host or a.unregister_host:
        from . import integration

        out = sys.stdout.write if sys.stdout else (lambda _s: None)
        # Runs from the installer: never show an error dialog, just log and return a code.
        try:
            if a.register_host:
                for where in integration.register_native_host():
                    out(f"registered: {where}\n")
                integration.write_launch_info()
            else:
                integration.unregister_native_host()
                try:
                    integration.set_torrent_association(False)   # only removes our own entries
                except Exception:  # noqa: BLE001
                    pass
                out("unregistered\n")
        except Exception:  # noqa: BLE001
            _setup_logging()
            logging.getLogger(__name__).exception("browser integration (un)registration failed")
            return 1
        return 0

    if a.quit:
        _stop_running(timeout=15)
        return 0

    if a.url and "://" not in a.url and not a.url.lower().startswith("magnet:"):
        import os

        if os.path.isfile(a.url):          # e.g. a .torrent file opened from Explorer
            a.url = os.path.abspath(a.url)
    args = {"url": a.url, "filename": a.filename, "dir": a.dir, "referer": a.referer,
            "silent": a.silent, "queue": a.queue, "minimized": a.minimized}
    # Single instance: hand the request to an already running copy - unless that
    # copy is an older build, in which case it is replaced by this one.
    from .integration import build_id

    info = ipc.send({"type": "ping"}, timeout=3)
    if info is not None:
        if info.get("build") == build_id():
            resp = ipc.send({"type": "cli", "data": args}, timeout=3)
            if resp and resp.get("ok"):
                return 0
        else:
            _stop_running(timeout=20)

    _setup_logging()
    from .ui.app import run_gui

    return run_gui(args)


if __name__ == "__main__":
    sys.exit(main())
