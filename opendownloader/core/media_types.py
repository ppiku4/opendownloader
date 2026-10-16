"""Common types for segmented media (HLS / DASH)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class KeyInfo:
    method: str               # only "AES-128" (standard HLS clear-key) is supported
    uri: str
    iv: bytes | None = None


@dataclass
class SegRef:
    url: str
    byterange: tuple[int, int] | None = None   # (offset, length)
    key: KeyInfo | None = None
    iv: bytes | None = None
    seq: int = 0
    duration: float = 0.0


@dataclass
class MediaTrack:
    kind: str                           # video | audio | muxed
    segments: list[SegRef] = field(default_factory=list)
    init: SegRef | None = None
    ext: str = ".ts"
