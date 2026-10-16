"""Segmented media downloader for HLS and DASH streams (+ optional yt-dlp)."""
from __future__ import annotations

import logging
import os
import queue
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests

from .. import paths
from ..util import human_size, sanitize_filename, unique_path
from . import dash, hls
from .aes import decrypt_aes128_cbc
from .base import BaseDownloader, DownloadError, ProtectedContentError, make_session
from .ffmpeg import find_ffmpeg, run_ffmpeg
from .media_types import MediaTrack, SegRef
from .models import DownloadItem, Status

log = logging.getLogger(__name__)


def _decrypt_aes128(data: bytes, key: bytes, iv: bytes) -> bytes:
    return decrypt_aes128_cbc(data, key, iv)


def _fetch_text(session: requests.Session, url: str, timeout: float) -> tuple[str, str]:
    r = session.get(url, timeout=timeout)
    if r.status_code >= 400:
        raise DownloadError(f"HTTP {r.status_code} while loading playlist", fatal=r.status_code in (401, 403, 404, 410))
    r.encoding = r.encoding or "utf-8"
    return r.text, r.url


class MediaDownloader(BaseDownloader):
    """Downloads every segment of the selected HLS/DASH track(s) in parallel,
    then joins them and (if ffmpeg is present) remuxes / merges to MP4."""

    def download(self) -> None:
        item = self.item
        st = self.ctx.settings
        self.timeout = float(st.get("timeout_s", 30))
        self.max_retries = int(st.get("max_retries", 20))
        self.retry_delay = float(st.get("retry_delay_s", 5))
        self.nconn = max(1, min(16, int(item.max_connections or st.get("media_connections", 6))))
        self.session = make_session(st, item, pool=self.nconn * 2)
        self.keys: dict[str, bytes] = {}
        self.key_lock = threading.Lock()
        self.lock = threading.Lock()
        self.workdir = paths.temp_dir() / f"media_{item.id}"
        self.workdir.mkdir(parents=True, exist_ok=True)
        item.segments = []

        kind = item.media.get("type", item.kind)
        if kind == "hls":
            tracks, live_urls = self._resolve_hls()
        else:
            tracks, live_urls = self._resolve_dash(), None
        item.status = Status.DOWNLOADING
        if live_urls:
            self.finish_on_stop = True
            self._record_live(tracks, live_urls)
        else:
            self._download_tracks(tracks)
            if self.stopping:
                return
        self._assemble(tracks)

    # ------------------------------------------------------------ resolve
    def _resolve_hls(self) -> tuple[list[MediaTrack], list[str] | None]:
        m = self.item.media
        url = m.get("variant") or m.get("url") or self.item.url
        text, url = _fetch_text(self.session, url, self.timeout)
        audio_url = m.get("audio")
        if hls.is_master(text):
            mp = hls.parse_master(text, url)
            if not mp.variants:
                raise DownloadError("Master playlist has no variants", fatal=True)
            want_h = int(m.get("height", 0) or 0)
            variant = next((v for v in mp.variants if want_h and v.height == want_h), mp.variants[0])
            rend = mp.audio_for(variant)
            if rend and not audio_url:
                audio_url = rend.url
            text, url = _fetch_text(self.session, variant.url, self.timeout)
        pl = hls.parse_media(text, url)
        tracks = [pl.to_track("video" if audio_url else "muxed")]
        live = [url]
        if audio_url:
            atext, aurl = _fetch_text(self.session, audio_url, self.timeout)
            apl = hls.parse_media(atext, aurl)
            tracks.append(apl.to_track("audio"))
            live.append(aurl)
        if not pl.segments:
            raise DownloadError("Playlist contains no segments", fatal=True)
        return tracks, (None if pl.endlist else live)

    def _resolve_dash(self) -> list[MediaTrack]:
        m = self.item.media
        text, url = _fetch_text(self.session, m.get("url") or self.item.url, self.timeout)
        mpd = dash.parse_mpd(text, url)
        return dash.select(mpd, m.get("video"), m.get("audio"))

    # ------------------------------------------------------------ segments
    def _seg_path(self, ti: int, si: int) -> Path:
        return self.workdir / f"t{ti}_{si:06d}.seg"

    @staticmethod
    def _all_refs(track: MediaTrack) -> list[SegRef]:
        return ([track.init] if track.init else []) + track.segments

    def _get_key(self, uri: str) -> bytes:
        with self.key_lock:
            if uri not in self.keys:
                r = self.session.get(uri, timeout=self.timeout)
                if r.status_code >= 400 or len(r.content) != 16:
                    raise DownloadError(f"Could not load AES-128 key ({r.status_code})", fatal=True)
                self.keys[uri] = r.content
            return self.keys[uri]

    def _fetch_segment(self, ref: SegRef, dest: Path, progress: list[int]) -> None:
        """Download one segment to `dest` (atomic). progress[0] tracks bytes in flight."""
        headers = {}
        if ref.byterange:
            off, length = ref.byterange
            headers["Range"] = f"bytes={off}-{off + length - 1}"
        attempt = 0
        while True:
            got = 0
            try:
                with self.session.get(ref.url, headers=headers, stream=True,
                                      timeout=(self.timeout, self.timeout)) as r:
                    if r.status_code >= 400:
                        if r.status_code in (401, 403, 404, 410) and attempt >= 2:
                            raise DownloadError(f"Segment HTTP {r.status_code}", fatal=True)
                        raise requests.ConnectionError(f"HTTP {r.status_code}")
                    buf = bytearray()
                    for chunk in r.iter_content(64 * 1024):
                        if self.stopping:
                            raise _Cancelled()
                        buf += chunk
                        got += len(chunk)
                        with self.lock:
                            progress[0] += len(chunk)
                        self.limiter.consume(len(chunk), self.stop_event)
                data = bytes(buf)
                if ref.key is not None:
                    data = _decrypt_aes128(data, self._get_key(ref.key.uri),
                                           ref.iv or ref.key.iv or bytes(16))
                tmp = dest.with_suffix(".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, dest)
                return
            except (requests.RequestException, OSError) as e:
                with self.lock:
                    progress[0] -= got
                attempt += 1
                if attempt > self.max_retries:
                    raise DownloadError(f"Segment failed: {e}") from e
                if self.stop_event.wait(min(self.retry_delay * attempt, 20)):
                    raise _Cancelled() from e
            except _Cancelled:
                with self.lock:
                    progress[0] -= got
                raise

    def _download_tracks(self, tracks: list[MediaTrack]) -> None:
        item = self.item
        jobs: queue.Queue = queue.Queue()
        total = 0
        done_bytes = 0
        done_count = 0
        for ti, t in enumerate(tracks):
            for si, ref in enumerate(self._all_refs(t)):
                total += 1
                p = self._seg_path(ti, si)
                if p.exists():
                    done_bytes += p.stat().st_size
                    done_count += 1
                else:
                    jobs.put((ti, si, ref))
        progress = [done_bytes]
        counters = {"done": done_count}
        errors: list[BaseException] = []

        def worker() -> None:
            while not self.stopping and not errors:
                try:
                    ti, si, ref = jobs.get_nowait()
                except queue.Empty:
                    return
                try:
                    self._fetch_segment(ref, self._seg_path(ti, si), progress)
                    with self.lock:
                        counters["done"] += 1
                except _Cancelled:
                    return
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)
                    return

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(self.nconn)]
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            with self.lock:
                item.downloaded = progress[0]
                d = counters["done"]
            item.media["segments_done"], item.media["segments_total"] = d, total
            item.size = int(item.downloaded / d * total) if d else -1
            item.active_connections = sum(t.is_alive() for t in threads)
            self.update_speed()
            self.maybe_persist()
            if errors:
                self.stop_event.set()
            time.sleep(0.25)
        if errors:
            err = errors[0]
            raise err if isinstance(err, DownloadError) else DownloadError(str(err))

    def _record_live(self, tracks: list[MediaTrack], urls: list[str]) -> None:
        """Keep polling a live playlist and fetch new segments until stopped."""
        item = self.item
        item.description = item.description or "Live stream recording - press Stop to finish"
        seen: list[set[int]] = [set() for _ in tracks]
        collected: list[list[SegRef]] = [[] for _ in tracks]
        progress = [0]
        target = 6.0
        for ti, t in enumerate(tracks):
            if t.init is not None:
                try:
                    self._fetch_segment(t.init, self._seg_path(ti, 0), progress)
                except _Cancelled:
                    pass
        while not self.stopping:
            ended = True
            for ti, url in enumerate(urls):
                try:
                    text, real = _fetch_text(self.session, url, self.timeout)
                    pl = hls.parse_media(text, real)
                except (requests.RequestException, DownloadError) as e:
                    log.info("live reload failed: %s", e)
                    ended = False
                    continue
                target = pl.target_duration
                ended = ended and pl.endlist
                if pl.init and tracks[ti].init is None:
                    tracks[ti].init = pl.init
                    try:
                        self._fetch_segment(pl.init, self._seg_path(ti, 0), progress)
                    except _Cancelled:
                        break
                for ref in pl.segments:
                    if ref.seq in seen[ti] or self.stopping:
                        continue
                    seen[ti].add(ref.seq)
                    collected[ti].append(ref)
                    si = len(collected[ti]) - 1 + (1 if tracks[ti].init else 0)
                    try:
                        self._fetch_segment(ref, self._seg_path(ti, si), progress)
                    except _Cancelled:
                        break
                    except DownloadError as e:
                        log.info("live segment skipped: %s", e)
                    item.downloaded = progress[0]
                    self.update_speed()
            if ended:
                break
            self.stop_event.wait(max(1.0, target / 2))
        for ti, t in enumerate(tracks):
            t.segments = collected[ti]
        self.stop_event.clear()

    # ------------------------------------------------------------ assemble
    def _assemble(self, tracks: list[MediaTrack]) -> None:
        item = self.item
        item.status = Status.PROCESSING
        outs: list[Path] = []
        for ti, t in enumerate(tracks):
            out = self.workdir / f"track{ti}{t.ext}"
            with open(out, "wb") as fh:
                for si in range(len(self._all_refs(t))):
                    p = self._seg_path(ti, si)
                    if p.exists():
                        with open(p, "rb") as src:
                            shutil.copyfileobj(src, fh, 1024 * 1024)
            outs.append(out)

        ffmpeg = find_ffmpeg(self.ctx.settings.get("ffmpeg_path", ""))
        stem = Path(sanitize_filename(item.filename or "video")).stem
        save = Path(item.save_dir)
        save.mkdir(parents=True, exist_ok=True)
        is_webm = all(o.suffix == ".webm" for o in outs)
        audio_exts = {".aac", ".mp3", ".m4a", ".opus", ".ogg", ".oga", ".flac", ".wav"}
        audio_only = (len(outs) == 1 and (outs[0].suffix.lower() in audio_exts
                                          or Path(item.filename or "").suffix.lower() in audio_exts))
        if audio_only:
            target_ext = ".mp3" if outs[0].suffix.lower() == ".mp3" else ".m4a"
        else:
            target_ext = ".webm" if is_webm else ".mp4"
        if ffmpeg and (len(outs) > 1 or self.ctx.settings.get("prefer_mp4_remux", True)):
            final = unique_path(save / f"{stem}{target_ext}")
            try:
                run_ffmpeg(ffmpeg, outs, final)
            except RuntimeError as e:
                if len(outs) > 1:
                    raise DownloadError(str(e)) from e
                final = unique_path(save / f"{stem}{outs[0].suffix}")
                shutil.move(str(outs[0]), final)
        else:
            final = unique_path(save / f"{stem}{outs[0].suffix}")
            shutil.move(str(outs[0]), final)
            if len(outs) > 1:
                audio = unique_path(save / f"{stem}.audio{outs[1].suffix}")
                shutil.move(str(outs[1]), audio)
                item.description = f"Audio saved separately as {audio.name} (install ffmpeg to merge automatically)"
        item.filename = final.name
        item.size = item.downloaded = final.stat().st_size
        shutil.rmtree(self.workdir, ignore_errors=True)


class _Cancelled(Exception):
    pass


# ---------------------------------------------------------------------------
class _PartsProgress:
    """yt-dlp downloads video and audio one after another, each with its own 0-100%.
    This folds them into one overall progress, weighting the parts by their expected size."""

    def __init__(self, item: DownloadItem, audio_only: bool):
        self.item = item
        self.audio_only = audio_only
        self.weights: list[float] = [1.0]
        self.index = -1
        self.current_file = ""
        self.done_bytes = 0
        self.cur_done = 0
        self.cur_total = 0

    def plan(self, sizes: list, approx: list, tbrs: list, duration, format_id: str) -> None:
        n = max(1, len(format_id.split("+"))) if format_id and format_id != "NA" else 1
        w = []
        for i in range(n):
            size = _num(sizes, i) or _num(approx, i)
            if not size and _num(tbrs, i):
                size = _num(tbrs, i) * 1000 / 8 * (float(duration) if _is_num(duration) else 1)
            w.append(size)
        if not all(w):   # unknown: assume video carries ~90% of the bytes
            w = [1.0] if n == 1 else [0.9] + [0.1 / (n - 1)] * (n - 1)
        self.weights = w

    def _label(self, i: int) -> str:
        n = len(self.weights)
        kind = "audio" if (self.audio_only or (n > 1 and i == n - 1)) else "video"
        return f"Downloading {kind}" + (f" ({i + 1} of {n})" if n > 1 else "")

    def update(self, done: int, total: int, filename: str) -> None:
        if filename and filename != self.current_file:
            if self.index >= 0:
                self.done_bytes += max(self.cur_done, self.cur_total)
            self.current_file = filename
            self.index = min(self.index + 1, len(self.weights) - 1)
            self.cur_done = self.cur_total = 0
        self.cur_done = max(0, done)
        if total > 0:
            self.cur_total = total
        i = max(self.index, 0)
        w_cur = self.weights[i] or 1.0
        w_rest = sum(self.weights[i + 1:])
        item = self.item
        item.stage = self._label(i)
        item.downloaded = self.done_bytes + self.cur_done
        if self.cur_total > 0:
            item.size = max(item.downloaded, int(self.done_bytes + self.cur_total * (1 + w_rest / w_cur)))
        last = i == len(self.weights) - 1
        if last and self.cur_total > 0 and self.cur_done >= self.cur_total and len(self.weights) > 1:
            item.stage = "Merging video and audio…"
            item.status = Status.PROCESSING


def _is_num(x) -> bool:
    try:
        float(x)
        return True
    except (TypeError, ValueError):
        return False


def _num(values, i: int) -> float:
    try:
        v = values[i]
        return float(v) if _is_num(v) else 0.0
    except (IndexError, TypeError):
        return 0.0


class YtDlpDownloader(BaseDownloader):
    """Page-URL extraction via the open-source yt-dlp project. Uses the yt-dlp
    program downloaded on demand (or on PATH); falls back to the Python module
    when running from source with `pip install yt-dlp`."""

    def download(self) -> None:
        from .tools import find_ytdlp_binary, ytdlp_module_available

        exe = find_ytdlp_binary()
        if exe:
            self._run_binary(exe)
        elif ytdlp_module_available():
            self._run_module()
        else:
            raise DownloadError("yt-dlp is not installed. Open Options → Video and click 'Download yt-dlp'.",
                                fatal=True)

    def _format(self) -> str:
        """yt-dlp format selector for the quality the user picked."""
        m = self.item.media
        if m.get("format"):
            return m["format"]
        ff = find_ffmpeg(self.ctx.settings.get("ffmpeg_path", ""))
        if m.get("audio_only"):
            return "ba[ext=m4a]/ba/b"
        h = int(m.get("height") or 0)
        if h:
            # Merging separate video + audio needs ffmpeg; otherwise best single file up to h.
            return f"bv*[height<={h}]+ba/b[height<={h}]/b" if ff else f"b[height<={h}]/b"
        return "bv*+ba/b" if ff else "b"

    def _out_template(self) -> str:
        item = self.item
        if item.filename_locked and item.filename:
            stem = Path(item.filename).stem.replace("%", "%%")
            return str(Path(item.save_dir) / f"{stem}.%(ext)s")
        return str(Path(item.save_dir) / "%(title).150B.%(ext)s")

    def _run_binary(self, exe: str) -> None:
        import subprocess

        item = self.item
        st = self.ctx.settings
        out_tmpl = self._out_template()
        Path(item.save_dir).mkdir(parents=True, exist_ok=True)
        tpl = "[od] %(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|" \
              "%(progress.speed)s|%(progress.filename)s"
        cmd = [exe, "--newline", "--no-playlist", "--continue", "--no-colors", "--progress",
               "--progress-template", "download:" + tpl, "--print", "after_move:[od-file] %(filepath)s",
               "--print", "before_dl:[od-plan] %(requested_formats.:.filesize)j|%(requested_formats.:.filesize_approx)j"
                          "|%(requested_formats.:.tbr)j|%(duration)j|%(format_id)s",
               "-f", self._format(), "-o", out_tmpl,
               "--concurrent-fragments", str(int(st.get("media_connections", 6))),
               "--user-agent", st.get("user_agent")]
        if item.referer:
            cmd += ["--referer", item.referer]
        ff = find_ffmpeg(st.get("ffmpeg_path", ""))
        if ff:
            cmd += ["--ffmpeg-location", ff, "--merge-output-format", "mp4"]
        if st.get("proxy"):
            cmd += ["--proxy", st.get("proxy")]
        limits = [int(item.speed_limit_kbps or 0)]
        if st.get("speed_limit_enabled"):
            limits.append(int(st.get("speed_limit_kbps") or 0))
        limits = [x for x in limits if x > 0]
        if limits:
            cmd += ["--limit-rate", f"{min(limits)}K"]
        cmd.append(item.url)
        flags = 0x08000000 if sys.platform == "win32" else 0
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=flags,
                                env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        tail: list[str] = []
        final_path = ""
        parts_progress = _PartsProgress(item, bool(item.media.get("audio_only")))
        item.stage = "Getting video information…"
        stopper = threading.Thread(target=self._kill_on_stop, args=(proc,), daemon=True)
        stopper.start()
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if line.startswith("[od] "):
                parts = line[5:].split("|", 4)
                num = [int(float(x)) if _is_num(x) else -1 for x in parts[:4]]
                fname = parts[4] if len(parts) > 4 and parts[4] not in ("NA", "") else ""
                item.status = Status.DOWNLOADING
                item.speed = float(max(0, num[3]))
                parts_progress.update(num[0], num[1] if num[1] > 0 else num[2], fname)
                if fname and not item.filename_locked:
                    item.filename = Path(fname).name
                self.maybe_persist()
            elif line.startswith("[od-plan] "):
                import json as _json

                f = line[10:].split("|")
                try:
                    vals = [_json.loads(x) if x not in ("NA", "") else None for x in f[:4]]
                    parts_progress.plan(vals[0] or [], vals[1] or [], vals[2] or [], vals[3], f[4] if len(f) > 4 else "")
                except (ValueError, IndexError):
                    pass
            elif line.startswith("[od-file] "):
                final_path = line[10:].strip()
            else:
                if "[Merger]" in line or "[ExtractAudio]" in line or "[VideoConvertor]" in line:
                    item.status = Status.PROCESSING
                tail = (tail + [line])[-15:]
        proc.wait()
        if self.stopping:
            return
        if proc.returncode != 0:
            msg = next((ln for ln in reversed(tail) if "ERROR" in ln), "\n".join(tail[-3:]) or "yt-dlp failed")
            if "DRM" in msg:
                raise ProtectedContentError()
            raise DownloadError(msg.replace("ERROR: ", ""))
        if final_path and Path(final_path).exists():
            item.filename = Path(final_path).name
            item.save_dir = str(Path(final_path).parent)
            item.size = item.downloaded = Path(final_path).stat().st_size
        item.stage = ""

    def _kill_on_stop(self, proc) -> None:
        while proc.poll() is None:
            if self.stop_event.wait(0.5):
                proc.terminate()
                try:
                    proc.wait(10)
                except Exception:  # noqa: BLE001
                    proc.kill()
                return

    def _run_module(self) -> None:
        import yt_dlp  # type: ignore

        item = self.item
        st = self.ctx.settings
        parts_progress = _PartsProgress(item, bool(item.media.get("audio_only")))

        def hook(d: dict[str, Any]) -> None:
            if self.stopping:
                raise yt_dlp.utils.DownloadCancelled("stopped")
            item.status = Status.DOWNLOADING
            info = d.get("info_dict") or {}
            if parts_progress.index < 0 and info.get("requested_formats"):
                rf = info["requested_formats"]
                parts_progress.plan([f.get("filesize") for f in rf], [f.get("filesize_approx") for f in rf],
                                    [f.get("tbr") for f in rf], info.get("duration"), info.get("format_id", ""))
            parts_progress.update(int(d.get("downloaded_bytes") or 0),
                                  int(d.get("total_bytes") or d.get("total_bytes_estimate") or 0), d.get("filename") or "")
            item.speed = float(d.get("speed") or 0)
            if d.get("filename") and not item.filename_locked:
                item.filename = Path(d["filename"]).name
            self.maybe_persist()

        opts = {
            "outtmpl": self._out_template(),
            "progress_hooks": [hook],
            "quiet": True, "no_warnings": True, "noprogress": True,
            "noplaylist": True, "continuedl": True,
            "format": self._format(),
            "http_headers": {"User-Agent": st.get("user_agent"), **({"Referer": item.referer} if item.referer else {})},
            "concurrent_fragment_downloads": int(st.get("media_connections", 6)),
        }
        ff = find_ffmpeg(st.get("ffmpeg_path", ""))
        if ff:
            opts["ffmpeg_location"] = ff
            opts["merge_output_format"] = "mp4"
        if st.get("proxy"):
            opts["proxy"] = st.get("proxy")
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(item.url, download=True)
                fn = ydl.prepare_filename(info)
                for cand in (Path(fn), Path(fn).with_suffix(".mp4"), Path(fn).with_suffix(".mkv"), Path(fn).with_suffix(".webm")):
                    if cand.exists():
                        item.filename = cand.name
                        item.size = item.downloaded = cand.stat().st_size
                        break
        except yt_dlp.utils.DownloadCancelled:
            return
        except yt_dlp.utils.DownloadError as e:
            msg = str(e)
            if "DRM" in msg:
                raise ProtectedContentError() from e
            raise DownloadError(msg) from e


# ---------------------------------------------------------------------------
def probe_media(settings, url: str, headers: dict[str, str] | None = None,
                referer: str = "") -> dict[str, Any]:
    """Inspect a media URL and return selectable quality options for the UI."""
    probe_item = DownloadItem(url=url, headers=headers or {}, referer=referer)
    s = make_session(settings, probe_item)
    timeout = float(settings.get("timeout_s", 30))
    out: dict[str, Any] = {"kind": "http", "options": [], "error": ""}
    low = url.split("?")[0].lower()
    try:
        if low.endswith(".mpd") or "dash" in (headers or {}).get("_mime", ""):
            text, real = _fetch_text(s, url, timeout)
            mpd = dash.parse_mpd(text, real)
            out["kind"] = "dash"
            audios = [a for a in mpd.audios() if not a.protected]
            best_audio = audios[0].id if audios else ""
            for v in mpd.videos():
                out["options"].append({
                    "label": v.label + ("  [DRM - not supported]" if v.protected else ""),
                    "protected": v.protected, "height": v.height,
                    "media": {"type": "dash", "url": url, "video": v.id, "audio": best_audio}})
            for a in audios:
                out["options"].append({"label": "Audio only - " + a.label, "protected": False, "height": 0,
                                       "media": {"type": "dash", "url": url, "video": "", "audio": a.id},
                                       "audio_only": True})
            if mpd.dynamic:
                out["error"] = "Live DASH streams are not supported."
        else:
            text, real = _fetch_text(s, url, timeout)
            if not text.lstrip().startswith("#EXTM3U"):
                out["options"].append({"label": "Direct file", "media": {}, "height": 0})
                return out
            out["kind"] = "hls"
            if hls.is_master(text):
                mp = hls.parse_master(text, real)
                seen: set[tuple[int, int]] = set()
                for v in mp.variants:
                    k = (v.height, v.bandwidth)
                    if k in seen:
                        continue
                    seen.add(k)
                    rend = mp.audio_for(v)
                    out["options"].append({
                        "label": v.label, "height": v.height,
                        "media": {"type": "hls", "url": url, "variant": v.url,
                                  "audio": rend.url if rend else None, "height": v.height}})
            else:
                pl = hls.parse_media(text, real)
                mins = int(pl.total_duration // 60)
                label = "Live stream (records until you press Stop)" if not pl.endlist else f"Stream  ({mins} min)"
                out["options"].append({"label": label, "height": 0,
                                       "media": {"type": "hls", "url": url}})
    except ProtectedContentError as e:
        out["error"] = str(e)
        out["protected"] = True
    except (DownloadError, requests.RequestException, ValueError) as e:
        out["error"] = str(e)
    finally:
        s.close()
    return out


# ---------------------------------------------------------------------------
def _page_options(info: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn yt-dlp's format list into a short list of choices (one per resolution).
    Note: yt-dlp uses "none" for 'definitely absent' and None for 'unknown'."""
    formats = info.get("formats") or []
    duration = float(info.get("duration") or 0)

    def size(f: dict[str, Any]) -> int:
        s = f.get("filesize") or f.get("filesize_approx")
        if not s and f.get("tbr") and duration:
            s = float(f["tbr"]) * 1000 / 8 * duration
        return int(s or 0)

    audio = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
    best_audio = max((size(f) for f in audio), default=0)
    by_height: dict[int, dict[str, Any]] = {}
    for f in formats:
        h = f.get("height")
        if not h or f.get("vcodec") == "none":
            continue
        cur = by_height.setdefault(int(h), {"size": 0, "fps": 0, "has_audio": False})
        cur["size"] = max(cur["size"], size(f))
        cur["fps"] = max(cur["fps"], int(f.get("fps") or 0))
        cur["has_audio"] = cur["has_audio"] or f.get("acodec") != "none"
    opts = []
    for h in sorted(by_height, reverse=True):
        d = by_height[h]
        total = d["size"] + (0 if d["has_audio"] else best_audio)
        label = f"{h}p" + (str(d["fps"]) if d["fps"] > 30 else "")
        if total:
            label += f"   ~{human_size(total)}"
        opts.append({"label": label, "height": h, "media": {"height": h}})
    if audio:
        label = "Audio only (best quality)" + (f"   ~{human_size(best_audio)}" if best_audio else "")
        opts.append({"label": label, "height": 0, "media": {"audio_only": True}, "audio_only": True})
    if not opts:
        opts.append({"label": "Best available quality", "height": 0, "media": {}})
    return opts


def probe_page(settings, url: str, referer: str = "") -> dict[str, Any]:
    """Ask yt-dlp which qualities a web-page video (YouTube, Vimeo, ...) offers."""
    import json
    import subprocess

    from .tools import find_ytdlp_binary, ytdlp_module_available

    out: dict[str, Any] = {"title": "", "options": [], "error": ""}
    try:
        exe = find_ytdlp_binary()
        if exe:
            cmd = [exe, "-J", "--no-playlist", "--no-warnings", "--user-agent", settings.get("user_agent")]
            if referer:
                cmd += ["--referer", referer]
            if settings.get("proxy"):
                cmd += ["--proxy", settings.get("proxy")]
            proc = subprocess.run(cmd + [url], capture_output=True, timeout=120,
                                  creationflags=0x08000000 if sys.platform == "win32" else 0,
                                  env={**os.environ, "PYTHONUTF8": "1"})
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
                raise RuntimeError(next((ln for ln in reversed(err) if "ERROR" in ln), err[-1] if err else "yt-dlp failed"))
            info = json.loads(proc.stdout.decode("utf-8", "replace"))
        elif ytdlp_module_available():
            import yt_dlp  # type: ignore

            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True}) as ydl:
                info = ydl.sanitize_info(ydl.extract_info(url, download=False))
        else:
            raise RuntimeError("yt-dlp is not installed")
        if info.get("_type") == "playlist" and info.get("entries"):
            info = info["entries"][0]
        out["title"] = info.get("title") or ""
        out["duration"] = info.get("duration") or 0
        out["options"] = _page_options(info)
    except Exception as e:  # noqa: BLE001
        msg = str(e).replace("ERROR: ", "")
        out["error"] = ("This video is DRM-protected and can't be downloaded." if "DRM" in msg else msg)
    return out
