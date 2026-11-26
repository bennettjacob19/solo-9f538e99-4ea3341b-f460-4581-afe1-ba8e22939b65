"""SQLite persistence.

Only rows with ``status = 'published'`` define the active configuration;
the active one is the most recent such row.  Failed / infeasible attempts are
kept as an audit trail but never become active, so after any error the
application keeps using the last published whole configuration.

The drill runs single-process (the container uses one gunicorn worker with
threads), so one shared connection guarded by an RLock is sufficient and
avoids connection leaks.  Each method finishes quickly; the slow simulated
device push happens outside the database.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS revisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    version_no    INTEGER,
    status        TEXT NOT NULL,          -- published | no_solution | publish_failed | invalid_rules
    created_at    TEXT NOT NULL,
    simulate_fail INTEGER NOT NULL DEFAULT 0,
    message       TEXT,
    entries       INTEGER,
    cpu_frames    INTEGER,
    hw_limit      INTEGER,
    cpu_limit     INTEGER,
    payload_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revisions_status_id ON revisions(status, id);

CREATE TABLE IF NOT EXISTS replay_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id    INTEGER,
    created_at     TEXT NOT NULL,
    total_frames   INTEGER,
    cpu_frames     INTEGER,
    hw_frames      INTEGER,
    all_correct    INTEGER NOT NULL DEFAULT 1,
    detail_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_replay_runs_rev ON replay_runs(revision_id, id);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Storage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        if db_path != ":memory:":
            parent = os.path.dirname(os.path.abspath(db_path))
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            db_path, timeout=15, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---------------- revisions ----------------

    def insert_revision(self, *, status: str, payload: dict,
                        version_no: int | None = None,
                        message: str | None = None,
                        entries: int | None = None,
                        cpu_frames: int | None = None,
                        hw_limit: int | None = None,
                        cpu_limit: int | None = None,
                        simulate_fail: bool = False) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO revisions
                   (version_no, status, created_at, simulate_fail, message,
                    entries, cpu_frames, hw_limit, cpu_limit, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (version_no, status, utcnow_iso(), 1 if simulate_fail else 0,
                 message, entries, cpu_frames, hw_limit, cpu_limit,
                 json.dumps(payload, ensure_ascii=False)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def next_version_no(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 AS v "
                "FROM revisions WHERE status='published'"
            ).fetchone()
            return int(row["v"])

    def get_revision(self, revision_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM revisions WHERE id = ?", (revision_id,)
            ).fetchone()
        return self._row_to_revision(row) if row else None

    def latest_published(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM revisions WHERE status='published' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._row_to_revision(row) if row else None

    def list_revisions(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM revisions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_revision(r) for r in rows]

    @staticmethod
    def _row_to_revision(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item

    # ---------------- replay runs ----------------

    def insert_replay_run(self, *, revision_id: int | None, detail: dict,
                          total_frames: int, cpu_frames: int,
                          hw_frames: int, all_correct: bool) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO replay_runs
                   (revision_id, created_at, total_frames, cpu_frames,
                    hw_frames, all_correct, detail_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (revision_id, utcnow_iso(), total_frames, cpu_frames,
                 hw_frames, 1 if all_correct else 0,
                 json.dumps(detail, ensure_ascii=False)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_replay_run(self, run_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM replay_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["detail"] = json.loads(item.pop("detail_json"))
        return item

    def list_replay_runs(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, revision_id, created_at, total_frames, "
                "cpu_frames, hw_frames, all_correct "
                "FROM replay_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
