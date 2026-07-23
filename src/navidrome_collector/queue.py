"""SQLite-backed queue for track download requests."""

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class QueueItem:
    id: int
    query: str
    artist: Optional[str] = None
    title: Optional[str] = None
    status: str = "pending"  # pending | in_progress | processing | done | failed
    file_path: Optional[str] = None
    error: Optional[str] = None
    created_at: str = field(default_factory=lambda: _now())
    updated_at: str = field(default_factory=lambda: _now())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    query       TEXT NOT NULL,
    artist      TEXT,
    title       TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    file_path   TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status);
"""


class Queue:
    """Thread-safe SQLite queue."""

    def __init__(self, db_path: str | Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self._path))
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.executescript(SCHEMA)
        return self._local.conn

    def add(self, query: str, artist: str | None = None, title: str | None = None) -> int:
        """Add a track. Returns the new item id."""
        now = _now()
        cur = self._conn().execute(
            "INSERT INTO queue (query, artist, title, status, created_at, updated_at) VALUES (?, ?, ?, 'pending', ?, ?)",
            (query, artist, title, now, now),
        )
        self._conn().commit()
        return cur.lastrowid

    def next_pending(self) -> Optional[QueueItem]:
        """Claim the next pending item (atomic)."""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM queue WHERE status = 'pending' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        now = _now()
        conn.execute(
            "UPDATE queue SET status = 'in_progress', updated_at = ? WHERE id = ?",
            (now, row["id"]),
        )
        conn.commit()
        data = dict(row)
        data["status"] = "in_progress"
        data["updated_at"] = now
        return QueueItem(**data)

    def mark_processing(self, item_id: int, pending_meta: list) -> None:
        """Mark item as processing with enqueued download metadata."""
        now = _now()
        self._conn().execute(
            "UPDATE queue SET status = 'processing', error = ?, updated_at = ? WHERE id = ?",
            (json.dumps({"pending": pending_meta}), now, item_id),
        )
        self._conn().commit()

    def mark_done(self, item_id: int, file_path: str) -> None:
        """Mark item as completed."""
        now = _now()
        self._conn().execute(
            "UPDATE queue SET status = 'done', file_path = ?, updated_at = ? WHERE id = ?",
            (file_path, now, item_id),
        )
        self._conn().commit()

    def mark_failed(self, item_id: int, error: str) -> None:
        """Mark item as failed."""
        now = _now()
        self._conn().execute(
            "UPDATE queue SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
            (error, now, item_id),
        )
        self._conn().commit()

    def list_items(self, status: str | None = None) -> list[QueueItem]:
        if status:
            rows = self._conn().execute(
                "SELECT * FROM queue WHERE status = ? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM queue ORDER BY id DESC"
            ).fetchall()
        return [QueueItem(**dict(r)) for r in rows]

    def get(self, item_id: int) -> Optional[QueueItem]:
        row = self._conn().execute(
            "SELECT * FROM queue WHERE id = ?", (item_id,)
        ).fetchone()
        return QueueItem(**dict(row)) if row else None

    def stats(self) -> dict[str, int]:
        rows = self._conn().execute(
            "SELECT status, COUNT(*) as cnt FROM queue GROUP BY status"
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    def clear(self, status: str | None = None) -> int:
        if status:
            cur = self._conn().execute("DELETE FROM queue WHERE status = ?", (status,))
        else:
            cur = self._conn().execute("DELETE FROM queue")
        self._conn().commit()
        return cur.rowcount

    def reset_to_pending(self, item_id: int) -> None:
        """Reset a processing/failed item back to pending for retry."""
        now = _now()
        self._conn().execute(
            "UPDATE queue SET status = 'pending', error = NULL, updated_at = ? WHERE id = ?",
            (now, item_id),
        )
        self._conn().commit()
