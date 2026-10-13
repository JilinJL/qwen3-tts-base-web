"""Durable job metadata only. Audio is deliberately never stored here."""

import hashlib
import json
import sqlite3
from pathlib import Path


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS jobs "
            "(id TEXT PRIMARY KEY, digest TEXT, payload TEXT, status TEXT, error TEXT)"
        )
        self.db.execute(
            "UPDATE jobs SET status='failed', error='worker_restarted_result_lost' "
            "WHERE status IN ('queued','running','succeeded')"
        )
        self.db.commit()

    @staticmethod
    def digest(payload: dict) -> str:
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def get(self, job_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT digest,payload,status,error FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        return dict(digest=row[0], payload=json.loads(row[1]), status=row[2], error=row[3])

    def insert(self, payload: dict):
        self.db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?)",
            (payload["job_id"], self.digest(payload), json.dumps(payload), "queued", ""),
        )
        self.db.commit()

    def update(self, job_id: str, status: str, error: str = ""):
        self.db.execute("UPDATE jobs SET status=?,error=? WHERE id=?", (status, error, job_id))
        self.db.commit()

    def close(self):
        self.db.close()
