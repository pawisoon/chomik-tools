"""Persistent download queue for the SABnzbd-compatible API. Survives restarts; downloads resume from .part files."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    nzo_id       TEXT PRIMARY KEY,
    name         TEXT NOT NULL,      -- release title shown to the arr
    category     TEXT NOT NULL,
    hit          TEXT NOT NULL,      -- JSON: id, name, owner, folder_path
    status       TEXT NOT NULL,      -- Queued | Downloading | Completed | Failed
    bytes_total  INTEGER NOT NULL DEFAULT 0,
    bytes_done   INTEGER NOT NULL DEFAULT 0,
    storage      TEXT,
    fail_message TEXT NOT NULL DEFAULT '',
    added        REAL NOT NULL,
    started      REAL,
    finished     REAL,
    hidden       INTEGER NOT NULL DEFAULT 0   -- removed from history by the arr
);
"""

ACTIVE = ("Queued", "Downloading")


@dataclass
class Item:
    nzo_id: str
    name: str
    category: str
    hit: dict
    status: str
    bytes_total: int
    bytes_done: int
    storage: str | None
    fail_message: str
    added: float
    started: float | None
    finished: float | None


class Queue:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.wake = threading.Event()
        with self.lock:  # a restart interrupted these; the worker resumes them
            self.db.execute("UPDATE items SET status='Queued' WHERE status='Downloading'")
            self.db.commit()

    def _rows(self, where: str, args=()) -> list[Item]:
        with self.lock:
            rows = self.db.execute(
                "SELECT nzo_id, name, category, hit, status, bytes_total, bytes_done, storage, fail_message, added, "
                f"started, finished FROM items WHERE {where}", args).fetchall()
        return [Item(r[0], r[1], r[2], json.loads(r[3]), *r[4:]) for r in rows]

    def add(self, name: str, category: str, hit: dict) -> str:
        with self.lock:
            dup = self._rows("hit LIKE ? AND status IN ('Queued','Downloading') AND hidden=0",
                             (f'%"id": "{hit["id"]}"%',))
            if dup:
                return dup[0].nzo_id
            nzo_id = "SABnzbd_nzo_" + uuid.uuid4().hex[:12]
            self.db.execute("INSERT INTO items (nzo_id, name, category, hit, status, bytes_total, added) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (nzo_id, name, category, json.dumps(hit, sort_keys=True), "Queued",
                             int(hit.get("size_bytes") or 0), time.time()))
            self.db.commit()
        self.wake.set()
        return nzo_id

    def get(self, nzo_id: str) -> Item | None:
        rows = self._rows("nzo_id=?", (nzo_id,))
        return rows[0] if rows else None

    def active(self) -> list[Item]:
        return self._rows("status IN ('Queued','Downloading') AND hidden=0 ORDER BY added")

    def history(self) -> list[Item]:
        return self._rows("status IN ('Completed','Failed') AND hidden=0 ORDER BY finished DESC")

    def next_queued(self) -> Item | None:
        rows = self._rows("status='Queued' AND hidden=0 ORDER BY added LIMIT 1")
        return rows[0] if rows else None

    def update(self, nzo_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE items SET {cols} WHERE nzo_id=?", (*fields.values(), nzo_id))
            self.db.commit()

    def hide(self, nzo_id: str) -> Item | None:
        """Remove from queue/history as the arr sees it. A queued item is also cancelled."""
        item = self.get(nzo_id)
        if item:
            status = "Failed" if item.status in ACTIVE else item.status
            self.update(nzo_id, hidden=1, status=status,
                        fail_message=item.fail_message or ("Removed" if item.status in ACTIVE else ""))
        return item

    def bytes_this_month(self) -> int:
        start = time.mktime(time.strptime(time.strftime("%Y-%m-01"), "%Y-%m-%d"))
        with self.lock:
            row = self.db.execute("SELECT COALESCE(SUM(bytes_done),0) FROM items WHERE started>=?", (start,)).fetchone()
        return int(row[0])
