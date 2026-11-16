"""SQLite job state: which source files already landed in the destination."""
from __future__ import annotations

import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS copies (
    src_owner   TEXT NOT NULL,
    src_id      TEXT NOT NULL,
    dst_account TEXT NOT NULL,
    dest_path   TEXT NOT NULL,
    name        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    status      TEXT NOT NULL,          -- done | failed | skipped_exists
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (src_owner, src_id, dst_account)
);
"""


class State:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def status(self, src_owner: str, src_id: str, dst: str) -> str | None:
        row = self.db.execute(
            "SELECT status FROM copies WHERE src_owner=? AND src_id=? AND dst_account=?",
            (src_owner.lower(), src_id, dst.lower()),
        ).fetchone()
        return row[0] if row else None

    def record(self, src_owner, src_id, dst, dest_path, name, size, status, error=None) -> None:
        self.db.execute(
            """INSERT INTO copies (src_owner, src_id, dst_account, dest_path, name, size, status, attempts, error, updated_at)
               VALUES (?,?,?,?,?,?,?,1,?,?)
               ON CONFLICT (src_owner, src_id, dst_account) DO UPDATE SET
                 status=excluded.status, error=excluded.error, dest_path=excluded.dest_path,
                 attempts=copies.attempts+1, updated_at=excluded.updated_at""",
            (src_owner.lower(), src_id, dst.lower(), dest_path, name, size, status, error, time.time()),
        )
        self.db.commit()

    def summary(self, dst: str) -> dict[str, int]:
        return dict(
            self.db.execute(
                "SELECT status, COUNT(*) FROM copies WHERE dst_account=? GROUP BY status", (dst.lower(),)
            ).fetchall()
        )
