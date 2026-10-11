"""Persistent state for SSH targets and autonomous remote plans."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    name TEXT PRIMARY KEY,
    target_json TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS remote_plans (
    id TEXT PRIMARY KEY,
    target TEXT NOT NULL,
    goal TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    status TEXT NOT NULL,
    current_milestone INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    codex_thread TEXT NOT NULL DEFAULT '',
    context_json TEXT NOT NULL DEFAULT '{}',
    mutations_json TEXT NOT NULL DEFAULT '[]',
    created REAL NOT NULL,
    updated REAL NOT NULL,
    result TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS remote_deviations (
    id TEXT PRIMARY KEY,
    plan TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    request_json TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS remote_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    plan TEXT,
    at REAL NOT NULL,
    event TEXT NOT NULL,
    details TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS remote_plans_target_created
    ON remote_plans(target, created DESC);
CREATE INDEX IF NOT EXISTS remote_deviations_plan_created
    ON remote_deviations(plan, created DESC);
"""


class RemoteStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        with self.lock, self.db:
            self.db.executescript(SCHEMA)
            interrupted = [
                row["id"] for row in self.db.execute(
                    "SELECT id FROM remote_plans WHERE status='running'"
                ).fetchall()
            ]
            now = time.time()
            self.db.execute(
                "UPDATE remote_plans SET status='approved',updated=? "
                "WHERE status='running'",
                (now,),
            )
            for pid in interrupted:
                row = self.db.execute(
                    "SELECT context_json FROM remote_plans WHERE id=?", (pid,)
                ).fetchone()
                context = json.loads(row["context_json"]) if row else {}
                context["next_guidance"] = (
                    "The coordinator restarted during this milestone. Re-inspect "
                    "the target before retrying and do not assume the previous "
                    "remote action completed."
                )
                self.db.execute(
                    "UPDATE remote_plans SET context_json=?,updated=? WHERE id=?",
                    (json.dumps(context), now, pid),
                )
                self.db.execute(
                    "INSERT INTO remote_events(plan,at,event,details) VALUES (?,?,?,?)",
                    (
                        pid, now, "remote_plan_recovered_after_restart",
                        "Interrupted running lease was safely re-queued for fresh inspection.",
                    ),
                )

    def event(self, plan, kind, details=""):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO remote_events(plan,at,event,details) VALUES (?,?,?,?)",
                (plan, time.time(), kind, str(details)[:6000]),
            )

    # ---------------- targets ----------------

    def put_target(self, target: dict):
        now = time.time()
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO targets(name,target_json,created,updated) VALUES (?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "target_json=excluded.target_json,updated=excluded.updated",
                (target["name"], json.dumps(target), now, now),
            )

    def get_target(self, name: str):
        with self.lock:
            row = self.db.execute(
                "SELECT target_json FROM targets WHERE name=?", (name,)
            ).fetchone()
        return json.loads(row["target_json"]) if row else None

    def list_targets(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT target_json FROM targets ORDER BY name"
            ).fetchall()
        return [json.loads(row["target_json"]) for row in rows]

    def delete_target(self, name: str) -> bool:
        with self.lock, self.db:
            active = self.db.execute(
                "SELECT 1 FROM remote_plans WHERE target=? AND "
                "status IN ('awaiting_approval','approved','running',"
                "'awaiting_deviation','ready_for_review') LIMIT 1",
                (name,),
            ).fetchone()
            if active:
                return False
            cur = self.db.execute("DELETE FROM targets WHERE name=?", (name,))
            return cur.rowcount == 1

    # ---------------- remote plans ----------------

    @staticmethod
    def _plan_row(row):
        if not row:
            return None
        value = dict(row)
        value["plan"] = json.loads(value.pop("plan_json"))
        value["context"] = json.loads(value.pop("context_json"))
        value["mutations"] = json.loads(value.pop("mutations_json"))
        return value

    def create_plan(self, target: str, goal: str, plan: dict) -> str:
        pid = plan["id"]
        now = time.time()
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO remote_plans("
                "id,target,goal,plan_json,status,created,updated"
                ") VALUES (?,?,?,?,?,?,?)",
                (
                    pid, target, goal, json.dumps(plan),
                    "awaiting_approval", now, now,
                ),
            )
        self.event(pid, "remote_plan_created", target)
        return pid

    def get_plan(self, pid: str):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM remote_plans WHERE id=?", (pid,)
            ).fetchone()
        return self._plan_row(row)

    def latest_plan(self, target: str):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM remote_plans WHERE target=? "
                "ORDER BY created DESC LIMIT 1",
                (target,),
            ).fetchone()
        return self._plan_row(row)

    def list_plans(self, target: str | None = None, limit: int = 30):
        with self.lock:
            if target:
                rows = self.db.execute(
                    "SELECT * FROM remote_plans WHERE target=? "
                    "ORDER BY created DESC LIMIT ?",
                    (target, limit),
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT * FROM remote_plans ORDER BY created DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._plan_row(row) for row in rows]

    def update_plan(self, pid: str, **fields):
        allowed = {
            "status", "current_milestone", "attempts", "codex_thread",
            "context", "mutations", "plan", "result", "error",
        }
        if not fields or any(key not in allowed for key in fields):
            raise ValueError("invalid remote plan update")
        encoded = {}
        for key, value in fields.items():
            if key == "context":
                encoded["context_json"] = json.dumps(value)
            elif key == "mutations":
                encoded["mutations_json"] = json.dumps(value)
            elif key == "plan":
                encoded["plan_json"] = json.dumps(value)
            else:
                encoded[key] = value
        encoded["updated"] = time.time()
        clause = ", ".join(f"{key}=?" for key in encoded)
        values = list(encoded.values()) + [pid]
        with self.lock, self.db:
            cur = self.db.execute(
                f"UPDATE remote_plans SET {clause} WHERE id=?", values
            )
            if cur.rowcount != 1:
                raise ValueError("no such remote plan")
            if fields.get("status") in ("completed", "cancelled", "rejected"):
                self.db.execute(
                    "UPDATE remote_deviations SET status='superseded',updated=? "
                    "WHERE plan=? AND status='pending'",
                    (time.time(), pid),
                )

    def resume_from_deviation(self, pid: str, **fields) -> bool:
        """Apply a prepared deviation result only while the plan still waits."""
        allowed = ["status","current_milestone","attempts","codex_thread","context","mutations","plan","result","error"]
        if not fields or any(key not in allowed for key in fields):
            raise ValueError("invalid remote plan deviation resume")
        if fields.get("status", "approved") != "approved":
            raise ValueError("deviation resume must return the plan to approved")
        fields = dict(fields)
        fields["status"] = "approved"
        encoded = {}
        if "context" in fields:
            encoded["context_json"] = json.dumps(fields.pop("context"))
        if "mutations" in fields:
            encoded["mutations_json"] = json.dumps(fields.pop("mutations"))
        if "plan" in fields:
            encoded["plan_json"] = json.dumps(fields.pop("plan"))
        encoded.update(fields)
        encoded["updated"] = time.time()
        clause = ", ".join(f"{key}=?" for key in encoded)
        values = list(encoded.values()) + [pid]
        with self.lock, self.db:
            cur = self.db.execute(
                f"UPDATE remote_plans SET {clause} "
                "WHERE id=? AND status='awaiting_deviation'",
                values,
            )
            return cur.rowcount == 1

    def approve_plan(self, pid: str) -> bool:
        with self.lock, self.db:
            cur = self.db.execute(
                "UPDATE remote_plans SET status='approved',updated=? "
                "WHERE id=? AND status='awaiting_approval'",
                (time.time(), pid),
            )
            ok = cur.rowcount == 1
        if ok:
            self.event(pid, "remote_plan_approved_by_owner")
        return ok

    def reject_plan(self, pid: str) -> bool:
        with self.lock, self.db:
            cur = self.db.execute(
                "UPDATE remote_plans SET status='rejected',updated=? "
                "WHERE id=? AND status='awaiting_approval'",
                (time.time(), pid),
            )
            ok = cur.rowcount == 1
        if ok:
            self.event(pid, "remote_plan_rejected_by_owner")
        return ok

    def cancel_plan(self, pid: str) -> bool:
        with self.lock, self.db:
            cur = self.db.execute(
                "UPDATE remote_plans SET status='cancelled',updated=? "
                "WHERE id=? AND status IN ("
                "'approved','running','awaiting_deviation','ready_for_review')",
                (time.time(), pid),
            )
            ok = cur.rowcount == 1
            if ok:
                self.db.execute(
                    "UPDATE remote_deviations SET status='superseded',updated=? "
                    "WHERE plan=? AND status='pending'",
                    (time.time(), pid),
                )
        if ok:
            self.event(pid, "remote_plan_cancelled_by_owner")
        return ok

    def next_plan(self):
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT id FROM remote_plans WHERE status='approved' "
                "ORDER BY created LIMIT 1"
            ).fetchone()
            if not row:
                return None
            pid = row["id"]
            cur = self.db.execute(
                "UPDATE remote_plans SET status='running',updated=? "
                "WHERE id=? AND status='approved'",
                (time.time(), pid),
            )
            if cur.rowcount != 1:
                return None
        return self.get_plan(pid)

    # ---------------- deviations ----------------

    def create_deviation(
        self, plan: str, target: str, reason: str, request: dict
    ) -> str:
        did = uuid.uuid4().hex[:12]
        now = time.time()
        with self.lock, self.db:
            existing = self.db.execute(
                "SELECT id FROM remote_deviations WHERE plan=? AND status='pending' "
                "ORDER BY created DESC LIMIT 1",
                (plan,),
            ).fetchone()
            if existing:
                return existing["id"]
            self.db.execute(
                "INSERT INTO remote_deviations("
                "id,plan,target,status,reason,request_json,created,updated"
                ") VALUES (?,?,?,?,?,?,?,?)",
                (
                    did, plan, target, "pending", reason[:4000],
                    json.dumps(request), now, now,
                ),
            )
            self.db.execute(
                "UPDATE remote_plans SET status='awaiting_deviation',updated=? "
                "WHERE id=?",
                (now, plan),
            )
        self.event(plan, "remote_deviation_requested", did + ": " + reason[:1000])
        return did

    def pending_deviation(self, plan: str):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM remote_deviations "
                "WHERE plan=? AND status='pending' ORDER BY created DESC LIMIT 1",
                (plan,),
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["request"] = json.loads(value.pop("request_json"))
        return value

    def get_deviation(self, did: str):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM remote_deviations WHERE id=?", (did,)
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["request"] = json.loads(value.pop("request_json"))
        return value

    def list_deviations(
        self, target: str | None = None, plan: str | None = None
    ):
        with self.lock:
            if plan:
                rows = self.db.execute(
                    "SELECT * FROM remote_deviations WHERE plan=? "
                    "ORDER BY created DESC",
                    (plan,),
                ).fetchall()
            elif target:
                rows = self.db.execute(
                    "SELECT * FROM remote_deviations WHERE target=? "
                    "ORDER BY created DESC",
                    (target,),
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT * FROM remote_deviations "
                    "ORDER BY created DESC LIMIT 50"
                ).fetchall()
        out = []
        for row in rows:
            value = dict(row)
            value["request"] = json.loads(value.pop("request_json"))
            out.append(value)
        return out

    def resolve_deviation(self, did: str, approved: bool) -> bool:
        status = "approved" if approved else "rejected"
        with self.lock, self.db:
            cur = self.db.execute(
                "UPDATE remote_deviations SET status=?,updated=? "
                "WHERE id=? AND status='pending' "
                "AND EXISTS (SELECT 1 FROM remote_plans p "
                "WHERE p.id=remote_deviations.plan AND p.status='awaiting_deviation')",
                (status, time.time(), did),
            )
            return cur.rowcount == 1

    def events(self, pid: str):
        with self.lock:
            return [
                dict(row)
                for row in self.db.execute(
                    "SELECT at,event,details FROM remote_events "
                    "WHERE plan=? ORDER BY seq LIMIT 500",
                    (pid,),
                )
            ]
