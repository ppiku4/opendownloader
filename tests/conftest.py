import hashlib
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("OPENDOWNLOADER_HOME", tempfile.mkdtemp(prefix="od-home-"))


class Handler(BaseHTTPRequestHandler):
    files: dict = {}

    def log_message(self, *a):  # silence
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        entry = self.files.get(path)
        if entry is None:
            self.send_response(404)
            self.end_headers()
            return
        body, opts = entry
        if callable(body):
            body = body()
        ctype = opts.get("type", "application/octet-stream")
        rng = self.headers.get("Range")
        start, end = 0, len(body) - 1
        status = 200
        if rng and opts.get("ranges", True):
            s, e = rng.replace("bytes=", "").split("-")
            start = int(s)
            end = int(e) if e else len(body) - 1
            end = min(end, len(body) - 1)
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if opts.get("disposition"):
            self.send_header("Content-Disposition", opts["disposition"])
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
            self.send_header("Accept-Ranges", "bytes")
        if not opts.get("no_length"):
            self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        data = body[start:end + 1]
        delay = opts.get("delay", 0)
        step = 32 * 1024
        try:
            for i in range(0, len(data), step):
                self.wfile.write(data[i:i + step])
                if delay:
                    time.sleep(delay)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


@pytest.fixture()
def server():
    Handler.files = {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, Handler.files
    srv.shutdown()


@pytest.fixture()
def manager(tmp_path):
    from opendownloader.config import Settings
    from opendownloader.core.manager import DownloadManager
    from opendownloader.core.store import Store

    st = Settings(tmp_path / "settings.json")
    st["download_dir"] = str(tmp_path / "dl")
    st["min_split_size_kb"] = 256
    st["retry_delay_s"] = 0.2
    st["max_retries"] = 3
    m = DownloadManager(st, Store(tmp_path / "db.sqlite"))
    yield m
    m.shutdown()


def sha(data: bytes) -> str:
    """Compare file contents by hash: a failing bytes == bytes assert prints megabytes of diff."""
    return hashlib.sha256(data).hexdigest()


def wait_for(pred, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False
