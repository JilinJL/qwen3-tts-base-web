"""Local audio catalog. SQLite transactions protect edits across request threads."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import numpy as np
import soundfile as sf
from filelock import FileLock


class AudioLibrary:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(self.root / ".library.lock"), timeout=30)
        with self.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS clips (
                id TEXT PRIMARY KEY, filename TEXT UNIQUE, metadata TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.root / "library.sqlite3", timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _record(self, row):
        if row is None:
            raise KeyError("音频记录不存在")
        record = json.loads(row["metadata"])
        record.update(
            {key: row[key] for key in ("id", "filename", "created_at", "updated_at")}
        )
        record["url"] = (
            f"/static/audio/{quote(row['filename'], safe='')}"
            if row["filename"]
            else None
        )
        record["available"] = bool(
            row["filename"] and self.audio_path(row["filename"]).is_file()
        )
        return record

    def audio_path(self, filename):
        path = (self.root / filename).resolve()
        if path.parent != self.root.resolve() or path.suffix.lower() != ".wav":
            raise ValueError("非法音频路径")
        return path

    def create(self, metadata, filename=None):
        now = datetime.now(timezone.utc).isoformat()
        record_id = uuid.uuid4().hex
        with self.connection() as db:
            db.execute(
                "INSERT INTO clips VALUES (?, ?, ?, ?, ?)",
                (
                    record_id,
                    filename,
                    json.dumps(metadata, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return self.get(record_id)

    def get(self, record_id):
        with self.connection() as db:
            return self._record(
                db.execute("SELECT * FROM clips WHERE id=?", (record_id,)).fetchone()
            )

    def list(self):
        # Import pre-catalog WAVs without moving them or inventing lost text/voice metadata.
        with self.lock:
            with self.connection() as db:
                known = {r[0] for r in db.execute("SELECT filename FROM clips")}
            for path in sorted(self.root.glob("*.wav")):
                if path.name in known or path.is_symlink():
                    continue
                try:
                    info = sf.info(str(path))
                    self.create(
                        {
                            "title": path.stem,
                            "text": "",
                            "synthesis_mode": "legacy",
                            "duration": info.duration,
                            "sample_rate": info.samplerate,
                        },
                        path.name,
                    )
                except (OSError, RuntimeError):
                    continue
            with self.connection() as db:
                records = [
                    self._record(row)
                    for row in db.execute("SELECT * FROM clips ORDER BY created_at")
                ]
        return sorted(
            records, key=lambda item: (item.get("position", 1e12), item["created_at"])
        )

    def update(self, record_id, changes):
        with self.lock, self.connection() as db:
            row = db.execute("SELECT * FROM clips WHERE id=?", (record_id,)).fetchone()
            self._record(row)
            metadata = json.loads(row["metadata"])
            metadata.update(changes)
            db.execute(
                "UPDATE clips SET metadata=?, updated_at=? WHERE id=?",
                (
                    json.dumps(metadata, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                    record_id,
                ),
            )
        return self.get(record_id)

    def save_audio(self, audio, sample_rate, metadata, record_id=None):
        audio = np.asarray(audio)
        if not audio.size or not np.isfinite(audio).all() or sample_rate <= 0:
            raise ValueError("模型返回的音频为空或包含无效数值")
        filename = f"{uuid.uuid4().hex}.wav"
        path = self.audio_path(filename)
        temporary = path.with_suffix(".wav.tmp")
        old_filename = None
        # The file becomes visible only together with its catalog entry.
        with self.lock:
            try:
                sf.write(str(temporary), audio, sample_rate, format="WAV")
                temporary.replace(path)
                info = sf.info(str(path))
                metadata = {
                    **metadata,
                    "duration": info.duration,
                    "sample_rate": sample_rate,
                }
                if record_id:
                    with self.connection() as db:
                        row = db.execute(
                            "SELECT * FROM clips WHERE id=?", (record_id,)
                        ).fetchone()
                        self._record(row)
                        old_filename = row["filename"]
                        previous = json.loads(row["metadata"])
                        previous.update(metadata)
                        db.execute(
                            "UPDATE clips SET filename=?, metadata=?, updated_at=? WHERE id=?",
                            (
                                filename,
                                json.dumps(previous, ensure_ascii=False),
                                datetime.now(timezone.utc).isoformat(),
                                record_id,
                            ),
                        )
                else:
                    record_id = self.create(metadata, filename)["id"]
            except BaseException:
                temporary.unlink(missing_ok=True)
                path.unlink(missing_ok=True)
                raise
            if old_filename:
                self.audio_path(old_filename).unlink(missing_ok=True)
        return self.get(record_id)

    def delete(self, record_id):
        with self.lock, self.connection() as db:
            row = db.execute("SELECT * FROM clips WHERE id=?", (record_id,)).fetchone()
            self._record(row)
            if row["filename"]:
                self.audio_path(row["filename"]).unlink(missing_ok=True)
            db.execute("DELETE FROM clips WHERE id=?", (record_id,))
