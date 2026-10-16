"""Web-page videos (YouTube & co.) go through yt-dlp: quality list + format selection."""
from opendownloader.core.media_engine import YtDlpDownloader, _page_options
from opendownloader.core.models import DownloadItem

# Shape of `yt-dlp -J` output for a typical YouTube video (separate video / audio streams)
YT_INFO = {
    "title": "Some video",
    "formats": [
        {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "filesize": 3_000_000},
        {"format_id": "251", "vcodec": "none", "acodec": "opus", "filesize": 3_200_000},
        {"format_id": "18", "vcodec": "avc1", "acodec": "mp4a", "height": 360, "fps": 30, "filesize": 9_000_000},
        {"format_id": "137", "vcodec": "avc1", "acodec": "none", "height": 1080, "fps": 30, "filesize": 60_000_000},
        {"format_id": "248", "vcodec": "vp9", "acodec": "none", "height": 1080, "fps": 30, "filesize_approx": 55_000_000},
        {"format_id": "136", "vcodec": "avc1", "acodec": "none", "height": 720, "fps": 60, "filesize": 30_000_000},
        {"format_id": "sb0", "vcodec": "none", "acodec": "none", "format_note": "storyboard"},
    ],
}


def test_page_options_one_entry_per_resolution():
    opts = _page_options(YT_INFO)
    labels = [o["label"] for o in opts]
    assert [o["height"] for o in opts] == [1080, 720, 360, 0]
    assert labels[0] == "1080p   ~60.27 MB"      # 60 MB video + 3.2 MB best audio
    assert labels[1].startswith("720p60")
    assert opts[-1]["audio_only"] and opts[-1]["media"] == {"audio_only": True}


def test_page_options_generic_single_file():
    opts = _page_options({"title": "x", "formats": []})
    assert opts == [{"label": "Best available quality", "height": 0, "media": {}}]


class _Ctx:
    def __init__(self, ffmpeg):
        self.settings = {"ffmpeg_path": ffmpeg}


def _fmt(media, ffmpeg_present, monkeypatch):
    import opendownloader.core.media_engine as me

    monkeypatch.setattr(me, "find_ffmpeg", lambda *_: "/x/ffmpeg" if ffmpeg_present else None)
    d = YtDlpDownloader.__new__(YtDlpDownloader)
    d.item = DownloadItem(url="https://www.youtube.com/watch?v=abc", media=media)

    class C:
        settings = type("S", (), {"get": lambda self, k, default=None: ""})()

    d.ctx = C()
    return d._format()


def test_format_selection(monkeypatch):
    assert _fmt({"height": 720}, True, monkeypatch) == "bv*[height<=720]+ba/b[height<=720]/b"
    assert _fmt({"height": 720}, False, monkeypatch) == "b[height<=720]/b"     # no merge without ffmpeg
    assert _fmt({"audio_only": True}, False, monkeypatch) == "ba[ext=m4a]/ba/b"
    assert _fmt({}, True, monkeypatch) == "bv*+ba/b"


def test_page_options_unknown_codecs_generic_hls():
    """Generic HLS sites: yt-dlp reports vcodec/acodec None (unknown) - still one option per height."""
    info = {"duration": 10, "formats": [
        {"format_id": "400", "height": 240, "vcodec": None, "acodec": None, "tbr": 400.0},
        {"format_id": "800", "height": 360, "vcodec": None, "acodec": None, "tbr": 800.0},
    ]}
    opts = _page_options(info)
    assert [o["height"] for o in opts] == [360, 240]
    assert opts[0]["label"] == "360p   ~976.56 KB"     # 800 kbit/s * 10 s


def test_parts_progress_is_one_smooth_bar():
    """Video then audio (each 0-100% in yt-dlp) must become one rising overall progress."""
    from opendownloader.core.media_engine import _PartsProgress

    item = DownloadItem(url="x")
    pp = _PartsProgress(item, audio_only=False)
    pp.plan([9_000_000, 1_000_000], [None, None], [2000, 128], 60, "137+140")
    seen = []
    for done in range(0, 9_000_001, 1_000_000):
        pp.update(done, 9_000_000, "v.f137.mp4")
        seen.append(item.progress)
    assert item.stage == "Downloading video (1 of 2)"
    for done in range(0, 1_000_001, 250_000):
        pp.update(done, 1_000_000, "v.f140.m4a")
        seen.append(item.progress)
    assert seen == sorted(seen) and abs(seen[-1] - 1.0) < 1e-9
    assert abs(seen[9] - 0.9) < 0.01                  # video done = 90 % of the bytes
    assert item.stage == "Merging video and audio…"
