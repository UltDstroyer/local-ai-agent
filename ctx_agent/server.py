"""Unix-socket coordinator for bounded jobs and owner-approved project plans."""

from __future__ import annotations

import hashlib
import json
import os
import socketserver
import threading
import time
from pathlib import Path

from .agent import run_agent
from .codex_bridge import BridgeError
from .contracts import ContractError, Plan, analyze_changes, contract_violations
from .orchestrator import (
    OrchestrationError,
    approve_deviation,
    create_negotiated_plan,
    ensure_workspace,
    reject_deviation,
    run_plan_step,
)
from .remote import RemoteContractError, RemoteTarget
from .remote_orchestrator import (
    RemoteOrchestrationError,
    approve_remote_deviation,
    create_remote_plan,
    inspect_target,
    reject_remote_deviation,
    run_remote_plan_step,
)
from .remote_store import RemoteStore
from .progress import (
    LOCAL_PLANNING_STAGES,
    REMOTE_PLANNING_STAGES,
    plan_snapshot,
    planning_snapshot,
)
from .runtime import Settings, valid_name
from .snapshot import SnapshotError, apply, create, diff, inventory, project_path
from .ssh_bridge import SSHBridgeError
from .store import Store


def digest_inventory(root: Path):
    return {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in inventory(root).items()
    }


class Coordinator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.settings.projects_dir.mkdir(parents=True, exist_ok=True)
        self.jobs = self.settings.state_dir / "jobs"
        self.jobs.mkdir(parents=True, exist_ok=True)
        if self.settings.workspaces_dir:
            self.settings.workspaces_dir.mkdir(parents=True, exist_ok=True)
        if self.settings.checkpoints_dir:
            self.settings.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        self.plan_baselines = self.settings.state_dir / "plan-baselines"
        self.plan_baselines.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.settings.state_dir / "state.sqlite3")
        self.remote_store = RemoteStore(
            self.settings.state_dir / "remote-state.sqlite3"
        )
        self.paused = False
        self.mode = "quiet"
        try:
            saved = json.loads(
                (self.settings.state_dir / "resource_mode.json").read_text()
            )
            if saved in ("quiet", "balanced", "maximum"):
                self.mode = saved
        except (OSError, ValueError):
            pass
        self.shutdown = threading.Event()
        self.lock = threading.RLock()
        self.execution_lock = threading.RLock()
        self.planning_progress = {}

    def project(self, name):
        return project_path(self.settings.projects_dir, name)

    def _planning_key(self, kind: str, scope: str):
        return (kind, scope)

    def _planning_start(self, kind: str, scope: str):
        specs = REMOTE_PLANNING_STAGES if kind == "remote_plan" else LOCAL_PLANNING_STAGES
        now = time.time()
        state = {
            "kind": kind,
            "scope": scope,
            "stages": [
                {"name": name, "estimated_seconds": seconds}
                for name, seconds in specs
            ],
            "stage_index": 0,
            "started_at": now,
            "stage_started_at": now,
            "planned_stages": [],
            "active": True,
            "status": "running",
        }
        with self.lock:
            self.planning_progress[self._planning_key(kind, scope)] = state

    def _planning_update(
        self, kind: str, scope: str, stage_index: int, planned_stages=None
    ):
        with self.lock:
            state = self.planning_progress.get(self._planning_key(kind, scope))
            if not state:
                return
            if int(stage_index) != int(state.get("stage_index", 0)):
                state["stage_index"] = int(stage_index)
                state["stage_started_at"] = time.time()
            if isinstance(planned_stages, (list, tuple)):
                state["planned_stages"] = [
                    str(x) for x in planned_stages if str(x).strip()
                ][:50]

    def _planning_finish(self, kind: str, scope: str, status: str, error: str = ""):
        with self.lock:
            state = self.planning_progress.get(self._planning_key(kind, scope))
            if not state:
                return
            state["active"] = False
            state["status"] = status
            state["phase"] = status
            if error:
                state["error"] = error[:2000]

    def _planning_view(self, kind: str, scope: str):
        with self.lock:
            state = self.planning_progress.get(self._planning_key(kind, scope))
            if not state:
                return None
            snapshot = dict(state)
            snapshot["stages"] = [dict(x) for x in state.get("stages", [])]
            snapshot["planned_stages"] = list(state.get("planned_stages", []))
        return planning_snapshot(snapshot)

    def _planning_active_count(self) -> int:
        with self.lock:
            return sum(
                1 for state in self.planning_progress.values()
                if state.get("active")
            )

    def _plan(self, project: str, pid: str = ""):
        if pid:
            row = self.store.get_plan(pid)
            if not row or row["project"] != project:
                raise ValueError("No such plan for this project")
            return row
        row = self.store.latest_plan(project)
        if not row:
            raise ValueError("Project has no plan")
        return row

    def _baseline_path(self, pid: str) -> Path:
        return self.plan_baselines / f"{pid}.json"

    def _plan_view(self, row: dict) -> dict:
        value = dict(row)
        workspace = (
            self.settings.workspaces_dir / row["id"]
            if self.settings.workspaces_dir else None
        )
        if workspace and workspace.is_dir():
            report = analyze_changes(self.project(row["project"]), workspace)
            value["mutation_usage"] = report.to_dict()
        value["deviations"] = self.store.list_deviations(plan=row["id"])[:10]
        value["progress"] = plan_snapshot(
            row,
            self.store.events(row["id"]),
            remote=False,
            mode=self.mode,
            paused=self.paused,
        )
        return value

    def _remote_plan_view(self, row: dict) -> dict:
        value = dict(row)
        value["deviations"] = self.remote_store.list_deviations(
            plan=row["id"]
        )[:10]
        value["progress"] = plan_snapshot(
            row,
            self.remote_store.events(row["id"]),
            remote=True,
            mode=self.mode,
            paused=self.paused,
        )
        return value

    def dispatch(self, data: dict):
        op = data.get("op")
        if op == "health":
            active = [
                p for p in self.store.list_plans(limit=20)
                if p["status"] in (
                    "approved", "running", "awaiting_deviation", "ready_for_review"
                )
            ]
            remote_active = [
                p for p in self.remote_store.list_plans(limit=20)
                if p["status"] in (
                    "awaiting_approval", "approved", "running",
                    "awaiting_deviation"
                )
            ]
            return {
                "status": "running",
                "paused": self.paused,
                "model": self.settings.model,
                "resource_mode": self.mode,
                "project_plans": len(active),
                "remote_plans": len(remote_active),
                "remote_targets": len(self.remote_store.list_targets()),
                "ssh_bridge_configured": self.settings.ssh_bridge_socket is not None,
                "autonomous_execution": self.mode == "maximum",
                "planning_tasks": self._planning_active_count(),
            }
        if op == "mode":
            mode = data.get("mode")
            if mode not in ("quiet", "balanced", "maximum"):
                raise ValueError("Resource mode must be quiet, balanced or maximum")
            with self.lock:
                self.mode = mode
                (self.settings.state_dir / "resource_mode.json").write_text(
                    json.dumps(mode)
                )
            self.store.event(None, "resource_mode_changed", mode)
            return {
                "mode": mode,
                "autonomous_execution": mode == "maximum",
            }
        if op == "projects":
            return {
                "projects": sorted(
                    x.name
                    for x in self.settings.projects_dir.iterdir()
                    if x.is_dir() and not x.is_symlink() and valid_name(x.name)
                )
            }
        if op == "init":
            project = data.get("project", "")
            path = self.project(project)
            path.mkdir(mode=0o770, exist_ok=False)
            return {"project": project, "path": str(path)}

        # -------- owner-managed SSH targets --------
        if op == "target_add":
            name = data.get("name", "")
            endpoint = data.get("ssh", "")
            port = data.get("port", 22)
            target = RemoteTarget.parse_ssh(name, endpoint, port)
            self.remote_store.put_target(target.to_dict())
            self.remote_store.event(None, "target_saved", target.name)
            return {"target": target.to_dict()}
        if op == "target_list":
            return {"targets": self.remote_store.list_targets()}
        if op in ("target_show", "target_remove", "target_inspect"):
            name = data.get("name", "")
            target_raw = self.remote_store.get_target(name)
            if not target_raw:
                raise ValueError("Unknown SSH target")
            target = RemoteTarget.from_dict(target_raw)
            if op == "target_show":
                return {"target": target.to_dict()}
            if op == "target_remove":
                if not self.remote_store.delete_target(name):
                    raise ValueError(
                        "Target has an active remote plan or does not exist"
                    )
                return {"removed": name}
            result = inspect_target(self.settings, target)
            return {
                "target": target.to_dict(),
                "inspection": result.get("output", ""),
            }

        # -------- negotiated SSH remote plans --------
        if op == "remote_plan_create":
            target_name = data.get("target", "")
            goal = data.get("goal", "")
            target_raw = self.remote_store.get_target(target_name)
            if not target_raw:
                raise ValueError("Unknown SSH target")
            if not isinstance(goal, str) or not 1 <= len(goal) <= 6000:
                raise ValueError("Goal must be 1 to 6000 characters")
            self._planning_start("remote_plan", target_name)
            try:
                row = create_remote_plan(
                    self.settings,
                    self.remote_store,
                    RemoteTarget.from_dict(target_raw),
                    goal,
                    self.mode,
                    progress=lambda index, stages=None: self._planning_update(
                        "remote_plan", target_name, index, stages
                    ),
                )
            except Exception as exc:
                self._planning_finish(
                    "remote_plan", target_name, "failed", str(exc)
                )
                raise
            self._planning_finish("remote_plan", target_name, "finished")
            return {"plan": self._remote_plan_view(row)}
        if op == "remote_plan_list":
            return {
                "plans": self.remote_store.list_plans(
                    target=data.get("target") or None
                )
            }
        if op == "remote_plan_progress":
            target_name = data.get("target", "")
            active = self._planning_view("remote_plan", target_name)
            if active and active.get("status") == "running":
                return {"progress": active}
            pid = data.get("id", "")
            row = (
                self.remote_store.get_plan(pid)
                if pid else self.remote_store.latest_plan(target_name)
            )
            if row and (not target_name or row["target"] == target_name):
                return {
                    "progress": plan_snapshot(
                        row,
                        self.remote_store.events(row["id"]),
                        remote=True,
                        mode=self.mode,
                        paused=self.paused,
                    )
                }
            if active:
                return {"progress": active}
            raise ValueError("No remote planning task or plan for target")
        if op in (
            "remote_plan_show", "remote_plan_approve",
            "remote_plan_reject", "remote_plan_cancel",
            "remote_plan_events",
        ):
            target_name = data.get("target", "")
            pid = data.get("id", "")
            row = (
                self.remote_store.get_plan(pid)
                if pid else self.remote_store.latest_plan(target_name)
            )
            if not row or (target_name and row["target"] != target_name):
                raise ValueError("No such remote plan")
            if op == "remote_plan_show":
                return {"plan": self._remote_plan_view(row)}
            if op == "remote_plan_events":
                return {
                    "events": self.remote_store.events(row["id"])
                }
            if op == "remote_plan_approve":
                if not self.remote_store.approve_plan(row["id"]):
                    raise ValueError(
                        "Remote plan is not awaiting initial approval"
                    )
                return {
                    "plan": self._remote_plan_view(
                        self.remote_store.get_plan(row["id"])
                    )
                }
            if op == "remote_plan_reject":
                if not self.remote_store.reject_plan(row["id"]):
                    raise ValueError(
                        "Remote plan is not awaiting initial approval"
                    )
                return {
                    "plan": self._remote_plan_view(
                        self.remote_store.get_plan(row["id"])
                    )
                }
            if not self.remote_store.cancel_plan(row["id"]):
                raise ValueError(
                    "Remote plan cannot be cancelled in its current state"
                )
            return {
                "plan": self._remote_plan_view(
                    self.remote_store.get_plan(row["id"])
                )
            }

        if op == "remote_deviation_list":
            return {
                "deviations": self.remote_store.list_deviations(
                    target=data.get("target") or None
                )
            }
        if op in (
            "remote_deviation_show",
            "remote_deviation_approve",
            "remote_deviation_reject",
        ):
            did = data.get("id", "")
            if not isinstance(did, str) or len(did) != 12 or not did.isalnum():
                raise ValueError("Invalid remote deviation identifier")
            deviation = self.remote_store.get_deviation(did)
            if not deviation:
                raise ValueError("No such remote deviation")
            if op == "remote_deviation_show":
                return {"deviation": deviation}
            if op == "remote_deviation_approve":
                return {
                    "plan": approve_remote_deviation(
                        self.settings, self.remote_store, did
                    )
                }
            return {
                "plan": reject_remote_deviation(
                    self.settings, self.remote_store, did
                )
            }

        # -------- ordinary bounded local jobs --------
        if op == "submit":
            project = data.get("project", "")
            prompt = data.get("prompt", "")
            if not self.project(project).is_dir():
                raise ValueError("Unknown project; run ctx-agent init first")
            if not isinstance(prompt, str) or not 1 <= len(prompt) <= 6000:
                raise ValueError("Prompt must be 1 to 6000 characters")
            return {"id": self.store.submit(project, prompt)}
        if op == "list":
            return {"jobs": self.store.list()}
        if op in ("show", "events", "cancel", "diff", "apply"):
            jid = data.get("id", "")
            if not isinstance(jid, str) or len(jid) != 12 or not jid.isalnum():
                raise ValueError("Invalid job identifier")
            job = self.store.get(jid)
            if not job:
                raise ValueError("No such job")
            if op == "show":
                return {"job": job}
            if op == "events":
                return {"events": self.store.events(jid)}
            if op == "cancel":
                return {"cancelled": self.store.cancel(jid)}
            if job["status"] != "completed":
                raise ValueError("Job is not completed")
            source = self.project(job["project"])
            scratch = self.jobs / jid / "work"
            changed, patch = diff(source, scratch)
            if op == "diff":
                return {"changed": changed, "diff": patch}
            baseline = json.loads((self.jobs / jid / "baseline.json").read_text())
            current = digest_inventory(source)
            for rel in changed:
                if current.get(rel) != baseline.get(rel):
                    raise ValueError(f"Project changed since snapshot: {rel}")
            applied = apply(source, scratch)
            self.store.event(jid, "applied_by_owner", ",".join(applied)[:1000])
            return {"applied": applied}

        # -------- negotiated Codex project plans --------
        if op == "plan_create":
            project = data.get("project", "")
            goal = data.get("goal", "")
            source = self.project(project)
            if not source.is_dir():
                raise ValueError("Unknown project")
            if not isinstance(goal, str) or not 1 <= len(goal) <= 6000:
                raise ValueError("Goal must be 1 to 6000 characters")
            self._planning_start("project_plan", project)
            try:
                row = create_negotiated_plan(
                    self.settings,
                    self.store,
                    source,
                    project,
                    goal,
                    self.mode,
                    progress=lambda index, stages=None: self._planning_update(
                        "project_plan", project, index, stages
                    ),
                )
            except Exception as exc:
                self._planning_finish(
                    "project_plan", project, "failed", str(exc)
                )
                raise
            self._planning_finish("project_plan", project, "finished")
            return {"plan": self._plan_view(row)}
        if op == "plan_list":
            project = data.get("project") or None
            return {"plans": self.store.list_plans(project=project)}
        if op == "plan_progress":
            project = data.get("project", "")
            active = self._planning_view("project_plan", project)
            if active and active.get("status") == "running":
                return {"progress": active}
            pid = data.get("id", "")
            row = self.store.get_plan(pid) if pid else self.store.latest_plan(project)
            if row and (not project or row["project"] == project):
                return {
                    "progress": plan_snapshot(
                        row,
                        self.store.events(row["id"]),
                        remote=False,
                        mode=self.mode,
                        paused=self.paused,
                    )
                }
            if active:
                return {"progress": active}
            raise ValueError("No planning task or plan for project")
        if op in (
            "plan_show", "plan_approve", "plan_reject", "plan_cancel",
            "plan_diff", "plan_apply",
        ):
            project = data.get("project", "")
            source = self.project(project)
            row = self._plan(project, data.get("id", ""))
            pid = row["id"]
            if op == "plan_show":
                return {"plan": self._plan_view(row)}
            if op == "plan_approve":
                if row["status"] != "awaiting_approval":
                    raise ValueError("Plan is not awaiting initial approval")
                self._baseline_path(pid).write_text(
                    json.dumps(digest_inventory(source))
                )
                if not self.store.approve_plan(pid):
                    raise ValueError("Plan changed concurrently")
                return {"plan": self._plan_view(self.store.get_plan(pid))}
            if op == "plan_reject":
                if not self.store.reject_plan(pid):
                    raise ValueError("Plan is not awaiting approval")
                return {"plan": self._plan_view(self.store.get_plan(pid))}
            if op == "plan_cancel":
                if not self.store.cancel_plan(pid):
                    raise ValueError("Plan cannot be cancelled in its current state")
                return {"plan": self._plan_view(self.store.get_plan(pid))}

            workspace = ensure_workspace(self.settings, source, pid)
            report = analyze_changes(source, workspace)
            changed, patch = diff(source, workspace)
            if op == "plan_diff":
                return {
                    "changed": changed,
                    "diff": patch,
                    "mutation_usage": report.to_dict(),
                }
            if row["status"] != "ready_for_review":
                raise ValueError("Plan is not ready for final owner review")
            plan = Plan.from_dict(row["plan"], project, pid)
            violations = contract_violations(plan, report)
            if violations:
                raise ValueError("Final diff violates approved plan: " + "; ".join(violations))
            baseline_path = self._baseline_path(pid)
            if not baseline_path.exists():
                raise ValueError("Missing approved-plan baseline")
            baseline = json.loads(baseline_path.read_text())
            current = digest_inventory(source)
            for rel in report.changed:
                if current.get(rel) != baseline.get(rel):
                    raise ValueError(f"Managed project changed since approval: {rel}")
            applied = apply(
                source,
                workspace,
                allow_deletions=plan.budget.allow_file_deletion,
            )
            self.store.update_plan(
                pid,
                status="completed",
                result=row.get("result", "") + "\n\nApplied by owner.",
            )
            self.store.event(pid, "plan_applied_by_owner", ",".join(applied)[:2000])
            return {"applied": applied, "plan": self.store.get_plan(pid)}

        if op == "deviation_list":
            return {
                "deviations": self.store.list_deviations(
                    project=data.get("project") or None
                )
            }
        if op in ("deviation_show", "deviation_approve", "deviation_reject"):
            did = data.get("id", "")
            if not isinstance(did, str) or len(did) != 12 or not did.isalnum():
                raise ValueError("Invalid deviation identifier")
            deviation = self.store.get_deviation(did)
            if not deviation:
                raise ValueError("No such deviation")
            if op == "deviation_show":
                return {"deviation": deviation}
            source = self.project(deviation["project"])
            if op == "deviation_approve":
                return {
                    "plan": approve_deviation(
                        self.settings, self.store, source, did
                    )
                }
            return {
                "plan": reject_deviation(
                    self.settings, self.store, source, did
                )
            }

        if op == "pause":
            self.paused = True
            return {
                "paused": True,
                "note": "Stops picking up new work; an active turn may finish.",
            }
        if op == "resume":
            self.paused = False
            return {"paused": False}
        if op == "note":
            project, key, value = (
                data.get("project"), data.get("key"), data.get("text")
            )
            if not self.project(project).is_dir() or not valid_name(key):
                raise ValueError("Invalid project or key")
            if not isinstance(value, str) or len(value) > 2000:
                raise ValueError("Note must be at most 2000 characters")
            self.store.note(project, key, value)
            return {"saved": key}
        if op == "notes":
            project = data.get("project")
            if not self.project(project).is_dir():
                raise ValueError("Unknown project")
            return {"notes": self.store.notes(project)}
        raise ValueError("Unknown request")

    def worker(self):
        while not self.shutdown.is_set():
            try:
                if self.paused:
                    self.shutdown.wait(0.5)
                    continue
                job = self.store.next()
                if not job:
                    self.shutdown.wait(0.5)
                    continue
                jid = job["id"]
                base = self.jobs / jid
                try:
                    source = self.project(job["project"])
                    base.mkdir(mode=0o700, exist_ok=False)
                    scratch = base / "work"
                    create(source, scratch)
                    (base / "baseline.json").write_text(
                        json.dumps(digest_inventory(source))
                    )
                    self.store.event(jid, "snapshot_ready")
                    with self.execution_lock:
                        answer = run_agent(
                            self.settings,
                            scratch,
                            job["prompt"],
                            self.store.notes(job["project"]),
                            lambda kind, detail: self.store.event(jid, kind, detail),
                            lambda: self.store.cancelled(jid),
                            self.mode,
                        )
                    if not self.store.cancelled(jid):
                        changes, _ = diff(source, scratch)
                        self.store.finish(
                            jid,
                            "completed",
                            answer[:20000]
                            + "\n\nChanged files: "
                            + ", ".join(changes),
                        )
                except Exception as exc:
                    if not self.store.cancelled(jid):
                        self.store.finish(jid, "failed", error=str(exc))
            except Exception:
                self.shutdown.wait(1)

    def remote_plan_worker(self):
        while not self.shutdown.is_set():
            try:
                if self.paused or self.mode != "maximum":
                    self.shutdown.wait(0.75)
                    continue
                row = self.remote_store.next_plan()
                if not row:
                    self.shutdown.wait(0.75)
                    continue
                try:
                    with self.execution_lock:
                        run_remote_plan_step(
                            self.settings,
                            self.remote_store,
                            row,
                            self.mode,
                        )
                except Exception as exc:
                    current = self.remote_store.get_plan(row["id"])
                    if current and current["status"] == "running":
                        self.remote_store.create_deviation(
                            row["id"],
                            row["target"],
                            "Remote autonomous step stopped by a system/tool error",
                            {
                                "type": "system_error",
                                "error": str(exc)[:4000],
                                "suggested_action": (
                                    "Check SSH/Codex bridges and remote connectivity, "
                                    "then approve to retry."
                                ),
                            },
                        )
            except Exception:
                self.shutdown.wait(1)

    def plan_worker(self):
        while not self.shutdown.is_set():
            try:
                # Maximum mode is the explicitly autonomous mode. Planning and
                # approval are available in every mode, but execution waits here.
                if self.paused or self.mode != "maximum":
                    self.shutdown.wait(0.75)
                    continue
                row = self.store.next_plan()
                if not row:
                    self.shutdown.wait(0.75)
                    continue
                try:
                    source = self.project(row["project"])
                    with self.execution_lock:
                        run_plan_step(
                            self.settings, self.store, source, row, self.mode
                        )
                except Exception as exc:
                    current = self.store.get_plan(row["id"])
                    if current and current["status"] == "running":
                        request = {
                            "type": "system_error",
                            "error": str(exc)[:4000],
                            "suggested_action": (
                                "Check Codex bridge/authentication or service logs, "
                                "then approve to retry."
                            ),
                        }
                        self.store.create_deviation(
                            row["id"],
                            row["project"],
                            "Autonomous step stopped by a system/tool error",
                            request,
                        )
            except Exception:
                self.shutdown.wait(1)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        raw = self.rfile.readline(262145)
        if len(raw) > 262144:
            return
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError("Request must be a JSON object")
            output = {"ok": True, "result": self.server.coordinator.dispatch(obj)}
        except (
            ValueError,
            OSError,
            SnapshotError,
            ContractError,
            OrchestrationError,
            RemoteContractError,
            RemoteOrchestrationError,
            SSHBridgeError,
            BridgeError,
            json.JSONDecodeError,
        ) as exc:
            output = {"ok": False, "error": str(exc)}
        except Exception:
            output = {"ok": False, "error": "Internal request error"}
        self.wfile.write((json.dumps(output) + "\n").encode())


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    settings = Settings.load(args.config)
    coordinator = Coordinator(settings)
    settings.socket_path.parent.mkdir(parents=True, exist_ok=True)
    if settings.socket_path.exists():
        if not settings.socket_path.is_socket():
            raise RuntimeError("Refusing to replace a non-socket path")
        settings.socket_path.unlink()
    with UnixServer(str(settings.socket_path), Handler) as server:
        server.coordinator = coordinator
        os.chmod(settings.socket_path, 0o660)
        job_worker = threading.Thread(target=coordinator.worker, daemon=True)
        plan_worker = threading.Thread(target=coordinator.plan_worker, daemon=True)
        remote_plan_worker = threading.Thread(
            target=coordinator.remote_plan_worker, daemon=True
        )
        job_worker.start()
        plan_worker.start()
        remote_plan_worker.start()
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            coordinator.shutdown.set()
            job_worker.join(timeout=2)
            plan_worker.join(timeout=2)
            remote_plan_worker.join(timeout=2)
            settings.socket_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
