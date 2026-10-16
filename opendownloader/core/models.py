"""Data model for downloads, segments and queues."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Status(str, Enum):
    QUEUED = "Queued"          # waiting to be started (manually or by a queue)
    DOWNLOADING = "Downloading"
    PAUSED = "Paused"
    COMPLETE = "Complete"
    ERROR = "Error"
    PROCESSING = "Processing"  # merging / remuxing media
    CONNECTING = "Connecting"

    @property
    def active(self) -> bool:
        return self in (Status.DOWNLOADING, Status.CONNECTING, Status.PROCESSING)


class Kind(str, Enum):
    HTTP = "http"
    HLS = "hls"
    DASH = "dash"
    YTDLP = "ytdlp"
    TORRENT = "torrent"


@dataclass
class Segment:
    start: int
    end: int            # inclusive; -1 when the total size is unknown
    downloaded: int = 0

    @property
    def pos(self) -> int:
        return self.start + self.downloaded

    @property
    def remaining(self) -> int:
        if self.end < 0:
            return -1
        return max(0, self.end - self.pos + 1)

    @property
    def done(self) -> bool:
        return self.end >= 0 and self.pos > self.end


@dataclass
class DownloadItem:
    url: str
    filename: str = ""
    save_dir: str = ""
    id: int | None = None
    kind: str = Kind.HTTP.value
    size: int = -1
    downloaded: int = 0
    status: Status = Status.QUEUED
    resumable: bool = False
    segments: list[Segment] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    referer: str = ""
    category: str = "General"
    queue_id: int | None = None
    description: str = ""
    added_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    last_try: float | None = None
    error: str = ""
    max_connections: int = 0          # 0 => use global setting
    speed_limit_kbps: int = 0         # 0 => unlimited (per download)
    media: dict[str, Any] = field(default_factory=dict)  # HLS/DASH selection info
    etag: str = ""
    last_modified: str = ""
    auth_user: str = ""
    auth_pass: str = ""
    filename_locked: bool = False     # user chose the name; don't override from headers
    awaiting_refresh: bool = False    # waiting for a fresh link from the browser

    # runtime-only fields (not persisted)
    speed: float = field(default=0.0, compare=False)
    active_connections: int = field(default=0, compare=False)
    stage: str = field(default="", compare=False)      # e.g. "Downloading audio (2 of 2)"

    @property
    def path(self) -> Path:
        return Path(self.save_dir) / self.filename

    @property
    def part_path(self) -> Path:
        return Path(str(self.path) + ".part")

    @property
    def progress(self) -> float:
        if self.size and self.size > 0:
            return min(1.0, self.downloaded / self.size)
        return 1.0 if self.status == Status.COMPLETE else 0.0

    @property
    def eta(self) -> float | None:
        if self.speed > 0 and self.size > 0:
            return (self.size - self.downloaded) / self.speed
        return None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("speed", None)
        d.pop("active_connections", None)
        d.pop("stage", None)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DownloadItem":
        d = dict(d)
        d.pop("speed", None)
        d.pop("active_connections", None)
        d.pop("stage", None)
        d["status"] = Status(d.get("status", Status.QUEUED.value))
        d["segments"] = [Segment(**s) for s in d.get("segments", [])]
        known = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Schedule:
    enabled: bool = False
    start_time: str = "02:00"         # HH:MM local time
    stop_enabled: bool = False
    stop_time: str = "08:00"
    days: list[int] = field(default_factory=lambda: list(range(7)))  # 0=Mon
    once: bool = False
    after_done: str = "none"          # none | exit | shutdown | sleep | hibernate
    last_started: str = ""            # YYYY-MM-DD guard so we start once per day


@dataclass
class DownloadQueue:
    name: str
    id: int | None = None
    max_concurrent: int = 2
    order: list[int] = field(default_factory=list)
    schedule: Schedule = field(default_factory=Schedule)
    running: bool = False
    retry_failed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DownloadQueue":
        d = dict(d)
        d["schedule"] = Schedule(**d.get("schedule", {}))
        known = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in known})
