import hashlib
import os
import time

from conftest import sha, wait_for
from opendownloader.core.models import Status


def _data(n):
    return os.urandom(n)


def test_multi_connection_download_with_dynamic_split(server, manager):
    base, files = server
    body = _data(6 * 1024 * 1024 + 123)
    files["/big.bin"] = (body, {"delay": 0.01})
    manager.settings["max_connections"] = 8
    item = manager.add(base + "/big.bin")
    seen_conns = []
    ok = wait_for(lambda: (seen_conns.append(item.active_connections), item.status == Status.COMPLETE)[1]
                  or item.status == Status.ERROR, 60)
    assert ok and item.status == Status.COMPLETE, item.error
    assert max(seen_conns) > 1, "should have split into several connections"
    assert sha(item.path.read_bytes()) == sha(body)
    assert item.filename == "big.bin"
    assert item.resumable


def test_pause_and_resume(server, manager):
    base, files = server
    body = _data(4 * 1024 * 1024)
    files["/r.bin"] = (body, {"delay": 0.05})
    manager.settings["max_connections"] = 2
    item = manager.add(base + "/r.bin")
    assert wait_for(lambda: item.downloaded > 300 * 1024, 20)
    manager.pause(item.id)
    assert wait_for(lambda: item.status == Status.PAUSED, 10)
    got = item.downloaded
    assert 0 < got < len(body)
    assert item.part_path.exists()
    files["/r.bin"] = (body, {"delay": 0})
    manager.start(item.id)
    assert wait_for(lambda: item.status == Status.COMPLETE, 30), item.error
    assert hashlib.sha256(item.path.read_bytes()).digest() == hashlib.sha256(body).digest()


def test_resume_after_restart_of_app(server, tmp_path):
    from opendownloader.config import Settings
    from opendownloader.core.manager import DownloadManager
    from opendownloader.core.store import Store

    base, files = server
    body = _data(3 * 1024 * 1024)
    files["/x.bin"] = (body, {"delay": 0.05})
    st_conn = 2
    st = Settings(tmp_path / "s.json")
    st["download_dir"] = str(tmp_path / "dl")
    st["min_split_size_kb"] = 256
    st["max_connections"] = st_conn
    m1 = DownloadManager(st, Store(tmp_path / "db"))
    item = m1.add(base + "/x.bin")
    assert wait_for(lambda: item.downloaded > 200 * 1024, 20)
    m1.shutdown()
    files["/x.bin"] = (body, {})
    m2 = DownloadManager(st, Store(tmp_path / "db"))
    it2 = m2.items[item.id]
    assert it2.status == Status.PAUSED and it2.segments
    m2.start(it2.id)
    assert wait_for(lambda: it2.status == Status.COMPLETE, 30), it2.error
    assert sha(it2.path.read_bytes()) == sha(body)
    m2.shutdown()


def test_no_range_server_and_disposition(server, manager):
    base, files = server
    body = _data(700 * 1024)
    files["/dl"] = (body, {"ranges": False, "disposition": "attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf"})
    item = manager.add(base + "/dl")
    assert wait_for(lambda: item.status in (Status.COMPLETE, Status.ERROR), 20)
    assert item.status == Status.COMPLETE, item.error
    assert item.filename == "résumé.pdf"
    assert not item.resumable
    assert sha(item.path.read_bytes()) == sha(body)


def test_unknown_length(server, manager):
    base, files = server
    body = _data(300 * 1024)
    files["/stream"] = (body, {"ranges": False, "no_length": True})
    item = manager.add(base + "/stream", filename="s.bin")
    assert wait_for(lambda: item.status in (Status.COMPLETE, Status.ERROR), 20)
    assert item.status == Status.COMPLETE, item.error
    assert sha(item.path.read_bytes()) == sha(body)


def test_404_is_error(server, manager):
    base, _ = server
    item = manager.add(base + "/missing.zip")
    assert wait_for(lambda: item.status == Status.ERROR, 20)
    assert "404" in item.error


def test_speed_limit(server, manager):
    base, files = server
    body = _data(600 * 1024)
    files["/slow.bin"] = (body, {})
    manager.settings["speed_limit_enabled"] = True
    manager.settings["speed_limit_kbps"] = 300
    manager.apply_speed_limit()
    t0 = time.time()
    item = manager.add(base + "/slow.bin")
    assert wait_for(lambda: item.status == Status.COMPLETE, 30)
    assert time.time() - t0 > 1.2  # 600KB at 300KB/s ~ 2s (minus initial burst)


def test_queue_runs_sequentially(server, manager):
    base, files = server
    for i in range(4):
        files[f"/q{i}.bin"] = (_data(200 * 1024), {"delay": 0.005})
    q = manager.main_queue
    q.max_concurrent = 1
    items = [manager.add(f"{base}/q{i}.bin", start=False, queue_id=q.id) for i in range(4)]
    manager.start_queue(q.id)
    max_active = 0

    def check():
        nonlocal max_active
        max_active = max(max_active, len(manager.workers))
        return all(i.status == Status.COMPLETE for i in items)

    assert wait_for(check, 40)
    assert max_active == 1
    assert wait_for(lambda: not q.running, 5)
    ev = []
    while not manager.events.empty():
        ev.append(manager.events.get()[0])
    assert "queue_done" in ev


def test_refresh_address(server, manager):
    base, files = server
    body = _data(2 * 1024 * 1024)
    files["/old.bin"] = (body, {"delay": 0.05})
    manager.settings["max_connections"] = 2
    item = manager.add(base + "/old.bin")
    assert wait_for(lambda: item.downloaded > 100 * 1024, 20)
    manager.request_refresh(item.id)
    assert wait_for(lambda: item.status == Status.PAUSED, 10)
    files["/new-link.bin"] = (body, {})
    got = manager.try_refresh(base + "/new-link.bin", len(body), "new-link.bin")
    assert got is item
    assert wait_for(lambda: item.status == Status.COMPLETE, 30), item.error
    assert sha(item.path.read_bytes()) == sha(body)


def test_scheduler_starts_queue(server, manager):
    import time as _t

    base, files = server
    files["/s.bin"] = (_data(100 * 1024), {})
    q = manager.main_queue
    item = manager.add(base + "/s.bin", start=False, queue_id=q.id)
    q.schedule.enabled = True
    q.schedule.start_time = _t.strftime("%H:%M")
    q.schedule.days = list(range(7))
    manager._last_sched_minute = ""
    assert wait_for(lambda: item.status == Status.COMPLETE, 20), item.error
    assert q.schedule.last_started == _t.strftime("%Y-%m-%d")


def test_restart_and_remove(server, manager):
    base, files = server
    body = _data(500 * 1024)
    files["/rr.bin"] = (body, {})
    item = manager.add(base + "/rr.bin")
    assert wait_for(lambda: item.status == Status.COMPLETE, 20)
    item.status = Status.PAUSED  # pretend unfinished
    manager.restart(item.id)
    assert wait_for(lambda: item.status == Status.COMPLETE, 20), item.error
    path = item.path
    manager.remove([item.id], delete_files=True)
    assert item.id not in manager.items and not path.exists()
