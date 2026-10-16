"""MPEG-DASH (MPD) parsing for static (VOD) manifests.

Supports SegmentTemplate (with $Number$/$Time$ + SegmentTimeline), SegmentList,
SegmentBase / single-file BaseURL, BaseURL inheritance and multi-period MPDs.
Encrypted representations (ContentProtection) are reported as DRM-protected and
are not downloaded.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urljoin

from .base import DownloadError, ProtectedContentError
from .media_types import MediaTrack, SegRef

_DUR_RE = re.compile(
    r"P(?:(?P<y>\d+(?:\.\d+)?)Y)?(?:(?P<mo>\d+(?:\.\d+)?)M)?(?:(?P<d>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<h>\d+(?:\.\d+)?)H)?(?:(?P<m>\d+(?:\.\d+)?)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?")
_TPL_RE = re.compile(r"\$(RepresentationID|Number|Bandwidth|Time)(%0(\d+)d)?\$")


def parse_duration(value: str | None) -> float:
    if not value:
        return 0.0
    m = _DUR_RE.fullmatch(value.strip())
    if not m:
        return 0.0
    g = {k: float(v) if v else 0.0 for k, v in m.groupdict().items()}
    return (g["y"] * 365 * 86400 + g["mo"] * 30 * 86400 + g["d"] * 86400
            + g["h"] * 3600 + g["m"] * 60 + g["s"])


def _strip_ns(root: ET.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def fill_template(tpl: str, rep_id: str, number: int | None = None,
                  bandwidth: int | None = None, time: int | None = None) -> str:
    values = {"RepresentationID": rep_id, "Number": number, "Bandwidth": bandwidth, "Time": time}

    def repl(m: re.Match) -> str:
        v = values[m.group(1)]
        if v is None:
            return m.group(0)
        if m.group(3) and isinstance(v, int):
            return str(v).zfill(int(m.group(3)))
        return str(v)

    return _TPL_RE.sub(repl, tpl).replace("$$", "$")


def _range(value: str | None) -> tuple[int, int] | None:
    if not value or "-" not in value:
        return None
    a, b = value.split("-", 1)
    return int(a), int(b) - int(a) + 1


@dataclass
class Representation:
    id: str
    kind: str                 # video | audio | text
    mime: str = ""
    codecs: str = ""
    bandwidth: int = 0
    width: int = 0
    height: int = 0
    lang: str = ""
    protected: bool = False
    periods: list[MediaTrack] = field(default_factory=list)   # one track per period

    @property
    def label(self) -> str:
        if self.kind == "video":
            return f"{self.height}p ({self.width}x{self.height})  {self.bandwidth / 1e6:.2f} Mbps  {self.codecs}"
        return f"Audio {self.lang or ''} {self.bandwidth / 1e3:.0f} kbps  {self.codecs}".replace("  ", " ")

    def track(self) -> MediaTrack:
        """Concatenate this representation across periods into a single track."""
        if not self.periods:
            raise DownloadError("Representation has no segments", fatal=True)
        first = self.periods[0]
        segs: list[SegRef] = []
        for p in self.periods:
            segs.extend(p.segments)
        ext = ".m4a" if self.kind == "audio" and "mp4" in self.mime else (
            ".webm" if "webm" in self.mime else ".mp4")
        return MediaTrack(kind=self.kind, init=first.init, segments=segs, ext=ext)


@dataclass
class Mpd:
    representations: list[Representation]
    dynamic: bool = False
    duration: float = 0.0

    def videos(self) -> list[Representation]:
        return sorted((r for r in self.representations if r.kind == "video"),
                      key=lambda r: (r.height, r.bandwidth), reverse=True)

    def audios(self) -> list[Representation]:
        return sorted((r for r in self.representations if r.kind == "audio"),
                      key=lambda r: r.bandwidth, reverse=True)

    def get(self, rep_id: str | None) -> Representation | None:
        return next((r for r in self.representations if r.id == rep_id), None)


def _base(url: str, el: ET.Element) -> str:
    b = el.find("BaseURL")
    if b is not None and b.text and b.text.strip():
        return urljoin(url, b.text.strip())
    return url


def _merge_tpl(parent: ET.Element | None, child: ET.Element | None) -> dict[str, object]:
    out: dict[str, object] = {}
    for el in (parent, child):
        if el is None:
            continue
        out.update(el.attrib)
        tl = el.find("SegmentTimeline")
        if tl is not None:
            out["_timeline"] = tl
        init = el.find("Initialization")
        if init is not None:
            out["_init_el"] = init
    return out


def _template_track(tpl: dict, base: str, rep: Representation, period_dur: float) -> MediaTrack:
    timescale = int(tpl.get("timescale", 1))
    start_number = int(tpl.get("startNumber", 1))
    media = str(tpl.get("media", ""))
    init = None
    if tpl.get("initialization"):
        init = SegRef(urljoin(base, fill_template(str(tpl["initialization"]), rep.id, bandwidth=rep.bandwidth)))
    segs: list[SegRef] = []
    timeline = tpl.get("_timeline")
    if timeline is not None:
        t = 0
        number = start_number
        entries = list(timeline.findall("S"))  # type: ignore[union-attr]
        end_time = period_dur * timescale if period_dur else None
        for i, s in enumerate(entries):
            if "t" in s.attrib:
                t = int(s.attrib["t"])
            d = int(s.attrib["d"])
            r = int(s.attrib.get("r", 0))
            if r < 0:  # repeat until next S@t or end of period
                nxt = entries[i + 1].attrib.get("t") if i + 1 < len(entries) else None
                limit = int(nxt) if nxt else end_time
                r = math.ceil((limit - t) / d) - 1 if limit else 0
            for _ in range(r + 1):
                url = fill_template(media, rep.id, number=number, bandwidth=rep.bandwidth, time=t)
                segs.append(SegRef(urljoin(base, url), seq=number, duration=d / timescale))
                t += d
                number += 1
    else:
        dur = int(tpl.get("duration", 0))
        if not dur:
            raise DownloadError("SegmentTemplate without duration or timeline", fatal=True)
        count = math.ceil(period_dur * timescale / dur) if period_dur else 0
        if count <= 0:
            raise DownloadError("Cannot determine the number of DASH segments", fatal=True)
        for n in range(start_number, start_number + count):
            url = fill_template(media, rep.id, number=n, bandwidth=rep.bandwidth,
                                time=(n - start_number) * dur)
            segs.append(SegRef(urljoin(base, url), seq=n, duration=dur / timescale))
    return MediaTrack(kind=rep.kind, init=init, segments=segs)


def _list_track(sl: ET.Element, base: str, kind: str) -> MediaTrack:
    init = None
    ini = sl.find("Initialization")
    if ini is not None:
        init = SegRef(urljoin(base, ini.get("sourceURL", "")) if ini.get("sourceURL") else base,
                      byterange=_range(ini.get("range")))
    segs = [SegRef(urljoin(base, s.get("media", "")) if s.get("media") else base,
                   byterange=_range(s.get("mediaRange")), seq=i)
            for i, s in enumerate(sl.findall("SegmentURL"))]
    return MediaTrack(kind=kind, init=init, segments=segs)


def parse_mpd(text: str, mpd_url: str) -> Mpd:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise DownloadError(f"Invalid MPD: {e}", fatal=True) from e
    _strip_ns(root)
    dynamic = root.get("type", "static") == "dynamic"
    total = parse_duration(root.get("mediaPresentationDuration"))
    reps: dict[str, Representation] = {}
    base0 = _base(mpd_url, root)
    periods = root.findall("Period")
    for p_index, period in enumerate(periods):
        pdur = parse_duration(period.get("duration"))
        if not pdur:
            if len(periods) == 1:
                pdur = total
            elif p_index + 1 < len(periods) and periods[p_index + 1].get("start"):
                pdur = parse_duration(periods[p_index + 1].get("start")) - parse_duration(period.get("start"))
            else:
                pdur = max(0.0, total - parse_duration(period.get("start")))
        pbase = _base(base0, period)
        for aset in period.findall("AdaptationSet"):
            abase = _base(pbase, aset)
            amime = aset.get("mimeType", "")
            ctype = aset.get("contentType", "")
            aprot = aset.find("ContentProtection") is not None
            for r in aset.findall("Representation"):
                mime = r.get("mimeType", amime)
                kind = ctype or mime.split("/")[0]
                if kind not in ("video", "audio"):
                    continue
                rid = r.get("id", f"{kind}{len(reps)}")
                rep = reps.get(rid) or Representation(
                    id=rid, kind=kind, mime=mime,
                    codecs=r.get("codecs", aset.get("codecs", "")),
                    bandwidth=int(r.get("bandwidth", 0)),
                    width=int(r.get("width", aset.get("width", 0)) or 0),
                    height=int(r.get("height", aset.get("height", 0)) or 0),
                    lang=aset.get("lang", ""),
                    protected=aprot or r.find("ContentProtection") is not None)
                reps[rid] = rep
                rbase = _base(abase, r)
                tpl = _merge_tpl(aset.find("SegmentTemplate"), r.find("SegmentTemplate"))
                sl = r.find("SegmentList") if r.find("SegmentList") is not None else aset.find("SegmentList")
                if tpl.get("media"):
                    track = _template_track(tpl, rbase, rep, pdur)
                elif sl is not None:
                    track = _list_track(sl, rbase, kind)
                else:  # SegmentBase / plain BaseURL: one self-contained file
                    track = MediaTrack(kind=kind, segments=[SegRef(rbase)])
                rep.periods.append(track)
    return Mpd(representations=list(reps.values()), dynamic=dynamic, duration=total)


def select(mpd: Mpd, video_id: str | None, audio_id: str | None) -> list[MediaTrack]:
    """Return the tracks to download. Missing ids => best available."""
    if mpd.dynamic:
        raise DownloadError("Live DASH streams are not supported yet (static MPD only).", fatal=True)
    tracks: list[MediaTrack] = []
    v = mpd.get(video_id) if video_id else (mpd.videos()[0] if mpd.videos() and video_id != "" else None)
    a = mpd.get(audio_id) if audio_id else (mpd.audios()[0] if mpd.audios() and audio_id != "" else None)
    for rep in (v, a):
        if rep is None:
            continue
        if rep.protected:
            raise ProtectedContentError()
        tracks.append(rep.track())
    if not tracks:
        raise DownloadError("No downloadable video/audio in this MPD", fatal=True)
    return tracks
