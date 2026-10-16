"""HLS (m3u8) playlist parsing.

Supports master playlists (variants + alternate audio renditions) and media
playlists with EXT-X-KEY (AES-128), EXT-X-MAP (fMP4 init), EXT-X-BYTERANGE and
live playlists. DRM schemes (SAMPLE-AES, FairPlay, Widevine, PlayReady) are
detected and reported as protected - they are NOT decrypted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from .base import ProtectedContentError
from .media_types import KeyInfo, MediaTrack, SegRef

_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def parse_attrs(s: str) -> dict[str, str]:
    return {k: v.strip('"') for k, v in _ATTR_RE.findall(s)}


@dataclass
class Variant:
    url: str
    bandwidth: int = 0
    resolution: str = ""
    codecs: str = ""
    audio_group: str = ""
    frame_rate: str = ""

    @property
    def height(self) -> int:
        try:
            return int(self.resolution.split("x")[1])
        except (IndexError, ValueError):
            return 0

    @property
    def label(self) -> str:
        parts = []
        if self.resolution:
            parts.append(f"{self.height}p ({self.resolution})")
        if self.bandwidth:
            parts.append(f"{self.bandwidth / 1e6:.2f} Mbps")
        if self.codecs:
            parts.append(self.codecs)
        return "  ".join(parts) or self.url.rsplit("/", 1)[-1]


@dataclass
class Rendition:
    type: str
    group_id: str
    name: str = ""
    language: str = ""
    url: str = ""
    default: bool = False


@dataclass
class MasterPlaylist:
    variants: list[Variant] = field(default_factory=list)
    renditions: list[Rendition] = field(default_factory=list)

    def audio_for(self, variant: Variant) -> Rendition | None:
        group = [r for r in self.renditions
                 if r.type == "AUDIO" and r.group_id == variant.audio_group and r.url]
        if not group:
            return None
        return next((r for r in group if r.default), group[0])


@dataclass
class MediaPlaylist:
    segments: list[SegRef] = field(default_factory=list)
    init: SegRef | None = None
    endlist: bool = False
    target_duration: float = 6.0
    media_sequence: int = 0
    total_duration: float = 0.0

    def to_track(self, kind: str = "muxed") -> MediaTrack:
        ext = ".mp4" if (self.init is not None or any(s.url.split("?")[0].endswith((".m4s", ".mp4")) for s in self.segments[:3])) else ".ts"
        return MediaTrack(kind=kind, init=self.init, segments=self.segments, ext=ext)


def is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def parse_master(text: str, base_url: str) -> MasterPlaylist:
    mp = MasterPlaylist()
    lines = [ln.strip() for ln in text.splitlines()]
    pending: dict[str, str] | None = None
    for ln in lines:
        if not ln:
            continue
        if ln.startswith("#EXT-X-STREAM-INF:"):
            pending = parse_attrs(ln.split(":", 1)[1])
        elif ln.startswith("#EXT-X-MEDIA:"):
            a = parse_attrs(ln.split(":", 1)[1])
            mp.renditions.append(Rendition(
                type=a.get("TYPE", ""), group_id=a.get("GROUP-ID", ""), name=a.get("NAME", ""),
                language=a.get("LANGUAGE", ""), url=urljoin(base_url, a["URI"]) if a.get("URI") else "",
                default=a.get("DEFAULT", "NO") == "YES"))
        elif ln.startswith("#EXT-X-SESSION-KEY:"):
            _check_key(parse_attrs(ln.split(":", 1)[1]))
        elif not ln.startswith("#") and pending is not None:
            mp.variants.append(Variant(
                url=urljoin(base_url, ln),
                bandwidth=int(pending.get("BANDWIDTH", "0") or 0),
                resolution=pending.get("RESOLUTION", ""),
                codecs=pending.get("CODECS", ""),
                audio_group=pending.get("AUDIO", ""),
                frame_rate=pending.get("FRAME-RATE", "")))
            pending = None
    mp.variants.sort(key=lambda v: (v.height, v.bandwidth), reverse=True)
    return mp


def _check_key(a: dict[str, str]) -> None:
    method = a.get("METHOD", "NONE").upper()
    if method == "NONE":
        return
    keyformat = a.get("KEYFORMAT", "identity").lower()
    if method != "AES-128" or keyformat != "identity":
        raise ProtectedContentError(
            f"This stream uses DRM ({method}, {keyformat}). OpenDownloader does not bypass DRM.")


def _parse_byterange(value: str, last_end: int) -> tuple[int, int]:
    if "@" in value:
        length, off = value.split("@", 1)
        return int(off), int(length)
    return last_end, int(value)


def parse_media(text: str, base_url: str) -> MediaPlaylist:
    pl = MediaPlaylist()
    key: KeyInfo | None = None
    duration = 0.0
    byterange: tuple[int, int] | None = None
    last_end: dict[str, int] = {}
    seq = 0
    for ln in (x.strip() for x in text.splitlines()):
        if not ln:
            continue
        if ln.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            pl.media_sequence = int(ln.split(":", 1)[1])
            seq = pl.media_sequence
        elif ln.startswith("#EXT-X-TARGETDURATION:"):
            pl.target_duration = float(ln.split(":", 1)[1])
        elif ln.startswith("#EXT-X-ENDLIST"):
            pl.endlist = True
        elif ln.startswith("#EXT-X-KEY:"):
            a = parse_attrs(ln.split(":", 1)[1])
            _check_key(a)
            if a.get("METHOD", "NONE").upper() == "NONE":
                key = None
            else:
                iv = a.get("IV")
                key = KeyInfo(method="AES-128", uri=urljoin(base_url, a.get("URI", "")),
                              iv=bytes.fromhex(iv[2:] if iv.lower().startswith("0x") else iv) if iv else None)
        elif ln.startswith("#EXT-X-MAP:"):
            a = parse_attrs(ln.split(":", 1)[1])
            br = None
            if a.get("BYTERANGE"):
                br = _parse_byterange(a["BYTERANGE"], 0)
            pl.init = SegRef(url=urljoin(base_url, a["URI"]), byterange=br, key=key)
        elif ln.startswith("#EXTINF:"):
            try:
                duration = float(ln.split(":", 1)[1].split(",")[0])
            except ValueError:
                duration = 0.0
        elif ln.startswith("#EXT-X-BYTERANGE:"):
            byterange = ln.split(":", 1)[1]  # resolved when URI is known
        elif not ln.startswith("#"):
            url = urljoin(base_url, ln)
            br = None
            if byterange is not None:
                br = _parse_byterange(byterange, last_end.get(url, 0))  # type: ignore[arg-type]
                last_end[url] = br[0] + br[1]
                byterange = None
            iv = None
            if key is not None:
                iv = key.iv or seq.to_bytes(16, "big")
            pl.segments.append(SegRef(url=url, byterange=br, key=key, iv=iv,
                                      seq=seq, duration=duration))
            pl.total_duration += duration
            seq += 1
            duration = 0.0
    return pl
