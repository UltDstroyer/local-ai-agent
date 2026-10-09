"""Transactional SQLite queue with single-worker leasing and audit history."""

from __future__ import annotations
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, project TEXT NOT NULL, prompt TEXT NOT NULL,
    status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
    result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, at REAL NOT NULL,
    event TEXT NOT NULL, details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    project TEXT NOT NULL, key TEXT NOT NULL, text TEXT NOT NULL,
    PRIMARY KEY(project, key)
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        with self.lock, self.db:
            self.db.executescript(SCHEMA)
            # Interrupted jobs are never blindly retried after restart.
            self.db.execute(
                "UPDATE jobs SET status='interrupted', updated=? WHERE status='running'",
                (time.time(),),
            )

    def event(self, job, kind, details=""):
        with self.lock, self.db:
            self.db.execute("INSERT INTO events(job,at,event,details) VALUES (?,?,?,?)",
                            (job, time.time(), kind, str(details)[:2000]))

    def submit(self, project, prompt):
        jid = uuid.uuid4().hex[:12]
        now = time.time()
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO jobs(id,project,prompt,status,created,updated) VALUES (?,?,?,?,?,?)",
                (jid, project, prompt, "queued", now, now),
            )
        self.event(jid, "submitted", project)
        return jid

    def get(self, jid):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            return dict(row) if row else None

    def list(self, limit=30):
        with self.lock:
            return [dict(row) for row in self.db.execute(
                "SELECT id,project,status,created,updated,result,error FROM jobs "
                "ORDER BY created DESC LIMIT ?", (limit,),
            )]

    def next(self):
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT id FROM jobs WHERE status='queued' ORDER BY created LIMIT 1"
            ).fetchone()
            if not row:
                return None
            jid = row["id"]
            self.db.execute(
                "UPDATE jobs SET status='running',updated=? WHERE id=? AND status='queued'",
                (time.time(), jid),
            )
            return self.get(jid)

    def finish(self, jid, status, result="", error=""):
        if status not in ("completed", "failed", "cancelled", "interrupted"):
            raise ValueError(status)
        with self.lock, self.db:
            self.db.execute(
                "UPDATE jobs SET status=?,result=?,error=?,updated=? "
                "WHERE id=? AND status IN ('running','queued')",
                (status, result[:30000], error[:4000], time.time(), jid),
            )
        self.event(jid, status, error or result[:200])

    def cancel(self, jid):
        with self.lock, self.db:
            cur = self.db.execute(
                "UPDATE jobs SET status='cancelled',updated=? "
                "WHERE id=? AND status IN ('queued','running')",
                (time.time(), jid),
            )
            ok = cur.rowcount > 0
        if ok:
            self.event(jid, "cancelled_by_owner")
        return ok

    def cancelled(self, jid):
        job = self.get(jid)
        return job is None or job["status"] == "cancelled"

    def events(self, jid):
        with self.lock:
            return [dict(x) for x in self.db.execute(
                "SELECT at,event,details FROM events WHERE job=? ORDER BY seq LIMIT 300",
                (jid,),
            )]

    def note(self, project, key, value):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO notes(project,key,text) VALUES(?,?,?) "
                "ON CONFLICT(project,key) DO UPDATE SET text=excluded.text",
                (project, key, value),
            )

    def notes(self, project):
        with self.lock:
            return {x["key"]: x["text"] for x in self.db.execute(
                "SELECT key,text FROM notes WHERE project=?", (project,),
            )}
