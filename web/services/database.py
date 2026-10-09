from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "web" / "data" / "experiments.db"


class Database:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self._init()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    band TEXT NOT NULL,
                    condition TEXT,
                    model_mode TEXT,
                    devices TEXT NOT NULL,
                    state TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    return_code INTEGER,
                    summary_json TEXT,
                    controller_log TEXT,
                    receiver_log TEXT
                )
                """
            )

    def upsert_job(self, job: dict[str, Any]) -> None:
        with self.lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, kind, band, condition, model_mode, devices, state,
                    started_at, ended_at, return_code, summary_json,
                    controller_log, receiver_log
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    state=excluded.state,
                    ended_at=excluded.ended_at,
                    return_code=excluded.return_code,
                    summary_json=excluded.summary_json,
                    controller_log=excluded.controller_log,
                    receiver_log=excluded.receiver_log
                """,
                (
                    job["id"],
                    job["kind"],
                    job["band"],
                    job.get("condition"),
                    job.get("model_mode"),
                    json.dumps(job.get("devices", [])),
                    job["state"],
                    job["started_at"],
                    job.get("ended_at"),
                    job.get("return_code"),
                    json.dumps(job.get("summary", {}), ensure_ascii=False),
                    job.get("controller_log"),
                    job.get("receiver_log"),
                ),
            )

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["devices"] = json.loads(item["devices"] or "[]")
        item["summary"] = json.loads(item.pop("summary_json") or "{}")
        return item
