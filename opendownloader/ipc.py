"""Local IPC between the running app, the browser native-messaging host and
second instances (command line). TCP on 127.0.0.1 with a random port and a
secret token stored in the user's app-data folder, so web pages cannot talk to it
(they can't read the token) - only local processes of this user can.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import socket
import sys
import threading
from typing import Any, Callable

from . import paths

log = logging.getLogger(__name__)
MAX_MSG = 32 * 1024 * 1024


def _read_line(conn: socket.socket) -> bytes:
    buf = bytearray()
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
        if b"\n" in chunk or len(buf) > MAX_MSG:
            break
    return bytes(buf).split(b"\n", 1)[0]


class IpcServer(threading.Thread):
    def __init__(self, handler: Callable[[dict[str, Any]], dict[str, Any]]):
        super().__init__(daemon=True, name="ipc")
        self.handler = handler
        self.token = secrets.token_urlsafe(32)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        f = paths.ipc_file()
        f.write_text(json.dumps({"port": self.port, "token": self.token, "pid": os.getpid()}), "utf-8")
        if sys.platform != "win32":
            os.chmod(f, 0o600)
        self._closing = False

    def run(self) -> None:
        while not self._closing:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(10)
            raw = _read_line(conn)
            msg = json.loads(raw.decode("utf-8"))
            if not isinstance(msg, dict) or not hmac.compare_digest(str(msg.pop("token", "")), self.token):
                resp: dict[str, Any] = {"ok": False, "error": "unauthorized"}
            else:
                try:
                    resp = self.handler(msg)
                except Exception as e:  # noqa: BLE001
                    log.exception("ipc handler failed")
                    resp = {"ok": False, "error": str(e)}
            conn.sendall(json.dumps(resp).encode("utf-8") + b"\n")
        except (OSError, ValueError) as e:
            log.info("ipc error: %s", e)
        finally:
            conn.close()

    def close(self) -> None:
        self._closing = True
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            data = json.loads(paths.ipc_file().read_text("utf-8"))
            if data.get("pid") == os.getpid():
                paths.ipc_file().unlink()
        except (OSError, ValueError):
            pass


def send(msg: dict[str, Any], timeout: float = 10) -> dict[str, Any] | None:
    """Send a message to the running app. Returns None if it isn't running."""
    try:
        info = json.loads(paths.ipc_file().read_text("utf-8"))
    except (OSError, ValueError):
        return None
    try:
        with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=timeout) as s:
            s.sendall(json.dumps({**msg, "token": info["token"]}).encode("utf-8") + b"\n")
            raw = _read_line(s)
        return json.loads(raw.decode("utf-8")) if raw else None
    except (OSError, ValueError, KeyError):
        return None
