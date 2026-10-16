"""BitTorrent downloads against a local libtorrent seeder (no internet needed)."""
import os
import threading
import time
from pathlib import Path

import pytest

lt = pytest.importorskip("libtorrent")

from conftest import sha, wait_for  # noqa: E402

from opendownloader.core import torrent_engine  # noqa: E402
from opendownloader.core.models import Kind, Status  # noqa: E402


@pytest.fixture()
def seeder(tmp_path):
    """A local seeder with a 3-file torrent. Yields (torrent bytes, source folder, port, info hash)."""
    src = tmp_path / "seed"
    (src / "Pack" / "sub").mkdir(parents=True)
    files = {"Pack/a.bin": os.urandom(1_500_000), "Pack/sub/b.bin": os.urandom(700_000),
             "Pack/c.txt": b"hello torrent\n" * 50_000}
    for rel, data in files.items():
        (src / rel).write_bytes(data)
    fs = lt.file_storage()
    for rel, data in sorted(files.items()):
        fs.add_file(rel, len(data))
    ct = lt.create_torrent(fs, 16 * 1024)
    lt.set_piece_hashes(ct, str(src))
    data = lt.bencode(ct.generate())
    ses = lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False, "enable_lsd": False,
                      "enable_upnp": False, "enable_natpmp": False})
    atp = lt.add_torrent_params()
    atp.ti = lt.torrent_info(data)
    atp.save_path = str(src)
    atp.flags = (atp.flags | lt.torrent_flags.seed_mode) & ~lt.torrent_flags.auto_managed & ~lt.torrent_flags.paused
    h = ses.add_torrent(atp)
    assert wait_for(lambda: ses.listen_port() > 0, 10)
    yield data, src, files, ses.listen_port(), str(h.info_hashes().get_best())
    ses.remove_torrent(h)
    del ses


def _connect_forever(port, stop):
    """Stand-in for trackers/DHT: keep pointing every torrent in our session at the seeder."""
    while not stop.is_set():
        s = torrent_engine._session
        if s is not None:
            for h in s.ses.get_torrents():
                try:
                    if h.status().num_peers == 0:
                        h.connect_peer(("127.0.0.1", port))
                except Exception:
                    pass
        stop.wait(1.0)


@pytest.fixture()
def tmanager(manager):
    manager.settings.update({"torrent_dht": False, "torrent_upnp": False, "torrent_seed_mode": "none"})
    yield manager


def _add(m, data, save_dir, priorities=None, url="https://example.com/pack.torrent"):
    meta = torrent_engine.parse_torrent(data)
    item = m.add(url, filename=meta.root, save_dir=str(save_dir), kind=Kind.TORRENT.value,
                 media={"info_hash": meta.info_hash, "priorities": priorities}, start=False)
    torrent_engine.torrent_path(item.id).write_bytes(data)
    return item


def test_parse_and_detect(seeder):
    data, _src, files, _port, ih = seeder
    meta = torrent_engine.parse_torrent(data)
    assert meta.name == "Pack" and meta.root == "Pack" and meta.info_hash == ih
    assert sorted(p.replace("\\", "/") for _, p, _ in meta.files) == sorted(files)
    assert meta.total_size == sum(len(v) for v in files.values())
    mag = torrent_engine.parse_magnet(f"magnet:?xt=urn:btih:{ih}&dn=Pack")
    assert mag.info_hash == ih and mag.name == "Pack"
    assert torrent_engine.is_torrent_link("magnet:?xt=urn:btih:" + ih)
    assert torrent_engine.is_torrent_link("https://x.org/dl?id=1", filename="a.torrent")
    assert torrent_engine.is_torrent_link("https://x.org/files/a.TORRENT?x=1")
    assert not torrent_engine.is_torrent_link("https://x.org/a.zip")
    with pytest.raises(ValueError):
        torrent_engine.parse_torrent(b"not a torrent")
    with pytest.raises(ValueError):
        torrent_engine.parse_magnet("magnet:?dn=nohash")


def test_download_selected_files(seeder, tmanager, tmp_path):
    data, _src, files, port, _ih = seeder
    out = tmp_path / "out"
    meta = torrent_engine.parse_torrent(data)
    prios = [4] * meta.num_files
    for idx, p, _ in meta.files:
        if p.replace("\\", "/").endswith("c.txt"):
            prios[idx] = 0
    item = _add(tmanager, data, out, prios)
    stop = threading.Event()
    threading.Thread(target=_connect_forever, args=(port, stop), daemon=True).start()
    try:
        tmanager.start(item.id)
        assert wait_for(lambda: item.status == Status.COMPLETE, 60), (item.status, item.error, item.stage)
    finally:
        stop.set()
    assert item.path == out / "Pack"
    assert sha((out / "Pack/a.bin").read_bytes()) == sha(files["Pack/a.bin"])
    assert sha((out / "Pack/sub/b.bin").read_bytes()) == sha(files["Pack/sub/b.bin"])
    c = out / "Pack/c.txt"
    assert not c.exists() or c.read_bytes() != files["Pack/c.txt"]   # not selected
    assert item.size == len(files["Pack/a.bin"]) + len(files["Pack/sub/b.bin"])
    assert item.downloaded == item.size
    assert not torrent_engine.is_seeding(item.id)        # default: stop when done


def test_magnet_metadata_pause_resume_and_seeding(seeder, tmanager, tmp_path):
    data, _src, files, port, ih = seeder
    stop = threading.Event()
    threading.Thread(target=_connect_forever, args=(port, stop), daemon=True).start()
    try:
        magnet = f"magnet:?xt=urn:btih:{ih}&dn=Pack"
        meta = torrent_engine.fetch_metadata(tmanager.settings, magnet, threading.Event(), timeout=30)
        assert meta.info_hash == ih and len(meta.files) == 3 and meta.data
        assert torrent_engine._session.find(ih) is None            # lookup doesn't linger

        # magnet download without a stored .torrent: metadata comes from the peer again
        tmanager.settings.update({"torrent_seed_mode": "ratio", "torrent_seed_ratio": 5.0})
        tmanager.set_item_speed_limit  # noqa: B018 - exists
        out = tmp_path / "out2"
        item = tmanager.add(magnet, filename="Pack", save_dir=str(out), kind=Kind.TORRENT.value, start=False)
        item.speed_limit_kbps = 400                               # slow enough to pause mid-way
        tmanager.start(item.id)
        assert wait_for(lambda: item.downloaded > 200_000, 60), (item.status, item.error, item.stage)
        tmanager.pause(item.id)
        assert wait_for(lambda: item.status == Status.PAUSED, 15)
        assert torrent_engine.resume_path(item.id).exists()
        assert torrent_engine.torrent_path(item.id).exists()       # saved once metadata arrived
        done_before = item.downloaded
        tmanager.set_item_speed_limit(item.id, 0)
        tmanager.start(item.id)
        assert wait_for(lambda: item.status == Status.COMPLETE, 60), (item.status, item.error, item.stage)
        assert item.downloaded >= done_before
        for rel, blob in files.items():
            assert sha((out / rel).read_bytes()) == sha(blob)
        assert torrent_engine.is_seeding(item.id) and item.stage.startswith("Seeding")
        torrent_engine.stop_seeding(item.id)
        assert not torrent_engine.is_seeding(item.id) and item.stage == ""
    finally:
        stop.set()


def test_remove_unfinished_deletes_only_torrent_files(seeder, tmanager, tmp_path):
    data, _src, _files, _port, _ih = seeder
    out = tmp_path / "out3"
    (out / "Pack").mkdir(parents=True)
    keep = out / "Pack" / "mine.txt"
    keep.write_text("not part of the torrent")
    item = _add(tmanager, data, out)
    for rel in ("Pack/a.bin", "Pack/sub/b.bin"):
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_bytes(b"partial")
    tmanager.remove([item.id])
    assert keep.exists()
    assert not (out / "Pack/a.bin").exists() and not (out / "Pack/sub").exists()
    assert not torrent_engine.torrent_path(item.id).exists()
