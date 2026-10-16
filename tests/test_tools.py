import sys

import pytest

from opendownloader.core import tools


def test_picks_newest_stable_ffmpeg(monkeypatch):
    sums = {
        "ffmpeg-master-latest-win64-lgpl-shared.zip": "a" * 64,
        "ffmpeg-n8.1-latest-win64-lgpl-shared-8.1.zip": "b" * 64,
        "ffmpeg-n9.0-latest-win64-lgpl-shared-9.0.zip": "c" * 64,
        "ffmpeg-n9.0-latest-win64-gpl-shared-9.0.zip": "d" * 64,
        "ffmpeg-n9.0-latest-linux64-lgpl-9.0.tar.xz": "e" * 64,
    }
    monkeypatch.setattr(sys, "platform", "win32")
    assert tools._pick_ffmpeg_asset(sums) == "ffmpeg-n9.0-latest-win64-lgpl-shared-9.0.zip"
    monkeypatch.setattr(sys, "platform", "linux")
    assert tools._pick_ffmpeg_asset(sums) == "ffmpeg-n9.0-latest-linux64-lgpl-9.0.tar.xz"


def test_checksum_mismatch_rejected(tmp_path):
    class Resp:
        status_code = 200
        headers = {"Content-Length": "5"}

        def iter_content(self, _n):
            yield b"hello"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Sess:
        def get(self, *a, **k):
            return Resp()

    with pytest.raises(tools.ToolError, match="Checksum mismatch"):
        tools._download(Sess(), "x", tmp_path / "f", "0" * 64, None, None, "x")
    import hashlib

    tools._download(Sess(), "x", tmp_path / "g", hashlib.sha256(b"hello").hexdigest(), None, None, "x")
    assert (tmp_path / "g").read_bytes() == b"hello"
