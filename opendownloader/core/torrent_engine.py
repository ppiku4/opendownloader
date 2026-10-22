"""BitTorrent downloads (magnet links and .torrent files), built on libtorrent.

One libtorrent session is shared by all torrent downloads. Each torrent download
runs in its own `TorrentDownloader` thread (like every other download) that adds
the torrent to the session, reports progress and removes it again when it stops.

Seeding: by default a torrent stops as soon as the chosen files are complete. If
the user enabled seeding in Options, the finished torrent stays in the session
(outside the download queue) until the chosen ratio or time is reached.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .. import __version__, paths
from ..config import Settings
from .base import BaseDownloader, DownloadError
from .models import DownloadItem, Status

log = logging.getLogger(__name__)

try:  # optional: builds without libtorrent simply have no torrent support
    import libtorrent as lt
except Exception:  # noqa: BLE001 - ImportError, or a DLL that fails to load
    lt = None  # type: ignore[assignment]

UNAVAILABLE = ("Torrent support isn't included in this copy of OpenDownloader "
               "(the libtorrent library is missing).")
MAX_TORRENT_FILE = 20 * 1024 * 1024
SEED_MODES = ("none", "ratio", "time")


def available() -> bool:
    return lt is not None


def is_magnet(url: str) -> bool:
    return (url or "").strip().lower().startswith("magnet:?")


def is_torrent_link(url: str, filename: str = "", mime: str = "") -> bool:
    """True for a magnet link, a .torrent file name/URL, or the BitTorrent mime type."""
    if is_magnet(url):
        return True
    if (filename or "").lower().endswith(".torrent") or "bittorrent" in (mime or "").lower():
        return True
    return urlsplit(url or "").path.lower().endswith(".torrent")


# ---------------------------------------------------------------------- files
def torrents_dir() -> Path:
    d = paths.app_data_dir() / "torrents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def torrent_path(item_id: int) -> Path:
    return torrents_dir() / f"{item_id}.torrent"


def resume_path(item_id: int) -> Path:
    return torrents_dir() / f"{item_id}.resume"


@dataclass
class TorrentMeta:
    name: str
    info_hash: str
    total_size: int = -1
    files: list[tuple[int, str, int]] = field(default_factory=list)  # (file index, path inside torrent, size)
    num_files: int = 0                                             # including hidden padding files
    keys: list[str] = field(default_factory=list)                  # every info-hash form (v1, v2)

    def matches(self, other_keys: list[str]) -> bool:
        return any(same_torrent(a, b) for a in self.keys for b in other_keys)
    data: bytes = b""                                              # .torrent file contents ("" = not known yet)

    @property
    def root(self) -> str:
        """First path component on disk: the file (single-file torrent) or folder."""
        if self.files:
            return self.files[0][1].replace("\\", "/").split("/")[0]
        return self.name


def _hash_str(ih: Any) -> str:
    try:
        return str(ih.get_best())
    except AttributeError:
        return str(ih)


# Torrents fetched from a magnet link may not have the v2 "piece layers" yet (peers send those
# while downloading); still allow writing the .torrent file.
_WRITE_FLAGS = getattr(getattr(lt, "write_flags", None), "allow_missing_piece_layer", 0) if lt else 0


def _torrent_bytes(atp: Any) -> bytes:
    # write_torrent_file_buf() returns text in the Python bindings (breaks on binary hashes)
    return bytes(lt.bencode(lt.write_torrent_file(atp, _WRITE_FLAGS)))


def _files(ti: Any) -> Any:
    """File list of a torrent_info (`layout()` in libtorrent 2.1+, `files()` before)."""
    return ti.layout() if hasattr(ti, "layout") else ti.files()


def _keys(ih: Any) -> set[str]:
    """All names of a torrent: v1 hash, v2 hash (hybrid torrents have both) and the 'best' one."""
    out = set()
    for attr in ("v1", "v2"):
        v = getattr(ih, attr, None)
        if v is not None:
            out.add(str(v))
    try:
        out.add(str(ih.get_best()))
    except AttributeError:
        out.add(str(ih))
    return {k for k in out if k and set(k) != {"0"}}


def same_torrent(a: str, b: str) -> bool:
    """Compare two info-hash strings (a v2 hash may be shown shortened to 40 characters)."""
    a, b = (a or "").lower(), (b or "").lower()
    return bool(a and b) and (a == b or a[:40] == b[:40])


def parse_torrent(data: bytes) -> TorrentMeta:
    if lt is None:
        raise ValueError(UNAVAILABLE)
    if not data or len(data) > MAX_TORRENT_FILE:
        raise ValueError("This isn't a valid .torrent file.")
    try:
        ti = lt.torrent_info(data)
    except RuntimeError as e:
        raise ValueError(f"This isn't a valid .torrent file ({e}).") from None
    fs = _files(ti)
    pad = lt.file_storage.flag_pad_file
    files = [(i, fs.file_path(i), fs.file_size(i)) for i in range(fs.num_files()) if not fs.file_flags(i) & pad]
    return TorrentMeta(name=ti.name(), info_hash=_hash_str(ti.info_hashes()),
                       total_size=sum(f[2] for f in files), files=files, num_files=fs.num_files(),
                       data=bytes(data), keys=sorted(_keys(ti.info_hashes())))


def parse_magnet(url: str) -> TorrentMeta:
    if lt is None:
        raise ValueError(UNAVAILABLE)
    try:
        p = lt.parse_magnet_uri(url.strip())
    except RuntimeError as e:
        raise ValueError(f"This magnet link isn't valid ({e}).") from None
    ih = _hash_str(p.info_hashes)
    if not ih or set(ih) == {"0"}:
        raise ValueError("This magnet link has no torrent hash.")
    return TorrentMeta(name=p.name or "", info_hash=ih, keys=sorted(_keys(p.info_hashes)))


def delete_data(item: DownloadItem) -> None:
    """Delete the files a torrent download wrote - only those files, never other files
    that happen to be in the same folder."""
    tp = torrent_path(item.id) if item.id is not None else None
    if tp is None or not tp.exists():
        return
    try:
        meta = parse_torrent(tp.read_bytes())
    except (OSError, ValueError):
        return
    base = Path(item.save_dir)
    dirs: set[Path] = set()
    for _idx, rel, _size in meta.files:
        f = base / rel
        try:
            f.unlink(missing_ok=True)
        except OSError:
            log.warning("could not delete %s", f)
        p = f.parent
        while p != base and base in p.parents:
            dirs.add(p)
            p = p.parent
    for d in sorted(dirs, key=lambda x: len(x.parts), reverse=True):
        try:
            d.rmdir()          # only if empty
        except OSError:
            pass


def forget(item_id: int) -> None:
    for p in (torrent_path(item_id), resume_path(item_id)):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------- session
def _session_settings(settings: Settings) -> dict[str, Any]:
    port = int(settings.get("torrent_port", 0) or 0)
    iface = f"0.0.0.0:{port},[::]:{port}"
    down = int(settings.get("speed_limit_kbps", 0)) * 1024 if settings.get("speed_limit_enabled") else 0
    s: dict[str, Any] = {
        "user_agent": f"OpenDownloader/{__version__} libtorrent/{lt.__version__}",
        "listen_interfaces": iface,
        "enable_dht": bool(settings.get("torrent_dht", True)),
        "enable_lsd": bool(settings.get("torrent_dht", True)),
        "enable_upnp": bool(settings.get("torrent_upnp", True)),
        "enable_natpmp": bool(settings.get("torrent_upnp", True)),
        "download_rate_limit": down,
        "upload_rate_limit": int(settings.get("torrent_upload_limit_kbps", 0) or 0) * 1024,
        "connections_limit": int(settings.get("torrent_max_connections", 200) or 200),
        "alert_mask": int(lt.alert_category.error | lt.alert_category.status | lt.alert_category.storage),
    }
    proxy = (settings.get("proxy") or "").strip()
    if proxy:
        u = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
        kinds = {"socks5": lt.proxy_type_t.socks5, "socks5h": lt.proxy_type_t.socks5,
                 "socks4": lt.proxy_type_t.socks4, "http": lt.proxy_type_t.http, "https": lt.proxy_type_t.http}
        if u.scheme in kinds and u.hostname:
            s.update({"proxy_type": int(kinds[u.scheme]), "proxy_hostname": u.hostname,
                      "proxy_port": u.port or (1080 if u.scheme.startswith("socks") else 8080),
                      "proxy_peer_connections": True, "proxy_tracker_connections": True})
            if u.username:
                s["proxy_type"] = int(lt.proxy_type_t.socks5_pw if u.scheme.startswith("socks5")
                                      else lt.proxy_type_t.http_pw)
                s["proxy_username"] = u.username
                s["proxy_password"] = u.password or ""
    return s


class _Seed:
    def __init__(self, handle: Any, item: DownloadItem, mode: str, ratio: float, minutes: int):
        self.handle, self.item = handle, item
        self.mode, self.ratio, self.minutes = mode, ratio, minutes
        self.started = time.monotonic()


class TorrentSession:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ses = lt.session(_session_settings(settings))
        self.lock = threading.RLock()
        self._waiters: dict[tuple[str, str], threading.Event] = {}
        self._results: dict[tuple[str, str], Any] = {}
        self.seeding: dict[int, _Seed] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True, name="torrent-alerts")
        self._thread.start()

    # --------------------------------------------------------------- basics
    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        try:
            self.ses.apply_settings(_session_settings(settings))
        except Exception:  # noqa: BLE001
            log.exception("could not apply torrent settings")

    def find(self, info_hash: str) -> Any | None:
        for h in self.ses.get_torrents():
            if h.is_valid() and any(same_torrent(info_hash, k) for k in _keys(h.info_hashes())):
                return h
        return None

    def add(self, atp: Any) -> Any:
        return self.ses.add_torrent(atp)

    def remove(self, handle: Any, wait: float = 0) -> None:
        """Remove a torrent from the session. Removal is asynchronous; with `wait`, block
        until it's gone so the same torrent can be added again right away."""
        try:
            if not handle.is_valid():
                return
            key = _hash_str(handle.info_hashes())
            self.ses.remove_torrent(handle)
        except Exception:  # noqa: BLE001
            log.exception("remove_torrent failed")
            return
        end = time.monotonic() + wait
        while wait and time.monotonic() < end and self.find(key) is not None:
            time.sleep(0.05)

    def flush(self, handle: Any, timeout: float = 60) -> bool:
        """Write everything libtorrent still holds in memory to the files and close them (waits for it).

        libtorrent 2.1 reports pieces as finished once they're verified, which can be before they
        reach the disk; without this a "complete" file can still contain unwritten (zero) ranges.
        """
        if not handle.is_valid():
            return False
        key = ("flush", _hash_str(handle.info_hashes()))
        ev = threading.Event()
        with self.lock:
            self._waiters[key] = ev
        try:
            handle.flush_cache()
            return ev.wait(timeout)
        except Exception:  # noqa: BLE001
            return False
        finally:
            with self.lock:
                self._waiters.pop(key, None)

    def save_resume(self, handle: Any, dest: Path, timeout: float = 10) -> bool:
        """Ask libtorrent for resume data and write it to `dest` (waits for the alert)."""
        if not handle.is_valid():
            return False
        key = ("resume", _hash_str(handle.info_hashes()))
        ev = threading.Event()
        with self.lock:
            self._waiters[key] = ev
            self._results.pop(key, None)
        try:
            handle.save_resume_data(lt.torrent_handle.save_info_dict)
        except Exception:  # noqa: BLE001
            with self.lock:
                self._waiters.pop(key, None)
            return False
        ok = ev.wait(timeout)
        with self.lock:
            self._waiters.pop(key, None)
            data = self._results.pop(key, None)
        if ok and data:
            try:
                tmp = dest.with_suffix(".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, dest)
                return True
            except OSError:
                log.exception("could not write resume data")
        return False

    # --------------------------------------------------------------- seeding
    def start_seeding(self, item: DownloadItem, handle: Any) -> bool:
        st = self.settings
        mode = st.get("torrent_seed_mode", "none")
        if mode not in ("ratio", "time"):
            return False
        with self.lock:
            self.seeding[item.id] = _Seed(handle, item, mode, float(st.get("torrent_seed_ratio", 1.0) or 1.0),
                                          int(st.get("torrent_seed_minutes", 60) or 60))
        item.stage = "Seeding"
        return True

    def stop_seeding(self, item_id: int) -> None:
        with self.lock:
            seed = self.seeding.pop(item_id, None)
        if seed is not None:
            seed.item.stage = ""
            self.remove(seed.handle)

    def _check_seeding(self) -> None:
        from ..util import human_speed

        for iid, seed in list(self.seeding.items()):
            try:
                s = seed.handle.status()
            except Exception:  # noqa: BLE001
                self.stop_seeding(iid)
                continue
            size = max(1, s.total_wanted)
            ratio = s.all_time_upload / size
            elapsed = time.monotonic() - seed.started
            if (seed.mode == "ratio" and ratio >= seed.ratio) or (seed.mode == "time" and elapsed >= seed.minutes * 60):
                self.stop_seeding(iid)
                continue
            up = human_speed(s.upload_payload_rate) if s.upload_payload_rate > 0 else ""
            seed.item.stage = f"Seeding · ratio {ratio:.2f}" + (f" · ↑ {up}" if up else "")

    # --------------------------------------------------------------- alerts
    def _pump(self) -> None:
        last_seed_check = 0.0
        while not self._stop.is_set():
            try:
                self.ses.wait_for_alert(500)
                for a in self.ses.pop_alerts():
                    self._on_alert(a)
                now = time.monotonic()
                if now - last_seed_check >= 1.0:
                    last_seed_check = now
                    self._check_seeding()
            except Exception:  # noqa: BLE001
                log.exception("torrent alert loop")
                time.sleep(1)

    def _on_alert(self, a: Any) -> None:
        if isinstance(a, lt.save_resume_data_alert):
            key = ("resume", _hash_str(a.handle.info_hashes()))
            with self.lock:
                if key in self._waiters:
                    self._results[key] = bytes(lt.write_resume_data_buf(a.params))
                    self._waiters[key].set()
        elif isinstance(a, lt.cache_flushed_alert):
            key = ("flush", _hash_str(a.handle.info_hashes()))
            with self.lock:
                if key in self._waiters:
                    self._waiters[key].set()
        elif isinstance(a, lt.save_resume_data_failed_alert):
            key = ("resume", _hash_str(a.handle.info_hashes()))
            with self.lock:
                if key in self._waiters:
                    self._waiters[key].set()
        elif a.category() & lt.alert_category.error:
            log.info("torrent: %s", a.message())

    def shutdown(self) -> None:
        for iid in list(self.seeding):
            self.stop_seeding(iid)
        self._stop.set()
        self._thread.join(2)


_session: TorrentSession | None = None
_session_lock = threading.Lock()


def session(settings: Settings) -> TorrentSession:
    global _session
    if lt is None:
        raise DownloadError(UNAVAILABLE, fatal=True)
    with _session_lock:
        if _session is None:
            _session = TorrentSession(settings)
        return _session


def apply_settings(settings: Settings) -> None:
    if _session is not None:
        _session.apply_settings(settings)


def is_seeding(item_id: int) -> bool:
    return _session is not None and item_id in _session.seeding


def reannounce_handle(h: Any) -> None:
    """Contact all trackers now (ignoring their minimum interval) and announce on DHT/LSD."""
    h.force_reannounce(0, -1, lt.reannounce_flags_t.ignore_min_interval)
    for fn in ("force_dht_announce", "force_lsd_announce"):
        try:
            getattr(h, fn)()
        except Exception:  # noqa: BLE001 - DHT/LSD switched off
            pass


def reannounce_seeding(item_id: int) -> bool:
    s = _session
    seed = s.seeding.get(item_id) if s is not None else None
    if seed is None or not seed.handle.is_valid():
        return False
    reannounce_handle(seed.handle)
    return True


def stop_seeding(item_id: int) -> None:
    if _session is not None:
        _session.stop_seeding(item_id)


def shutdown() -> None:
    global _session
    with _session_lock:
        if _session is not None:
            _session.shutdown()
            _session = None


# ---------------------------------------------------------------------- metadata
def fetch_metadata(settings: Settings, magnet: str, cancel: threading.Event,
                   timeout: float = 0) -> TorrentMeta:
    """Get the file list of a magnet link from peers (without downloading any file data)."""
    meta = parse_magnet(magnet)
    s = session(settings)
    existing = s.find(meta.info_hash)
    if existing is not None and existing.status().has_metadata:
        return _meta_from_handle(existing, magnet)
    atp = lt.parse_magnet_uri(magnet.strip())
    atp.save_path = str(paths.temp_dir() / "torrent-metadata")
    atp.flags = ((atp.flags | lt.torrent_flags.default_dont_download | lt.torrent_flags.upload_mode)
                 & ~lt.torrent_flags.auto_managed & ~lt.torrent_flags.paused)
    try:
        h = s.add(atp)
    except RuntimeError as e:
        raise ValueError(f"Couldn't open this magnet link ({e}).") from None
    t0 = time.monotonic()
    try:
        while not cancel.is_set():
            if h.status().has_metadata:
                return _meta_from_handle(h, magnet)
            if timeout and time.monotonic() - t0 > timeout:
                raise TimeoutError("No peers answered with the torrent information.")
            cancel.wait(0.3)
        raise InterruptedError
    finally:
        s.remove(h, wait=5)


def _meta_from_handle(h: Any, magnet: str) -> TorrentMeta:
    ti = h.torrent_file()
    atp = lt.add_torrent_params()
    atp.ti = ti
    try:
        atp.trackers = list(lt.parse_magnet_uri(magnet.strip()).trackers)
    except Exception:  # noqa: BLE001
        pass
    return parse_torrent(_torrent_bytes(atp))


# ---------------------------------------------------------------------- downloader
class TorrentDownloader(BaseDownloader):
    """Downloads one torrent. The item's url is the magnet link (or the .torrent's
    original address); the .torrent itself is kept in torrents/<id>.torrent."""

    def __init__(self, item: DownloadItem, ctx: Any, recheck: bool = False):
        super().__init__(item, ctx)
        self.handle: Any = None
        self.user_paused = False      # paused in libtorrent; the torrent stays loaded and resumes instantly
        self.recheck_on_start = recheck

    # ------------------------------------------------- user commands (UI thread)
    def pause_torrent(self) -> None:
        h = self.handle
        if h is not None and h.is_valid():
            h.pause()
        self.user_paused = True

    def resume_torrent(self) -> None:
        self.user_paused = False
        h = self.handle
        if h is not None and h.is_valid():
            h.resume()

    def recheck(self) -> None:
        """Verify the downloaded data against the torrent's hashes again."""
        h = self.handle
        if h is None or not h.is_valid():
            self.recheck_on_start = True     # still starting: check as soon as the torrent is added
            return
        if self.user_paused:
            self.resume_torrent()
        h.force_recheck()

    def reannounce(self) -> None:
        h = self.handle
        if h is not None and h.is_valid() and not self.user_paused:
            reannounce_handle(h)

    def set_speed_limit(self, kbps: int) -> None:
        super().set_speed_limit(kbps)
        h = self.handle
        if h is not None and h.is_valid():
            h.set_download_limit(kbps * 1024 if kbps > 0 else -1)

    def _params(self) -> Any:
        item = self.item
        atp = None
        rp, tp = resume_path(item.id), torrent_path(item.id)
        want = list((item.media or {}).get("info_hashes") or [])
        if (item.media or {}).get("info_hash"):
            want.append(item.media["info_hash"])
        if is_magnet(item.url):
            try:
                want += parse_magnet(item.url).keys
            except ValueError:
                pass
        if rp.exists():
            try:
                atp = lt.read_resume_data(rp.read_bytes())
                if want and not any(same_torrent(w, k) for w in want for k in _keys(atp.info_hashes)):
                    raise ValueError("resume data belongs to another torrent")
            except Exception:  # noqa: BLE001 - bad resume data: start from the .torrent (files are re-checked)
                log.warning("ignoring unusable resume data for %s", item.id)
                atp = None
        if atp is None:
            if tp.exists():
                atp = lt.add_torrent_params()
                atp.ti = lt.torrent_info(tp.read_bytes())
            elif is_magnet(item.url):
                atp = lt.parse_magnet_uri(item.url.strip())
            else:
                raise DownloadError("The .torrent file of this download is missing.", fatal=True)
            prios = (item.media or {}).get("priorities")
            if prios and atp.ti is not None and len(prios) == atp.ti.num_files():
                atp.file_priorities = [int(p) for p in prios]
        if is_magnet(item.url):
            try:
                extra = list(lt.parse_magnet_uri(item.url.strip()).trackers)
                atp.trackers = list(dict.fromkeys(list(atp.trackers) + extra))
            except Exception:  # noqa: BLE001
                pass
        atp.save_path = item.save_dir
        atp.flags = (atp.flags & ~lt.torrent_flags.auto_managed & ~lt.torrent_flags.paused
                     & ~lt.torrent_flags.upload_mode)
        return atp

    def _on_metadata(self, h: Any) -> None:
        item = self.item
        ti = h.torrent_file()
        tp = torrent_path(item.id)
        if not tp.exists():
            atp = lt.add_torrent_params()
            atp.ti = ti
            try:
                tp.write_bytes(_torrent_bytes(atp))
            except OSError:
                log.exception("could not save .torrent")
        fs = _files(ti)
        if fs.num_files():
            item.filename = fs.file_path(0).replace("\\", "/").split("/")[0]
        prios = (item.media or {}).get("priorities")
        if prios and len(prios) == fs.num_files() and list(h.get_file_priorities()) != list(prios):
            h.prioritize_files([int(p) for p in prios])
        item.media = {**(item.media or {}), "info_hash": _hash_str(ti.info_hashes()),
                      "info_hashes": sorted(_keys(ti.info_hashes())), "files": fs.num_files()}
        self.maybe_persist(force=True)

    def download(self) -> None:
        if lt is None:
            raise DownloadError(UNAVAILABLE, fatal=True)
        item = self.item
        s = session(self.ctx.settings)
        Path(item.save_dir).mkdir(parents=True, exist_ok=True)
        atp = self._params()
        key = _hash_str(atp.ti.info_hashes() if atp.ti is not None else atp.info_hashes)
        existing = s.find(key)
        if existing is not None:
            # still seeding from an earlier run, or a metadata lookup that is being closed
            s.stop_seeding(item.id)
            s.remove(existing, wait=5)
            if s.find(key) is not None:
                raise DownloadError("This torrent is already being downloaded.", fatal=True)
        try:
            h = s.add(atp)
        except RuntimeError as e:
            raise DownloadError(f"Couldn't start this torrent ({e}).", fatal=True) from None
        self.handle = h
        if item.speed_limit_kbps > 0:
            h.set_download_limit(item.speed_limit_kbps * 1024)
        if self.recheck_on_start:
            self.recheck_on_start = False
            h.force_recheck()
        if self.user_paused:
            h.pause()
        states = lt.torrent_status.states
        checking = {states.checking_files, states.checking_resume_data}
        got_meta = False
        finished = False
        try:
            while not self.stopping:
                st = h.status()
                if st.errc.value():
                    where = f" ({st.error_file})" if isinstance(st.error_file, str) and st.error_file else ""
                    raise DownloadError(f"{st.errc.message()}{where}")
                if st.has_metadata and not got_meta:
                    got_meta = True
                    self._on_metadata(h)
                if st.has_metadata:
                    item.size = st.total_wanted
                    item.downloaded = st.total_wanted_done
                item.speed = float(st.download_payload_rate)
                item.active_connections = st.num_peers
                if self.user_paused and st.state not in checking:
                    item.status = Status.PAUSED
                    item.stage = "Paused (Resume continues instantly)"
                    item.speed = 0.0
                    self.maybe_persist()
                    self.stop_event.wait(0.5)
                    continue
                if st.state in checking:
                    item.status = Status.CONNECTING
                    item.stage = f"Checking existing data… {st.progress * 100:.0f}%"
                elif not st.has_metadata:
                    item.status = Status.CONNECTING
                    item.stage = (f"Getting torrent information… ({st.num_peers} peers)" if st.num_peers
                                  else "Looking for peers…")
                else:
                    item.status = Status.DOWNLOADING
                    item.stage = (f"Downloading from {st.num_peers} peers ({st.num_seeds} seeds)" if st.num_peers
                                  else "Looking for peers…")
                if st.has_metadata and st.is_finished and st.state not in checking:
                    finished = True
                    break
                self.maybe_persist()
                self.stop_event.wait(0.5)
        except BaseException:
            s.save_resume(h, resume_path(item.id))
            s.remove(h)
            self.handle = None
            item.stage = ""
            raise
        item.speed = 0.0
        item.active_connections = 0
        item.stage = ""
        if finished and not s.flush(h):
            log.warning("torrent %s: libtorrent didn't confirm writing all data before completing", item.id)
        s.save_resume(h, resume_path(item.id))
        self.handle = None
        if finished:
            item.downloaded = max(item.downloaded, item.size)
            if not s.start_seeding(item, h):
                s.remove(h)
        else:
            s.remove(h)
