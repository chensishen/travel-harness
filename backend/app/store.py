import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC
from pathlib import Path

from backend.harness.schemas import now


def save_json(path: Path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temporary.replace(path)


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "harness.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, data TEXT
                );
                CREATE INDEX IF NOT EXISTS events_job ON events(job_id, seq);
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY, data TEXT, created_at TEXT
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, job: dict):
        job["updated_at"] = now()
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO jobs VALUES (?, ?)",
                (job["id"], json.dumps(job, ensure_ascii=False)),
            )

    def get(self, job_id: str):
        with self.connect() as db:
            row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def jobs(self):
        with self.connect() as db:
            rows = db.execute("SELECT data FROM jobs ORDER BY rowid DESC LIMIT 100").fetchall()
        return [json.loads(row[0]) for row in rows]

    def event(self, job_id: str, **event):
        event["at"] = now()
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO events(job_id,data) VALUES (?,?)",
                (job_id, json.dumps(event, ensure_ascii=False)),
            )
        return cursor.lastrowid

    def events(self, job_id: str, after: int = 0):
        with self.connect() as db:
            rows = db.execute(
                "SELECT seq,data FROM events WHERE job_id=? AND seq>? ORDER BY seq", (job_id, after)
            ).fetchall()
        return [{"seq": seq, **json.loads(data)} for seq, data in rows]

    def timing_samples(self):
        with self.connect() as db:
            rows = db.execute("SELECT job_id,data FROM events WHERE json_extract(data,'$.status')='completed' AND json_extract(data,'$.duration_s')>0 ORDER BY seq DESC LIMIT 300").fetchall()
        samples, seen = {}, set()
        for job_id, data in rows:
            event = json.loads(data)
            pair = (job_id, event['node'])
            if pair in seen:
                continue
            seen.add(pair)
            samples.setdefault(event['node'], []).append(event['duration_s'])
        return samples

    def cached(self, key: str, max_age_days: int = 90):
        from datetime import datetime, timedelta

        with self.connect() as db:
            row = db.execute("SELECT data,created_at FROM cache WHERE key=?", (key,)).fetchone()
        if row and datetime.fromisoformat(row[1]) > datetime.now(UTC) - timedelta(
            days=max_age_days
        ):
            return json.loads(row[0])
        return None

    def put_cache(self, key: str, value):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO cache VALUES (?,?,?)",
                (key, json.dumps(value, ensure_ascii=False), now()),
            )

    def cache_created_at(self, key: str):
        with self.connect() as db:
            row=db.execute('SELECT created_at FROM cache WHERE key=?',(key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def key(prefix: str, value: str):
        return prefix + ":" + hashlib.sha256(value.encode()).hexdigest()
