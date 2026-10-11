"""Transactional SQLite state for jobs, plans, deviations, notes and audit history."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from .governance import valid_local_owner_request

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
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY, project TEXT NOT NULL, goal TEXT NOT NULL,
    plan_json TEXT NOT NULL, status TEXT NOT NULL,
    current_milestone INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    codex_thread TEXT NOT NULL DEFAULT '',
    context_json TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL, updated REAL NOT NULL,
    result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS deviations (
    id TEXT PRIMARY KEY, plan TEXT NOT NULL, project TEXT NOT NULL,
    status TEXT NOT NULL, reason TEXT NOT NULL, request_json TEXT NOT NULL,
    created REAL NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS plans_project_created ON plans(project, created DESC);
CREATE INDEX IF NOT EXISTS deviations_plan_created ON deviations(plan, created DESC);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        with self.lock, self.db:
            self.db.executescript(SCHEMA)
            # Ordinary jobs are not retried blindly. Approved project plans are
            # durable workflows, so a daemon restart makes them resumable.
            now = time.time()
            self.db.execute(
                "UPDATE jobs SET status='interrupted', updated=? WHERE status='running'",
                (now,),
            )
            interrupted = [
                row["id"] for row in self.db.execute(
                    "SELECT id FROM plans WHERE status='running'"
                ).fetchall()
            ]
            self.db.execute(
                "UPDATE plans SET status='approved', updated=? WHERE status='running'",
                (now,),
            )
            for pid in interrupted:
                row = self.db.execute(
                    "SELECT context_json FROM plans WHERE id=?", (pid,)
                ).fetchone()
                context = json.loads(row["context_json"]) if row else {}
                context["next_guidance"] = (
                    "The coordinator restarted mid-milestone. Re-inspect the "
                    "workspace and tests before continuing; do not assume the "
                    "previous Codex turn completed."
                )
                self.db.execute(
                    "UPDATE plans SET context_json=?,updated=? WHERE id=?",
                    (json.dumps(context), now, pid),
                )
                self.db.execute(
                    "INSERT INTO events(job,at,event,details) VALUES (?,?,?,?)",
                    (
                        pid, now, "plan_recovered_after_restart",
                        "Interrupted running lease was safely re-queued.",
                    ),
                )

    def event(self, job, kind, details=""):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO events(job,at,event,details) VALUES (?,?,?,?)",
                (job, time.time(), kind, str(details)[:4000]),
            )

    # ---------------- ordinary bounded jobs ----------------

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
                "SELECT at,event,details FROM events WHERE job=? ORDER BY seq LIMIT 500",
                (jid,),
            )]

    # ---------------- durable project plans ----------------

    @staticmethod
    def _plan_row(row):
        if not row:
            return None
        value = dict(row)
        value["plan"] = json.loads(value.pop("plan_json"))
        value["context"] = json.loads(value.pop("context_json"))
        return value

    def create_plan(self, project: str, goal: str, plan: dict) -> str:
        pid = plan["id"]
        now = time.time()
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO plans(id,project,goal,plan_json,status,created,updated) "
                "VALUES (?,?,?,?,?,?,?)",
                (pid, project, goal, json.dumps(plan), "awaiting_approval", now, now),
            )
        self.event(pid, "plan_created", project)
        return pid

    def get_plan(self, pid: str):
        with self.lock:
            row = self.db.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone()
        return self._plan_row(row)

    def latest_plan(self, project: str):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM plans WHERE project=? ORDER BY created DESC LIMIT 1",
                (project,),
            ).fetchone()
        return self._plan_row(row)

    def list_plans(self, project: str | None = None, limit: int = 30):
        with self.lock:
            if project:
                rows = self.db.execute(
                    "SELECT * FROM plans WHERE project=? ORDER BY created DESC LIMIT ?",
                    (project, limit),
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT * FROM plans ORDER BY created DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._plan_row(row) for row in rows]

    def update_plan(self, pid: str, **fields):
        allowed = {
            "status", "current_milestone", "attempts", "codex_thread",
            "context", "plan", "result", "error",
        }
        if not fields or any(key not in allowed for key in fields):
            raise ValueError("invalid plan update")
        with self.lock:
            state_row = self.db.execute(
                "SELECT status FROM plans WHERE id=?", (pid,)
            ).fetchone()
        if not state_row:
            raise ValueError("no such plan")
        current_status = state_row["status"]
        requested_status = fields.get("status")
        # Only the dedicated owner cancel transition may produce "cancelled".
        # Internal code historically used cancelled as a generic recovery sink,
        # which made technical failures indistinguishable from an owner action.
        if requested_status == "cancelled":
            with self.lock:
                context_row = self.db.execute(
                    "SELECT context_json FROM plans WHERE id=?", (pid,)
                ).fetchone()
            context = dict(fields.get("context") or (
                json.loads(context_row["context_json"]) if context_row else {}
            ))
            cycles = int(context.get("hard_replan_cycles", 0)) + 1
            context["hard_replan_cycles"] = cycles
            prior = str(fields.get("result", "")).strip()
            context["last_internal_failure"] = prior[:2400]
            if cycles <= 2:
                context["next_guidance"] = (
                    "Qwen manager hard recovery: start a genuinely fresh technical "
                    "replan from the current observed state. Re-inspect first, open "
                    "a fresh Codex thread, and do not repeat the failed strategy. "
                    + (("Previous failure: " + prior) if prior else "")
                )[:3500]
                fields["status"] = "approved"
                fields["attempts"] = 0
                fields["codex_thread"] = ""
                fields["context"] = context
                fields["result"] = ""
            else:
                context["next_guidance"] = ""
                fields["status"] = "failed_internal"
                fields["context"] = context
                if not prior:
                    fields["result"] = (
                        "Qwen and Codex exhausted bounded fresh replans without "
                        "identifying an owner decision."
                    )
            requested_status = fields.get("status")
        terminal = {"completed", "cancelled", "rejected", "failed_internal"}
        if current_status in terminal:
            if requested_status is not None and requested_status != current_status:
                raise ValueError("terminal plan state cannot be resurrected")
        if (
            current_status in ("awaiting_approval", "awaiting_deviation")
            and requested_status is not None
            and requested_status != current_status
        ):
            raise ValueError(
                "guarded plan state must use its dedicated transition method"
            )
        encoded = {}
        for key, value in fields.items():
            if key == "context":
                encoded["context_json"] = json.dumps(value)
            elif key == "plan":
                encoded["plan_json"] = json.dumps(value)
            else:
                encoded[key] = value
        encoded["updated"] = time.time()
        clause = ", ".join(f"{key}=?" for key in encoded)
        values = list(encoded.values()) + [pid]
        with self.lock, self.db:
            cur = self.db.execute(
                f"UPDATE plans SET {clause} WHERE id=?", values
            )
            if cur.rowcount != 1:
                raise ValueError("no such plan")
            if fields.get("status") in ("completed", "cancelled", "rejected", "failed_internal"):
                self.db.execute(
                    "UPDATE deviations SET status='superseded',updated=? "
                    "WHERE plan=? AND status='pending'",
                    (time.time(), pid),
                )

    def resume_from_deviation(self, pid: str, **fields) -> bool:
        """Apply a prepared deviation result only while the plan still waits."""
        allowed = ["status","current_milestone","attempts","codex_thread","context","plan","result","error"]
        if not fields or any(key not in allowed for key in fields):
            raise ValueError("invalid plan deviation resume")
        if fields.get("status", "approved") != "approved":
            raise ValueError("deviation resume must return the plan to approved")
        fields = dict(fields)
        fields["status"] = "approved"
        encoded = {}
        if "context" in fields:
            encoded["context_json"] = json.dumps(fields.pop("context"))
        if "plan" in fields:
            encoded["plan_json"] = json.dumps(fields.pop("plan"))
        encoded.update(fields)
        encoded["updated"] = time.time()
        clause = ", ".join(f"{key}=?" for key in encoded)
        values = list(encoded.values()) + [pid]
        with self.lock, self.db:
            cur = self.db.execute(
                f"UPDATE plans SET {clause} "
                "WHERE id=? AND status='awaiting_deviation'",
                values,
            )
            return cur.rowcount == 1

    def approve_plan(self, pid: str) -> bool:
        with self.lock, self.db:
            cur = self.db.execute(
                "UPDATE plans SET status='approved',updated=? "
                "WHERE id=? AND status='awaiting_approval'",
                (time.time(), pid),
            )
            ok = cur.rowcount == 1
        if ok:
            self.event(pid, "plan_approved_by_owner")
        return ok

    def reject_plan(self, pid: str) -> bool:
        with self.lock, self.db:
            cur = self.db.execute(
                "UPDATE plans SET status='rejected',updated=? "
                "WHERE id=? AND status='awaiting_approval'",
                (time.time(), pid),
            )
            ok = cur.rowcount == 1
        if ok:
            self.event(pid, "plan_rejected_by_owner")
        return ok

    def cancel_plan(self, pid: str) -> bool:
        with self.lock, self.db:
            cur = self.db.execute(
                "UPDATE plans SET status='cancelled',updated=? "
                "WHERE id=? AND status IN ('approved','running','awaiting_deviation')",
                (time.time(), pid),
            )
            ok = cur.rowcount == 1
            if ok:
                self.db.execute(
                    "UPDATE deviations SET status='superseded',updated=? "
                    "WHERE plan=? AND status='pending'",
                    (time.time(), pid),
                )
        if ok:
            self.event(pid, "plan_cancelled_by_owner")
        return ok

    def retry_internal_plan(self, pid: str) -> bool:
        """Requeue only an internal failure or a legacy non-owner cancellation."""
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT status,context_json FROM plans WHERE id=?", (pid,)
            ).fetchone()
            if not row:
                return False
            status = row["status"]
            if status == "cancelled":
                owner_cancel = self.db.execute(
                    "SELECT 1 FROM events WHERE job=? "
                    "AND event='plan_cancelled_by_owner' LIMIT 1",
                    (pid,),
                ).fetchone()
                if owner_cancel:
                    return False
            elif status != "failed_internal":
                return False
            context = json.loads(row["context_json"]) if row["context_json"] else {}
            context["hard_replan_cycles"] = 0
            context["next_guidance"] = (
                "Qwen manager recovery: start a fresh Codex thread and choose a "
                "materially different technical route from the current workspace "
                "state. Preserve completed work and prior owner approvals."
            )
            now = time.time()
            cur = self.db.execute(
                "UPDATE plans SET status='approved',attempts=0,codex_thread='',"
                "context_json=?,result='',error='',updated=? "
                "WHERE id=? AND status IN ('failed_internal','cancelled')",
                (json.dumps(context), now, pid),
            )
            ok = cur.rowcount == 1
        if ok:
            self.event(pid, "internal_plan_requeued", status)
        return ok

    def next_plan(self):
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT id FROM plans WHERE status='approved' ORDER BY created LIMIT 1"
            ).fetchone()
            if not row:
                return None
            pid = row["id"]
            cur = self.db.execute(
                "UPDATE plans SET status='running',updated=? "
                "WHERE id=? AND status='approved'",
                (time.time(), pid),
            )
            if cur.rowcount != 1:
                return None
        return self.get_plan(pid)

    # ---------------- deviations ----------------

    def create_deviation(self, plan: str, project: str, reason: str, request: dict) -> str:
        if not valid_local_owner_request(request):
            raise ValueError(
                "local deviation request is not eligible for owner approval"
            )
        did = uuid.uuid4().hex[:12]
        now = time.time()
        with self.lock, self.db:
            plan_row = self.db.execute(
                "SELECT status FROM plans WHERE id=?", (plan,)
            ).fetchone()
            if not plan_row:
                raise ValueError("cannot create deviation for missing plan")

            existing = self.db.execute(
                "SELECT id FROM deviations WHERE plan=? AND status='pending' "
                "ORDER BY created DESC LIMIT 1",
                (plan,),
            ).fetchone()
            if existing:
                if plan_row["status"] == "awaiting_deviation":
                    return existing["id"]
                self.db.execute(
                    "UPDATE deviations SET status='superseded',updated=? "
                    "WHERE id=? AND status='pending'",
                    (now, existing["id"]),
                )

            if plan_row["status"] != "running":
                raise ValueError(
                    "plan is no longer running; refusing stale deviation"
                )
            cur = self.db.execute(
                "UPDATE plans SET status='awaiting_deviation',updated=? "
                "WHERE id=? AND status='running'",
                (now, plan),
            )
            if cur.rowcount != 1:
                raise ValueError(
                    "plan changed concurrently; refusing stale deviation"
                )
            self.db.execute(
                "INSERT INTO deviations(id,plan,project,status,reason,request_json,created,updated) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (did, plan, project, "pending", reason[:4000], json.dumps(request), now, now),
            )
        self.event(plan, "deviation_requested", did + ": " + reason[:1000])
        return did

    def pending_deviation(self, plan: str):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM deviations "
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
                "SELECT * FROM deviations WHERE id=?", (did,)
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["request"] = json.loads(value.pop("request_json"))
        return value

    def list_deviations(self, project: str | None = None, plan: str | None = None):
        with self.lock:
            if plan:
                rows = self.db.execute(
                    "SELECT * FROM deviations WHERE plan=? ORDER BY created DESC", (plan,)
                ).fetchall()
            elif project:
                rows = self.db.execute(
                    "SELECT * FROM deviations WHERE project=? ORDER BY created DESC", (project,)
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT * FROM deviations ORDER BY created DESC LIMIT 50"
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
                "UPDATE deviations SET status=?,updated=? "
                "WHERE id=? AND status='pending' "
                "AND EXISTS (SELECT 1 FROM plans p "
                "WHERE p.id=deviations.plan AND p.status='awaiting_deviation')",
                (status, time.time(), did),
            )
            ok = cur.rowcount == 1
        return ok

    # ---------------- owner notes ----------------

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
