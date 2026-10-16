import pytest

from opendownloader.core import updater


@pytest.mark.parametrize("a,b", [
    ("1.2.0", "1.1.9"),
    ("v1.10.0", "1.9.3"),
    ("2", "1.99"),
    ("1.2.1", "1.2"),
])
def test_version_order(a, b):
    assert updater.parse_version(a) > updater.parse_version(b)


def test_version_equal_and_suffix():
    assert updater.parse_version("v1.2.0") == updater.parse_version("1.2")
    assert updater.parse_version("1.2.0-beta") == updater.parse_version("1.2.0")
    assert updater.parse_version("garbage") == (0,)


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class _Sess:
    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.proxies = {}

    def get(self, url, **_k):
        return self.routes[url]


def _release(assets):
    return {"tag_name": "v9.0.0", "body": "notes", "html_url": "https://example/rel", "assets": assets}


def test_check_uses_asset_digest(monkeypatch):
    rel = _release([{"name": "OpenDownloader-Setup.exe", "browser_download_url": "https://example/setup",
                     "size": 123, "digest": "sha256:" + "AB" * 32}])
    monkeypatch.setattr(updater.tools, "_session", lambda _s=None: _Sess({updater.LATEST_API: _Resp(200, rel)}))
    info = updater.check()
    assert info.version == "9.0.0" and info.newer
    assert info.setup_url == "https://example/setup"
    assert info.setup_sha256 == "ab" * 32


def test_check_falls_back_to_sums_file(monkeypatch):
    rel = _release([
        {"name": "OpenDownloader-Setup.exe", "browser_download_url": "https://example/setup", "size": 1},
        {"name": "SHA256SUMS.txt", "browser_download_url": "https://example/sums"},
    ])
    sums = f"{'c' * 64}  OpenDownloader-Setup.exe\n{'d' * 64}  other.zip\n"
    monkeypatch.setattr(updater.tools, "_session", lambda _s=None: _Sess({
        updater.LATEST_API: _Resp(200, rel), "https://example/sums": _Resp(200, text=sums)}))
    assert updater.check().setup_sha256 == "c" * 64


def test_no_release(monkeypatch):
    monkeypatch.setattr(updater.tools, "_session", lambda _s=None: _Sess({updater.LATEST_API: _Resp(404)}))
    with pytest.raises(updater.UpdateError, match="No releases"):
        updater.check()


def test_download_refuses_unverifiable_installer():
    info = updater.UpdateInfo("9.0.0", "", "", "https://example/setup", None, 1)
    with pytest.raises(updater.UpdateError, match="checksum"):
        updater.download(info)


def test_source_run_cannot_self_install():
    assert updater.can_self_install() is False
