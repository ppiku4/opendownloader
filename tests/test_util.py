from opendownloader.core.batch import expand
from opendownloader.core.grabber import GrabOptions, extract_links, grab
from opendownloader.util import guess_filename, parse_content_disposition, sanitize_filename


def test_content_disposition():
    assert parse_content_disposition('attachment; filename="a b.zip"') == "a b.zip"
    assert parse_content_disposition("attachment; filename*=UTF-8''%E2%82%AC.txt") == "€.txt"
    assert parse_content_disposition("inline") is None


def test_sanitize():
    assert sanitize_filename('a<b>:c?.txt') == "a_b__c_.txt"
    assert sanitize_filename("CON.txt") == "_CON.txt"
    assert guess_filename("https://x.com/path/file%20name.zip?x=1") == "file name.zip"
    assert guess_filename("https://x.com/", mime="application/pdf") == "download.pdf"


def test_batch():
    assert expand("http://x/*.jpg", "1", "3", 3) == ["http://x/001.jpg", "http://x/002.jpg", "http://x/003.jpg"]
    assert expand("http://x/*.jpg", "a", "c") == ["http://x/a.jpg", "http://x/b.jpg", "http://x/c.jpg"]


def test_extract_links():
    html = '<base href="/sub/"><a href="f.zip">x</a><img src="i.png" srcset="a.jpg 1x, b.jpg 2x"><a href="javascript:1">'
    links = extract_links(html, "https://s.com/page")
    assert "https://s.com/sub/f.zip" in links and "https://s.com/sub/b.jpg" in links
    assert not any(link.startswith("javascript") for link in links)


def test_grabber(server):
    import requests

    base, files = server
    files["/"] = (b'<a href="/p2">p2</a><a href="/a.zip">a</a>', {"type": "text/html"})
    files["/p2"] = (b'<a href="/b.pdf">b</a><a href="/p3">p3</a>', {"type": "text/html"})
    files["/p3"] = (b'<a href="/c.pdf">c</a>', {"type": "text/html"})
    res = grab(GrabOptions(base + "/", depth=1, extensions=("zip", "pdf")), requests.Session())
    urls = {u for u, _ in res}
    assert urls == {base + "/a.zip", base + "/b.pdf"}


def test_register_host_on_fresh_profile(tmp_path, monkeypatch):
    """Regression: the packaged app must create the native_host folder itself."""
    import sys

    from opendownloader import integration, paths

    monkeypatch.setenv("OPENDOWNLOADER_HOME", str(tmp_path / "fresh"))
    monkeypatch.setattr(integration, "FROZEN", True)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "OpenDownloader.exe"))
    monkeypatch.setattr(integration, "_unix_dirs", lambda: ([tmp_path / "c" / "NativeMessagingHosts"], []))
    (tmp_path / "c").mkdir()
    integration.register_native_host()
    assert (paths.app_data_dir() / "native_host" / "com.opendownloader.host.chromium.json").exists()


def test_sniff_media_ext(tmp_path):
    from opendownloader.util import sniff_media_ext

    cases = {b"ID3\x04\x00" + b"\x00" * 40: ".mp3", b"\xff\xfb\x90\x00" + b"\x00" * 40: ".mp3",
             b"\xff\xf1\x50\x80" + b"\x00" * 40: ".aac", b"OggS\x00\x02" + b"\x00" * 22 + b"OpusHead": ".opus",
             b"OggS" + b"\x00" * 40: ".ogg", b"fLaC" + b"\x00" * 40: ".flac", b"RIFF\x00\x00\x00\x00WAVE": ".wav",
             b"\x1a\x45\xdf\xa3" + b"\x00" * 40: ".webm", b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 30: ".m4a",
             b"\x00\x00\x00\x20ftypisom" + b"\x00" * 30: ".mp4", b"<html>": ""}
    for i, (head, ext) in enumerate(cases.items()):
        f = tmp_path / f"f{i}"
        f.write_bytes(head)
        assert sniff_media_ext(f) == ext, (head[:12], ext)
