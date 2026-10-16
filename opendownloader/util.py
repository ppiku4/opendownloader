"""Small helpers: filenames, sizes, headers, mime types."""
from __future__ import annotations

import mimetypes
import os
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

EXTRA_MIME = {
    "application/vnd.apple.mpegurl": ".m3u8",
    "application/x-mpegurl": ".m3u8",
    "application/dash+xml": ".mpd",
    "video/mp2t": ".ts",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "audio/webm": ".weba",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
    "audio/mpeg": ".mp3",
    "application/x-7z-compressed": ".7z",
    "application/x-rar-compressed": ".rar",
    "application/vnd.rar": ".rar",
    "application/x-msdownload": ".exe",
    "application/x-msi": ".msi",
    "application/zip": ".zip",
    "application/pdf": ".pdf",
}


def sanitize_filename(name: str, default: str = "download") -> str:
    name = unquote(name or "").strip()
    name = _INVALID.sub("_", name)
    name = name.strip(" .")
    if not name:
        name = default
    stem = name.split(".")[0].upper()
    if stem in _RESERVED:
        name = "_" + name
    if len(name) > 200:
        root, ext = os.path.splitext(name)
        name = root[: 200 - len(ext)] + ext
    return name


def parse_content_disposition(value: str | None) -> str | None:
    """Return filename from a Content-Disposition header (RFC 6266 / 5987)."""
    if not value:
        return None
    m = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", value, re.I)
    if m:
        charset = m.group(1) or "utf-8"
        try:
            return unquote(m.group(2).strip().strip('"'), encoding=charset, errors="replace")
        except LookupError:
            return unquote(m.group(2).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"((?:\\.|[^"\\])*)"', value, re.I)
    if m:
        raw = m.group(1).replace('\\"', '"')
    else:
        m = re.search(r"filename\s*=\s*([^;]+)", value, re.I)
        if not m:
            return None
        raw = m.group(1).strip()
    # Many servers send raw UTF-8 bytes that requests decodes as latin-1.
    try:
        raw = raw.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return unquote(raw)


def filename_from_url(url: str) -> str:
    path = urlsplit(url).path
    name = unquote(path.rsplit("/", 1)[-1]) if path else ""
    return name


def ext_for_mime(mime: str | None) -> str:
    if not mime:
        return ""
    mime = mime.split(";")[0].strip().lower()
    return EXTRA_MIME.get(mime) or mimetypes.guess_extension(mime) or ""


def guess_filename(url: str, content_disposition: str | None = None, mime: str | None = None) -> str:
    name = parse_content_disposition(content_disposition) or filename_from_url(url)
    name = sanitize_filename(name)
    if "." not in name:
        name += ext_for_mime(mime)
    return name


def unique_path(path: Path) -> Path:
    """Return a non-existing path by appending ' (n)' before the extension."""
    if not path.exists() and not Path(str(path) + ".part").exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        cand = path.with_name(f"{stem} ({n}){suffix}")
        if not cand.exists() and not Path(str(cand) + ".part").exists():
            return cand
        n += 1


def human_size(n: float | int | None) -> str:
    if n is None or n < 0:
        return "Unknown"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.0f} {u}" if u == "B" else f"{f:.2f} {u}"
        f /= 1024
    return f"{n} B"


def human_speed(bps: float) -> str:
    return human_size(bps) + "/s" if bps > 0 else ""


def human_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or seconds == float("inf"):
        return ""
    if 0 < seconds < 1:
        return "< 1s"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def fmt_time(ts: float | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def cookie_header(cookies: list[dict] | str | None) -> str:
    if not cookies:
        return ""
    if isinstance(cookies, str):
        return cookies
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies if "name" in c)


URL_RE = re.compile(r"^(https?|ftp)://[^\s/$.?#].[^\s]*$", re.I)


def is_url(text: str) -> bool:
    return bool(URL_RE.match((text or "").strip()))


def sniff_media_ext(path: Path) -> str:
    """Real extension of an audio/video file from its first bytes ("" if unknown)."""
    try:
        with open(path, "rb") as f:
            h = f.read(64)
    except OSError:
        return ""
    if h[:3] == b"ID3" or (len(h) > 1 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0 and (h[1] & 0x06) != 0):
        return ".mp3"
    if len(h) > 1 and h[0] == 0xFF and (h[1] & 0xF6) == 0xF0:
        return ".aac"                                     # ADTS
    if h[:4] == b"OggS":
        return ".opus" if b"OpusHead" in h else ".ogg"
    if h[:4] == b"fLaC":
        return ".flac"
    if h[:4] == b"RIFF" and h[8:12] == b"WAVE":
        return ".wav"
    if h[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm"
    if h[4:8] == b"ftyp":
        brand = h[8:12]
        return ".m4a" if brand in (b"M4A ", b"M4B ") else ".mp4"
    return ""
