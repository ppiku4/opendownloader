"""SQLite persistence for downloads and queues."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .. import paths
from .models import DownloadItem, DownloadQueue


class Store:
    def __init__(self, file: Path | None = None):
        self.file = file or paths.db_file()
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(self.file), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS queues (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL)")
        self.db.commit()

    # items ---------------------------------------------------------------
    def load_items(self) -> dict[int, DownloadItem]:
        out: dict[int, DownloadItem] = {}
        with self._lock:
            rows = self.db.execute("SELECT id, data FROM items ORDER BY id").fetchall()
        for rid, data in rows:
            try:
                item = DownloadItem.from_dict(json.loads(data))
            except (ValueError, TypeError):
                continue
            item.id = rid
            out[rid] = item
        return out

    def insert_item(self, item: DownloadItem) -> int:
        with self._lock:
            cur = self.db.execute("INSERT INTO items (data) VALUES (?)", (json.dumps(item.to_dict()),))
            self.db.commit()
            item.id = cur.lastrowid
        self.save_item(item)
        return item.id  # type: ignore[return-value]

    def save_item(self, item: DownloadItem) -> None:
        data = json.dumps(item.to_dict())
        with self._lock:
            self.db.execute("UPDATE items SET data=? WHERE id=?", (data, item.id))
            self.db.commit()

    def delete_item(self, item_id: int) -> None:
        with self._lock:
            self.db.execute("DELETE FROM items WHERE id=?", (item_id,))
            self.db.commit()

    # queues --------------------------------------------------------------
    def load_queues(self) -> dict[int, DownloadQueue]:
        out: dict[int, DownloadQueue] = {}
        with self._lock:
            rows = self.db.execute("SELECT id, data FROM queues ORDER BY id").fetchall()
        for rid, data in rows:
            q = DownloadQueue.from_dict(json.loads(data))
            q.id = rid
            out[rid] = q
        return out

    def save_queue(self, q: DownloadQueue) -> None:
        with self._lock:
            if q.id is None:
                cur = self.db.execute("INSERT INTO queues (data) VALUES ('{}')")
                q.id = cur.lastrowid
            self.db.execute("UPDATE queues SET data=? WHERE id=?", (json.dumps(q.to_dict()), q.id))
            self.db.commit()

    def delete_queue(self, qid: int) -> None:
        with self._lock:
            self.db.execute("DELETE FROM queues WHERE id=?", (qid,))
            self.db.commit()

    def close(self) -> None:
        with self._lock:
            self.db.close()
