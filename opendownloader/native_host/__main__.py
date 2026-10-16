"""Native messaging host (started by the browser).

Protocol: each message is a 32-bit native-endian length followed by UTF-8 JSON,
on stdin/stdout. Every message is forwarded to the running app over local IPC;
if the app isn't running it is launched first.
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import time
from typing import BinaryIO

from .. import __version__, ipc, paths


def _open_stdio() -> tuple[BinaryIO, BinaryIO]:
    """Binary stdin/stdout. In a windowed (GUI-subsystem) Windows .exe Python may
    not set up sys.stdin/sys.stdout, so take the pipe handles the browser passed
    straight from the OS."""
    if sys.platform == "win32":
        import ctypes
        import msvcrt

        k32 = ctypes.windll.kernel32
        k32.GetStdHandle.restype = ctypes.c_void_p
        streams = []
        for std_id, flags, mode in ((-10, os.O_RDONLY, "rb"), (-11, os.O_WRONLY, "wb")):
            h = k32.GetStdHandle(std_id)
            if not h or h == ctypes.c_void_p(-1).value:
                raise OSError("no standard handle - not started by a browser?")
            fd = msvcrt.open_osfhandle(h, flags | os.O_BINARY)
            streams.append(os.fdopen(fd, mode, buffering=0))
        return streams[0], streams[1]
    return sys.stdin.buffer, sys.stdout.buffer


def _read_exact(stream: BinaryIO, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def _read(stream: BinaryIO) -> dict | None:
    raw = _read_exact(stream, 4)
    if len(raw) < 4:
        return None
    (n,) = struct.unpack("=I", raw)
    return json.loads(_read_exact(stream, n).decode("utf-8"))


def _write(stream: BinaryIO, msg: dict) -> None:
    data = json.dumps(msg).encode("utf-8")
    stream.write(struct.pack("=I", len(data)) + data)
    stream.flush()


def _launch_app() -> bool:
    try:
        info = json.loads((paths.app_data_dir() / "launch.json").read_text("utf-8"))
    except (OSError, ValueError):
        return False
    kwargs: dict = {"cwd": info.get("cwd") or None, "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if sys.platform == "win32":
        detached, new_group, breakaway = 0x00000008, 0x00000200, 0x01000000
        try:
            subprocess.Popen(info["command"], creationflags=detached | new_group | breakaway, **kwargs)
        except OSError:
            subprocess.Popen(info["command"], creationflags=detached | new_group, **kwargs)
    else:
        subprocess.Popen(info["command"], start_new_session=True, **kwargs)
    return True


def handle(msg: dict) -> dict:
    if msg.get("type") == "host_ping":
        return {"ok": True, "host": __version__}
    resp = ipc.send(msg)
    if resp is None and msg.get("type") != "ping":
        if not _launch_app():
            return {"ok": False, "error": "OpenDownloader is not running and could not be started"}
        deadline = time.time() + 20
        while time.time() < deadline:
            time.sleep(0.5)
            resp = ipc.send(msg)
            if resp is not None:
                break
    return resp or {"ok": False, "error": "OpenDownloader is not running"}


def is_browser_launch(argv: list[str]) -> bool:
    """Browsers start the native host with fixed arguments:
    Chrome/Edge/Brave: <chrome-extension://ID/> [--parent-window=N]
    Firefox:           <path to host manifest .json> <extension id>"""
    from ..integration import FIREFOX_EXTENSION_ID

    return any(a.startswith("chrome-extension://") or a == FIREFOX_EXTENSION_ID for a in argv) \
        or "--native-host" in argv


def main() -> int:
    try:
        fin, fout = _open_stdio()
    except OSError:
        return 1
    while True:
        try:
            msg = _read(fin)
        except (ValueError, OSError):
            break
        if msg is None:
            break
        try:
            resp = handle(msg)
        except Exception as e:  # noqa: BLE001
            resp = {"ok": False, "error": str(e)}
        if "_rid" in msg:
            resp["_rid"] = msg["_rid"]
        try:
            _write(fout, resp)
        except OSError:
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
