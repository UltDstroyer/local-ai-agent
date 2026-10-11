"""Unix-socket coordinator for bounded jobs and owner-approved project plans."""

from __future__ import annotations

import hashlib
import json
import os
import socketserver
import threading
import time
from pathlib import Path

from .agent import ask, run_agent
from .codex_bridge import BridgeError
from .contracts import ContractError, Plan, analyze_changes, contract_violations
from .manager_brief import manager_deviation_brief
from .manager_report import manager_progress_report
from .management import ensure_management_contract, recovery_delta
from .governance import (
    owner_decision_key,
    valid_local_owner_request,
    valid_remote_owner_request,
)
from .orchestrator import (
    OrchestrationError,
    approve_deviation,
    classify_project_error,
    create_negotiated_plan,
    ensure_workspace,
    reject_deviation,
    restore_checkpoint,
    run_plan_step,
)
from .remote import RemoteContractError, RemoteTarget
from .remote_orchestrator import (
    RemoteOrchestrationError,
    approve_remote_deviation,
    classify_remote_error,
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
        self.remote_inputs: dict[str, dict[str, str]] = {}
        # Human-input values are intentionally process-memory only. If the
        # coordinator restarted, persisted availability markers are stale.
        for remote_row in self.remote_store.list_plans(limit=100):
            if remote_row.get("status") not in (
                "approved", "running", "awaiting_deviation"
            ):
                continue
            context = dict(remote_row.get("context", {}))
            available = list(context.get("available_inputs", []))
            if not available:
                continue
            expired = list(context.get("expired_inputs", []))
            for key in available:
                if key not in expired:
                    expired.append(key)
            context["available_inputs"] = []
            context["expired_inputs"] = expired[-20:]
            context["next_guidance"] = (
                "The coordinator restarted and ephemeral human inputs were cleared. "
                "If one is still required, request that one input from the owner again."
            )
            self.remote_store.update_plan(remote_row["id"], context=context)
            self.remote_store.event(
                remote_row["id"],
                "remote_ephemeral_inputs_expired_after_restart",
                ",".join(str(x) for x in available)[:1000],
            )

        self._retire_invalid_remote_deviations()
        self._retire_invalid_local_deviations()

    def project(self, name):
        return project_path(self.settings.projects_dir, name)

    def _retire_invalid_remote_deviations(self):
        for row in self.remote_store.list_plans(limit=100):
            if row.get("status") != "awaiting_deviation":
                continue
            deviation = self.remote_store.pending_deviation(row["id"])
            if not deviation or valid_remote_owner_request(deviation.get("request")):
                continue
            if not self.remote_store.resolve_deviation(deviation["id"], False):
                continue
            context = dict(row.get("context", {}))
            context["next_guidance"] = (
                "Qwen governance retired an internal/obsolete request before it "
                "reached the boss. Re-evaluate the technical problem with Codex "
                "and escalate only a concrete authority, dependency, credential, "
                "or human-choice decision."
            )
            if self.remote_store.resume_from_deviation(
                row["id"], attempts=0, context=context
            ):
                self.remote_store.event(
                    row["id"],
                    "remote_internal_deviation_auto_retired",
                    deviation["id"],
                )
            else:
                self.remote_store.event(
                    row["id"],
                    "remote_internal_deviation_retired_without_resume",
                    deviation["id"] + ": plan changed concurrently",
                )

    def _retire_invalid_local_deviations(self):
        for row in self.store.list_plans(limit=100):
            if row.get("status") != "awaiting_deviation":
                continue
            deviation = self.store.pending_deviation(row["id"])
            if not deviation or valid_local_owner_request(deviation.get("request")):
                continue
            if not self.store.resolve_deviation(deviation["id"], False):
                continue
            request = deviation.get("request") or {}
            if (
                "mutation_budget" in request
                or request.get("add_write_scope")
            ):
                try:
                    restore_checkpoint(
                        self.settings, self.project(row["project"]), row
                    )
                except Exception:
                    self.store.fail_internal_plan(
                        row["id"],
                        (
                            "An obsolete mutation request could not be safely "
                            "rolled back during governance recovery."
                        ),
                        context=dict(row.get("context", {})),
                    )
                    continue
            context = dict(row.get("context", {}))
            context["next_guidance"] = (
                "Qwen governance retired an internal/obsolete request before it "
                "reached the boss. Re-evaluate with Codex and escalate only a "
                "real owner authority or preference decision."
            )
            if self.store.resume_from_deviation(
                row["id"], attempts=0, context=context
            ):
                self.store.event(
                    row["id"],
                    "project_internal_deviation_auto_retired",
                    deviation["id"],
                )
            else:
                self.store.event(
                    row["id"],
                    "project_internal_deviation_retired_without_resume",
                    deviation["id"] + ": plan changed concurrently",
                )

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

    def _owner_policy_path(self) -> Path:
        return self.settings.state_dir / "owner-policy.json"

    def _load_owner_policy(self) -> dict:
        try:
            value = json.loads(self._owner_policy_path().read_text())
            if isinstance(value, dict):
                return value
        except (OSError, ValueError, TypeError):
            pass
        return {
            "explicit_preferences": [],
            "decision_evidence": [],
        }

    def _save_owner_policy(self, policy: dict):
        path = self._owner_policy_path()
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(policy, indent=2, ensure_ascii=False))
        temp.replace(path)

    def _record_owner_decision_evidence(
        self, *, outcome: str, request: dict, scope: str
    ):
        policy = self._load_owner_policy()
        evidence = list(policy.get("decision_evidence") or [])
        proposed = request.get("proposed_action") or request.get("action") or {}
        evidence.append({
            "at": time.time(),
            "scope": scope,
            "outcome": outcome,
            "type": str(request.get("type", ""))[:80],
            "risk": str(request.get("risk", ""))[:30],
            "operation": (
                str(proposed.get("op", ""))[:80]
                if isinstance(proposed, dict) else ""
            ),
            "subject": (
                str(proposed.get("service") or proposed.get("path") or "")[:240]
                if isinstance(proposed, dict) else ""
            ),
        })
        policy["decision_evidence"] = evidence[-120:]
        self._save_owner_policy(policy)

    def _learn_explicit_owner_preferences(self, message: str):
        """Learn only preferences the owner explicitly states, never inferred traits."""
        policy = self._load_owner_policy()
        try:
            reply = ask(
                self.settings,
                [
                    {
                        "role": "system",
                        "content": (
                            "Extract only explicit durable work preferences. "
                            "Do not infer personality, identity, beliefs, or unstated preferences. "
                            "Return strict JSON only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Owner message:\n" + message[:4000] +
                            "\n\nReturn JSON: "
                            '{"preferences":[{"rule":"short explicit preference",'
                            '"evidence":"brief quote/paraphrase from the owner"}]}. '
                            "If there is no explicit durable preference, return an empty list."
                        ),
                    },
                ],
                "balanced" if self.mode == "quiet" else self.mode,
                tools=[],
                json_mode=True,
                num_predict=500,
                num_ctx=4096,
            )
            value = json.loads(str(reply.get("content", "")))
            prefs = value.get("preferences") if isinstance(value, dict) else []
        except Exception:
            prefs = []
        if not isinstance(prefs, list):
            prefs = []
        existing = [
            item for item in policy.get("explicit_preferences", [])
            if isinstance(item, dict) and isinstance(item.get("rule"), str)
        ]
        known = {item["rule"].strip().lower() for item in existing}
        for item in prefs[:6]:
            if not isinstance(item, dict):
                continue
            rule = str(item.get("rule", "")).strip()[:500]
            evidence = str(item.get("evidence", "")).strip()[:700]
            if not rule or rule.lower() in known:
                continue
            existing.append({
                "rule": rule,
                "evidence": evidence,
                "recorded_at": time.time(),
            })
            known.add(rule.lower())
        policy["explicit_preferences"] = existing[-80:]
        self._save_owner_policy(policy)

    def _plan_lineage(self, row: dict, *, remote: bool) -> list[dict]:
        store = self.remote_store if remote else self.store
        lineage = []
        seen = set()
        current = row
        while current and current.get("id") not in seen and len(lineage) < 8:
            seen.add(current.get("id"))
            lineage.append(current)
            parent = str(
                (current.get("context") or {}).get("recovery_of_plan_id", "")
            ).strip()
            current = store.get_plan(parent) if parent else None
        lineage.reverse()
        return lineage

    def _manager_report(self, row: dict, *, remote: bool) -> dict:
        store = self.remote_store if remote else self.store
        lineage = self._plan_lineage(row, remote=remote)
        events = []
        deviations = []
        for item in lineage:
            events.extend(store.events(item["id"]))
            deviations.extend(store.list_deviations(plan=item["id"]))
        report = manager_progress_report(
            self.settings,
            lineage,
            events,
            deviations,
            self.mode,
            owner_policy=self._load_owner_policy(),
        )
        return {
            "report": report,
            "plan_ids": [item["id"] for item in lineage],
            "current_plan_id": row["id"],
        }

    def _manager_chat(self, row: dict, message: str, *, remote: bool) -> dict:
        if not isinstance(message, str) or not message.strip() or len(message) > 4000:
            raise ValueError("Manager chat message must be 1 to 4000 characters")
        message = message.strip()
        store = self.remote_store if remote else self.store
        context = ensure_management_contract(row.get("context", {}))
        context["owner_policy_snapshot"] = self._load_owner_policy()
        conversation = [
            item for item in context.get("manager_conversation", [])
            if isinstance(item, dict)
            and item.get("role") in ("owner", "qwen")
            and isinstance(item.get("content"), str)
        ][-10:]
        pending = store.pending_deviation(row["id"])
        plan = row.get("plan") or {}
        milestones = list(plan.get("milestones") or [])
        index = int(row.get("current_milestone", 0) or 0)
        current = milestones[index] if 0 <= index < len(milestones) else "final review"
        pending_summary = None
        if pending:
            pending_summary = {
                "id": pending.get("id", ""),
                "reason": pending.get("reason", ""),
                "request": pending.get("request", {}),
            }
        prompt = f"""You are Qwen, the manager speaking directly with your human boss.
Codex and execution tools report to you. The boss may ask questions, add
requirements, challenge your recommendation, or change priorities.

Current plan:
{json.dumps({
    "id": row.get("id", ""),
    "scope": row.get("target") if remote else row.get("project"),
    "status": row.get("status", ""),
    "goal": row.get("goal", ""),
    "current_milestone": current,
    "milestones": milestones,
}, ensure_ascii=False)[:12000]}

Pending owner decision, if any:
{json.dumps(pending_summary, ensure_ascii=False)[:8000]}

Recent manager conversation:
{json.dumps(conversation, ensure_ascii=False)[:8000]}

Boss message:
{message}

Reply as Qwen in plain language. Be concise but substantive. Explain your
reasoning at the manager level, not with raw stack traces or tool noise.
Treat the boss message as authoritative guidance and preferences within the
already-approved scope. It is NOT approval of a pending package, service,
credential, destructive action, or other authority gate. If a pending
deviation still needs an explicit approve/reject command, say so clearly.
Never ask the boss for machine state you can inspect yourself. Do not expose
secret values or request secrets through chat; use the secure input flow.
"""
        reply = ask(
            self.settings,
            [
                {
                    "role": "system",
                    "content": (
                        "You are Qwen, the manager. Speak directly to your boss. "
                        "Natural-language chat never bypasses approval gates."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "balanced" if self.mode == "quiet" else self.mode,
            tools=[],
            json_mode=False,
            num_predict=900,
            num_ctx=8192,
        )
        answer = str(reply.get("content", "")).strip()[:6000]
        if not answer:
            answer = "I received your guidance and will carry it into the next work cycle."
        conversation.extend([
            {"role": "owner", "content": message[:4000]},
            {"role": "qwen", "content": answer[:6000]},
        ])
        context["manager_conversation"] = conversation[-20:]
        guidance = [
            item for item in context.get("owner_guidance", [])
            if isinstance(item, str) and item.strip()
        ]
        guidance.append(message[:4000])
        context["owner_guidance"] = guidance[-12:]
        context["last_owner_guidance"] = message[:4000]
        # Make the newest boss instruction impossible to miss on the next worker
        # turn without erasing Qwen's existing technical guidance.
        previous = str(context.get("next_guidance", "")).strip()
        context["next_guidance"] = (
            "OWNER GUIDANCE (highest priority within existing approved authority): "
            + message[:3000]
            + (("\n\nExisting manager guidance: " + previous) if previous else "")
        )[:5000]
        store.update_plan(row["id"], context=context)
        store.event(
            row["id"],
            "owner_manager_chat",
            "Owner guidance added through manager chat.",
        )
        self._learn_explicit_owner_preferences(message)
        return {
            "reply": answer,
            "plan_id": row["id"],
            "pending_deviation": (
                {
                    "id": pending.get("id", ""),
                    "status": pending.get("status", ""),
                }
                if pending else None
            ),
        }

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

    @staticmethod
    def _public_deviations(items: list[dict]) -> list[dict]:
        # Keep only non-sensitive routing metadata. The live CLI needs the
        # owning plan/scope to surface the right pending decision while raw
        # request payloads remain hidden until an explicit show operation.
        return [
            {
                "id": item.get("id", ""),
                "plan": item.get("plan", ""),
                "project": item.get("project", ""),
                "target": item.get("target", ""),
                "status": item.get("status", ""),
                "created": item.get("created"),
                "updated": item.get("updated"),
            }
            for item in items[:10]
        ]

    @staticmethod
    def _public_plan_contract(raw: dict) -> dict:
        value = dict(raw or {})
        # Negotiation transcripts and Codex proposals are audit data, not part
        # of the boss-facing plan contract.
        value.pop("planning", None)
        return value

    def _plan_view(self, row: dict) -> dict:
        value = {
            "id": row["id"],
            "project": row["project"],
            "goal": row["goal"],
            "status": row["status"],
            "current_milestone": row["current_milestone"],
            "plan": self._public_plan_contract(row.get("plan", {})),
            "created": row.get("created"),
            "updated": row.get("updated"),
            "result": row.get("result", ""),
            "has_internal_error": bool(row.get("error")),
        }
        workspace = (
            self.settings.workspaces_dir / row["id"]
            if self.settings.workspaces_dir else None
        )
        if workspace and workspace.is_dir():
            report = analyze_changes(self.project(row["project"]), workspace)
            value["mutation_usage"] = report.to_dict()
        value["deviations"] = self._public_deviations(
            self.store.list_deviations(plan=row["id"])
        )
        value["progress"] = plan_snapshot(
            row,
            self.store.events(row["id"]),
            remote=False,
            mode=self.mode,
            paused=self.paused,
        )
        return value

    def _remote_plan_view(self, row: dict) -> dict:
        value = {
            "id": row["id"],
            "target": row["target"],
            "goal": row["goal"],
            "status": row["status"],
            "current_milestone": row["current_milestone"],
            "plan": self._public_plan_contract(row.get("plan", {})),
            "created": row.get("created"),
            "updated": row.get("updated"),
            "result": row.get("result", ""),
            "has_internal_error": bool(row.get("error")),
        }
        value["deviations"] = self._public_deviations(
            self.remote_store.list_deviations(plan=row["id"])
        )
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
                    "awaiting_approval", "approved", "running",
                    "awaiting_deviation", "ready_for_review"
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
            context = ensure_management_contract(row.get("context", {}))
            context["owner_policy_snapshot"] = self._load_owner_policy()
            self.remote_store.update_plan(row["id"], context=context)
            row = self.remote_store.get_plan(row["id"])
            return {"plan": self._remote_plan_view(row)}
        if op == "remote_plan_list":
            return {
                "plans": self.remote_store.list_plans(
                    target=data.get("target") or None
                )
            }
        if op == "remote_plan_report":
            target_name = data.get("target", "")
            pid = data.get("id", "")
            row = (
                self.remote_store.get_plan(pid)
                if pid else self.remote_store.latest_plan(target_name)
            )
            if not row or (target_name and row["target"] != target_name):
                raise ValueError("No such remote plan")
            return self._manager_report(row, remote=True)
        if op == "remote_plan_chat":
            target_name = data.get("target", "")
            pid = data.get("id", "")
            row = (
                self.remote_store.get_plan(pid)
                if pid else self.remote_store.latest_plan(target_name)
            )
            if not row or (target_name and row["target"] != target_name):
                raise ValueError("No such remote plan")
            if row.get("status") in ("completed", "cancelled", "rejected"):
                raise ValueError("Remote plan is no longer active")
            return self._manager_chat(
                row, data.get("message", ""), remote=True
            )
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
            "remote_plan_retry_internal", "remote_plan_prepare_recovery",
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
                self.remote_inputs.pop(row["id"], None)
                return {
                    "plan": self._remote_plan_view(
                        self.remote_store.get_plan(row["id"])
                    )
                }
            if op == "remote_plan_prepare_recovery":
                if row.get("status") not in ("failed_internal", "cancelled"):
                    raise ValueError("Remote plan does not need a recovery proposal")
                successor = self._prepare_remote_recovery_plan(row)
                if not successor:
                    raise ValueError("Could not prepare remote recovery plan")
                return {"plan": self._remote_plan_view(successor)}
            with self.execution_lock:
                if not self.remote_store.cancel_plan(row["id"]):
                    raise ValueError(
                        "Remote plan cannot be cancelled in its current state"
                    )
            self.remote_inputs.pop(row["id"], None)
            return {
                "plan": self._remote_plan_view(
                    self.remote_store.get_plan(row["id"])
                )
            }

        if op == "remote_input_submit":
            did = data.get("id", "")
            key = data.get("key", "")
            supplied = data.get("value", "")
            if not isinstance(did, str) or len(did) != 12 or not did.isalnum():
                raise ValueError("Invalid remote deviation identifier")
            if not isinstance(key, str) or not key or len(key) > 64:
                raise ValueError("Invalid input key")
            if not isinstance(supplied, str) or not supplied or len(supplied) > 8192:
                raise ValueError("Human input must be 1 to 8192 characters")
            deviation = self.remote_store.get_deviation(did)
            if not deviation or deviation.get("status") != "pending":
                raise ValueError("No pending deviation for this input")
            request = deviation.get("request") or {}
            expected = request.get("input_request") or {}
            if expected.get("key") != key:
                raise ValueError("Input key does not match this deviation")
            pid = deviation["plan"]
            row = self.remote_store.get_plan(pid)
            if not row:
                raise ValueError("Input deviation refers to a missing remote plan")
            previous_inputs = dict(self.remote_inputs.get(pid, {}))
            previous_context = dict(row.get("context", {}))
            self.remote_inputs.setdefault(pid, {})[key] = supplied
            context = dict(previous_context)
            available = list(context.get("available_inputs", []))
            if key not in available:
                available.append(key)
            context["available_inputs"] = available[-20:]
            expired = [
                item for item in context.get("expired_inputs", [])
                if item != key
            ]
            context["expired_inputs"] = expired[-20:]
            input_requests = dict(context.get("input_requests", {}))
            input_requests[key] = {
                "prompt": str(expected.get("prompt", "Enter value"))[:300],
                "reason": str(expected.get("reason", "Human input is required."))[:600],
                "sensitive": expected.get("sensitive") is True,
            }
            context["input_requests"] = input_requests
            self.remote_store.update_plan(pid, context=context)
            try:
                plan = approve_remote_deviation(
                    self.settings, self.remote_store, did
                )
            except Exception:
                if previous_inputs:
                    self.remote_inputs[pid] = previous_inputs
                else:
                    self.remote_inputs.pop(pid, None)
                current = self.remote_store.get_plan(pid)
                if current and current.get("status") == "awaiting_deviation":
                    self.remote_store.update_plan(
                        pid, context=previous_context
                    )
                raise
            self.remote_store.event(pid, "remote_human_input_received", key)
            self._record_owner_decision_evidence(
                outcome="approved",
                request=request,
                scope=deviation.get("target", ""),
            )
            return {"plan": self._remote_plan_view(plan)}

        if op == "remote_deviation_list":
            self._retire_invalid_remote_deviations()
            return {
                "deviations": self._public_deviations(
                    self.remote_store.list_deviations(
                        target=data.get("target") or None
                    )
                )
            }
        if op in (
            "remote_deviation_show",
            "remote_deviation_approve",
            "remote_deviation_reject",
        ):
            self._retire_invalid_remote_deviations()
            did = data.get("id", "")
            if not isinstance(did, str) or len(did) != 12 or not did.isalnum():
                raise ValueError("Invalid remote deviation identifier")
            deviation = self.remote_store.get_deviation(did)
            if not deviation:
                raise ValueError("No such remote deviation")
            if (
                op in ("remote_deviation_show", "remote_deviation_approve")
                and deviation.get("status") != "pending"
            ):
                raise ValueError(
                    "Qwen has already resolved or retired this item; no boss decision is pending"
                )
            if op == "remote_deviation_show":
                return {
                    "deviation": deviation,
                    "brief": manager_deviation_brief(
                        self.settings, deviation, self.mode
                    ),
                }
            if op == "remote_deviation_approve":
                request = deviation.get("request") or {}
                input_request = request.get("input_request")
                if isinstance(input_request, dict):
                    return {
                        "input_request": input_request,
                        "deviation": did,
                    }
                plan = approve_remote_deviation(
                    self.settings, self.remote_store, did
                )
                self._record_owner_decision_evidence(
                    outcome="approved",
                    request=request,
                    scope=deviation.get("target", ""),
                )
                return {"plan": plan}
            request = deviation.get("request") or {}
            plan = reject_remote_deviation(
                self.settings, self.remote_store, did
            )
            self._record_owner_decision_evidence(
                outcome="rejected",
                request=request,
                scope=deviation.get("target", ""),
            )
            return {"plan": plan}

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
            context = ensure_management_contract(row.get("context", {}))
            context["owner_policy_snapshot"] = self._load_owner_policy()
            self.store.update_plan(row["id"], context=context)
            row = self.store.get_plan(row["id"])
            return {"plan": self._plan_view(row)}
        if op == "plan_list":
            project = data.get("project") or None
            return {
                "plans": [
                    self._plan_view(row)
                    for row in self.store.list_plans(project=project)
                ]
            }
        if op == "plan_report":
            project = data.get("project", "")
            row = self._plan(project, data.get("id", ""))
            return self._manager_report(row, remote=False)
        if op == "plan_chat":
            project = data.get("project", "")
            row = self._plan(project, data.get("id", ""))
            if row.get("status") in ("completed", "cancelled", "rejected"):
                raise ValueError("Plan is no longer active")
            return self._manager_chat(
                row, data.get("message", ""), remote=False
            )
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
            "plan_retry_internal", "plan_prepare_recovery",
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
            if op == "plan_prepare_recovery":
                if row.get("status") not in ("failed_internal", "cancelled"):
                    raise ValueError("Plan does not need a recovery proposal")
                successor = self._prepare_local_recovery_plan(row)
                if not successor:
                    raise ValueError("Could not prepare recovery plan")
                return {"plan": self._plan_view(successor)}
            if op == "plan_cancel":
                with self.execution_lock:
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
            with self.execution_lock:
                row = self.store.get_plan(pid)
                if not row or row["status"] != "ready_for_review":
                    raise ValueError("Plan is not ready for final owner review")
                workspace = ensure_workspace(self.settings, source, pid)
                report = analyze_changes(source, workspace)
                plan = Plan.from_dict(row["plan"], project, pid)
                violations = contract_violations(plan, report)
                if violations:
                    raise ValueError(
                        "Final diff violates approved plan: " + "; ".join(violations)
                    )
                baseline_path = self._baseline_path(pid)
                if not baseline_path.exists():
                    raise ValueError("Missing approved-plan baseline")
                baseline = json.loads(baseline_path.read_text())
                current = digest_inventory(source)
                for rel in report.changed:
                    if current.get(rel) != baseline.get(rel):
                        raise ValueError(
                            f"Managed project changed since approval: {rel}"
                        )
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
                self.store.event(
                    pid, "plan_applied_by_owner", ",".join(applied)[:2000]
                )
                return {"applied": applied, "plan": self.store.get_plan(pid)}

        if op == "deviation_list":
            self._retire_invalid_local_deviations()
            return {
                "deviations": self._public_deviations(
                    self.store.list_deviations(
                        project=data.get("project") or None
                    )
                )
            }
        if op in ("deviation_show", "deviation_approve", "deviation_reject"):
            self._retire_invalid_local_deviations()
            did = data.get("id", "")
            if not isinstance(did, str) or len(did) != 12 or not did.isalnum():
                raise ValueError("Invalid deviation identifier")
            deviation = self.store.get_deviation(did)
            if not deviation:
                raise ValueError("No such deviation")
            if (
                op in ("deviation_show", "deviation_approve")
                and deviation.get("status") != "pending"
            ):
                raise ValueError(
                    "Qwen has already resolved or retired this item; no boss decision is pending"
                )
            if op == "deviation_show":
                return {
                    "deviation": deviation,
                    "brief": manager_deviation_brief(
                        self.settings, deviation, self.mode
                    ),
                }
            source = self.project(deviation["project"])
            request = deviation.get("request") or {}
            if op == "deviation_approve":
                plan = approve_deviation(
                    self.settings, self.store, source, did
                )
                self._record_owner_decision_evidence(
                    outcome="approved",
                    request=request,
                    scope=deviation.get("project", ""),
                )
                return {"plan": plan}
            plan = reject_deviation(
                self.settings, self.store, source, did
            )
            self._record_owner_decision_evidence(
                outcome="rejected",
                request=request,
                scope=deviation.get("project", ""),
            )
            return {"plan": plan}

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

    def _prepare_remote_recovery_plan(self, failed: dict) -> dict | None:
        if failed.get("status") == "cancelled":
            if any(
                event.get("event") == "remote_plan_cancelled_by_owner"
                for event in self.remote_store.events(failed["id"])
            ):
                return None
        context = dict(failed.get("context", {}))
        existing = str(context.get("recovery_plan_id", "")).strip()
        if existing:
            row = self.remote_store.get_plan(existing)
            if row:
                return row
        target_raw = self.remote_store.get_target(failed["target"])
        if not target_raw:
            return None
        plan_raw = failed.get("plan") or {}
        milestones = list(plan_raw.get("milestones", []))
        index = int(failed.get("current_milestone", 0))
        completed = milestones[:max(0, index)]
        failed_stage = milestones[index] if 0 <= index < len(milestones) else ""
        postmortem = context.get("hard_replan_postmortem") or {
            "failure": failed.get("result", ""),
            "recent_attempts": list(context.get("history", []))[-6:],
        }
        recovery_goal = (
            "Create a major successor recovery plan for an existing failed plan. "
            "Do not execute anything while planning. The replacement must be "
            "materially different where the previous strategy failed and must "
            "preserve already-correct work.\n\n"
            f"Original goal:\n{failed.get('goal', '')}\n\n"
            f"Failed plan ID: {failed.get('id', '')}\n"
            f"Completed milestones: {json.dumps(completed)}\n"
            f"Failed milestone: {failed_stage}\n"
            f"Failure/postmortem: {json.dumps(postmortem)[:9000]}\n\n"
            "Re-inspect the target and jointly determine what went wrong. "
            "The resulting plan will require fresh owner approval before execution."
        )
        self._planning_start("remote_plan", failed["target"])
        try:
            row = create_remote_plan(
                self.settings,
                self.remote_store,
                RemoteTarget.from_dict(target_raw),
                recovery_goal,
                self.mode,
                progress=lambda stage_index, stages=None: self._planning_update(
                    "remote_plan", failed["target"], stage_index, stages
                ),
            )
        except Exception as exc:
            self._planning_finish(
                "remote_plan", failed["target"], "failed", str(exc)
            )
            raise
        self._planning_finish("remote_plan", failed["target"], "finished")
        successor_context = ensure_management_contract(row.get("context", {}))
        failure_text = (
            str(postmortem.get("failure", ""))
            if isinstance(postmortem, dict)
            else str(postmortem)
        )
        delta = recovery_delta(
            failed_stage=failed_stage,
            completed=completed,
            old_remaining=milestones[max(0, index):],
            new_milestones=list((row.get("plan") or {}).get("milestones", [])),
            failure=failure_text,
        )
        successor_context.update({
            "recovery_of_plan_id": failed["id"],
            "recovery_postmortem": postmortem,
            "completed_prior_milestones": completed,
            "recovery_delta": delta,
            "owner_policy_snapshot": self._load_owner_policy(),
            "owner_guidance": list(context.get("owner_guidance") or [])[-12:],
        })
        self.remote_store.update_plan(
            row["id"],
            context=successor_context,
        )
        context["recovery_plan_id"] = row["id"]
        context["recovery_plan_required"] = False
        context["recovery_delta"] = delta
        self.remote_store.update_plan(
            failed["id"],
            context=context,
        )
        self.remote_store.event(
            failed["id"],
            "remote_recovery_plan_proposed",
            row["id"],
        )
        return self.remote_store.get_plan(row["id"])

    def _prepare_local_recovery_plan(self, failed: dict) -> dict | None:
        if failed.get("status") == "cancelled":
            if any(
                event.get("event") == "plan_cancelled_by_owner"
                for event in self.store.events(failed["id"])
            ):
                return None
        context = dict(failed.get("context", {}))
        existing = str(context.get("recovery_plan_id", "")).strip()
        if existing:
            row = self.store.get_plan(existing)
            if row:
                return row
        project = failed["project"]
        source = self.project(project)
        plan_raw = failed.get("plan") or {}
        milestones = list(plan_raw.get("milestones", []))
        index = int(failed.get("current_milestone", 0))
        completed = milestones[:max(0, index)]
        failed_stage = milestones[index] if 0 <= index < len(milestones) else ""
        postmortem = {
            "failure": context.get("last_internal_failure") or failed.get("result", ""),
            "recent_attempts": list(context.get("history", []))[-6:],
        }
        recovery_goal = (
            "Create a major successor recovery plan for an existing failed project "
            "plan. Do not execute anything while planning. Diagnose the previous "
            "failure with Qwen and propose a materially new route for owner approval.\n\n"
            f"Original goal:\n{failed.get('goal', '')}\n\n"
            f"Failed plan ID: {failed.get('id', '')}\n"
            f"Completed milestones: {json.dumps(completed)}\n"
            f"Failed milestone: {failed_stage}\n"
            f"Failure/postmortem: {json.dumps(postmortem)[:9000]}"
        )
        self._planning_start("project_plan", project)
        try:
            row = create_negotiated_plan(
                self.settings,
                self.store,
                source,
                project,
                recovery_goal,
                self.mode,
                progress=lambda stage_index, stages=None: self._planning_update(
                    "project_plan", project, stage_index, stages
                ),
            )
        except Exception as exc:
            self._planning_finish(
                "project_plan", project, "failed", str(exc)
            )
            raise
        self._planning_finish("project_plan", project, "finished")
        successor_context = ensure_management_contract(row.get("context", {}))
        failure_text = (
            str(postmortem.get("failure", ""))
            if isinstance(postmortem, dict)
            else str(postmortem)
        )
        delta = recovery_delta(
            failed_stage=failed_stage,
            completed=completed,
            old_remaining=milestones[max(0, index):],
            new_milestones=list((row.get("plan") or {}).get("milestones", [])),
            failure=failure_text,
        )
        successor_context.update({
            "recovery_of_plan_id": failed["id"],
            "recovery_postmortem": postmortem,
            "completed_prior_milestones": completed,
            "recovery_delta": delta,
            "owner_policy_snapshot": self._load_owner_policy(),
            "owner_guidance": list(context.get("owner_guidance") or [])[-12:],
        })
        self.store.update_plan(row["id"], context=successor_context)
        context["recovery_plan_id"] = row["id"]
        context["recovery_plan_required"] = False
        context["recovery_delta"] = delta
        self.store.update_plan(failed["id"], context=context)
        self.store.event(failed["id"], "recovery_plan_proposed", row["id"])
        return self.store.get_plan(row["id"])

    def remote_plan_worker(self):
        while not self.shutdown.is_set():
            try:
                if self.paused or self.mode != "maximum":
                    self.shutdown.wait(0.75)
                    continue
                row = self.remote_store.next_plan()
                if not row:
                    failed = next((
                        item for item in self.remote_store.list_plans(limit=30)
                        if item.get("status") == "failed_internal"
                        and not str(
                            (item.get("context") or {}).get("recovery_plan_id", "")
                        ).strip()
                        and float(
                            (item.get("context") or {}).get(
                                "recovery_plan_retry_after", 0
                            ) or 0
                        ) <= time.time()
                    ), None)
                    if failed:
                        try:
                            self._prepare_remote_recovery_plan(failed)
                        except Exception as exc:
                            failed_context = dict(failed.get("context", {}))
                            attempts = int(
                                failed_context.get("recovery_plan_attempts", 0)
                            ) + 1
                            failed_context["recovery_plan_attempts"] = attempts
                            failed_context["recovery_plan_retry_after"] = (
                                time.time() + min(300, 30 * attempts)
                            )
                            self.remote_store.update_plan(
                                failed["id"], context=failed_context
                            )
                            self.remote_store.event(
                                failed["id"],
                                "remote_recovery_plan_preparation_failed",
                                str(exc)[:1200],
                            )
                    self.shutdown.wait(0.75)
                    continue
                try:
                    with self.execution_lock:
                        run_remote_plan_step(
                            self.settings,
                            self.remote_store,
                            row,
                            self.mode,
                            inputs=dict(self.remote_inputs.get(row["id"], {})),
                        )
                    current = self.remote_store.get_plan(row["id"])
                    if current and current.get("status") == "failed_internal":
                        self.remote_inputs.pop(row["id"], None)
                        self._prepare_remote_recovery_plan(current)
                    elif current and current.get("status") in (
                        "completed", "cancelled", "rejected"
                    ):
                        self.remote_inputs.pop(row["id"], None)
                except Exception as exc:
                    current = self.remote_store.get_plan(row["id"])
                    if current and current["status"] == "running":
                        failure = classify_remote_error(exc)
                        if failure["type"] == "human_input_missing":
                            context = dict(current.get("context", {}))
                            available = [
                                str(x) for x in context.get("available_inputs", [])
                                if isinstance(x, str)
                            ]
                            live = set(self.remote_inputs.get(row["id"], {}))
                            missing = next((key for key in available if key not in live), "")
                            metadata = dict(context.get("input_requests", {})).get(missing, {})
                            if missing:
                                refresh_request = {
                                    "type": "human_input_refresh",
                                    "boss_request": (
                                        "Allow me to ask you to re-enter one "
                                        "ephemeral input that was lost after restart."
                                    ),
                                    "risk": "low",
                                    "input_request": {
                                        "key": missing,
                                        "prompt": str(metadata.get("prompt", f"Re-enter {missing}"))[:300],
                                        "reason": str(metadata.get(
                                            "reason",
                                            "The coordinator restarted, so the value is no longer in memory."
                                        ))[:600],
                                        "sensitive": metadata.get("sensitive") is True,
                                    },
                                    "suggested_action": (
                                        "Re-enter only this value; no new authority is requested."
                                    ),
                                }
                                decision_key = owner_decision_key(refresh_request)
                                rejected = set(context.get("rejected_owner_decisions", []))
                                if decision_key and decision_key in rejected:
                                    context["next_guidance"] = (
                                        "The owner already rejected re-entering this "
                                        "human input. Do not ask again. Replan without "
                                        "that value or stop cleanly."
                                    )
                                    context["available_inputs"] = [
                                        item for item in context.get("available_inputs", [])
                                        if item != missing
                                    ]
                                    context["expired_inputs"] = [
                                        item for item in context.get("expired_inputs", [])
                                        if item != missing
                                    ]
                                    self.remote_store.update_plan(
                                        row["id"],
                                        status="approved",
                                        attempts=0,
                                        context=context,
                                    )
                                    self.remote_store.event(
                                        row["id"],
                                        "remote_rejected_owner_decision_not_reasked",
                                        decision_key,
                                    )
                                else:
                                    self.remote_store.create_deviation(
                                        row["id"],
                                        row["target"],
                                        "Qwen needs one previously supplied ephemeral input again",
                                        refresh_request,
                                    )
                            else:
                                self.remote_store.update_plan(
                                    row["id"],
                                    status="cancelled",
                                    result=(
                                        "An ephemeral input was missing but its key "
                                        "could not be identified safely."
                                    ),
                                    context=context,
                                )
                        elif failure["type"] in (
                            "action_format",
                            "codex_format",
                            "timeout",
                            "ssh_transport",
                            "remote_command",
                            "policy_scope",
                            "codex_worker",
                        ):
                            context = dict(current.get("context", {}))
                            recoveries = int(
                                context.get("worker_internal_recoveries", 0)
                            ) + 1
                            context["worker_internal_recoveries"] = recoveries
                            context["next_guidance"] = (
                                "Qwen manager correction: this is an internal technical "
                                "or policy-routing problem, not yet an owner decision. "
                                "Diagnose it with Codex, repair the approach inside existing "
                                "authority, and retry. "
                                + str(failure.get("error", ""))[:1800]
                            )
                            if recoveries <= 3:
                                self.remote_store.update_plan(
                                    row["id"],
                                    status="approved",
                                    attempts=0,
                                    context=context,
                                )
                                self.remote_store.event(
                                    row["id"],
                                    "remote_internal_format_recovery",
                                    f"{recoveries}: "
                                    + str(failure.get("error", ""))[:1100],
                                )
                            else:
                                self.remote_store.update_plan(
                                    row["id"],
                                    status="cancelled",
                                    result=(
                                        "Qwen and Codex repeatedly hit the same internal "
                                        "format failure and stopped without requiring "
                                        "an owner decision."
                                    ),
                                    context=context,
                                )
                                self.remote_store.event(
                                    row["id"],
                                    "remote_internal_recovery_exhausted",
                                    str(failure.get("error", ""))[:1200],
                                )
                        else:
                            if failure["type"] == "credentials":
                                request = {
                                    "type": "human_access",
                                    "reason": str(
                                        failure.get("error", "")
                                    )[:1800],
                                    "boss_request": (
                                        "Restore or authorize SSH key access to "
                                        f"{row['target']} so I can continue."
                                    ),
                                    "risk": "medium",
                                    "suggested_action": (
                                        "Fix only the existing SSH authentication "
                                        "for this target; do not broaden remote scope."
                                    ),
                                }
                                self.remote_store.create_deviation(
                                    row["id"],
                                    row["target"],
                                    "Qwen needs restored SSH access to the target",
                                    request,
                                )
                            else:
                                context = dict(current.get("context", {}))
                                context["next_guidance"] = (
                                    "Qwen manager correction: an unclassified internal "
                                    "remote failure occurred. Diagnose it with Codex "
                                    "before involving the owner. "
                                    + str(failure.get("error", ""))[:1800]
                                )
                                self.remote_store.update_plan(
                                    row["id"],
                                    status="cancelled",
                                    context=context,
                                    result=(
                                        "The remote worker stopped on an unclassified "
                                        "internal failure without creating an owner "
                                        "decision."
                                    ),
                                )
                                self.remote_store.event(
                                    row["id"],
                                    "remote_unclassified_internal_failure",
                                    str(failure.get("error", ""))[:1200],
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
                    failed = next((
                        item for item in self.store.list_plans(limit=30)
                        if item.get("status") == "failed_internal"
                        and not str(
                            (item.get("context") or {}).get("recovery_plan_id", "")
                        ).strip()
                        and float(
                            (item.get("context") or {}).get(
                                "recovery_plan_retry_after", 0
                            ) or 0
                        ) <= time.time()
                    ), None)
                    if failed:
                        try:
                            self._prepare_local_recovery_plan(failed)
                        except Exception as exc:
                            failed_context = dict(failed.get("context", {}))
                            attempts = int(
                                failed_context.get("recovery_plan_attempts", 0)
                            ) + 1
                            failed_context["recovery_plan_attempts"] = attempts
                            failed_context["recovery_plan_retry_after"] = (
                                time.time() + min(300, 30 * attempts)
                            )
                            self.store.update_plan(
                                failed["id"], context=failed_context
                            )
                            self.store.event(
                                failed["id"],
                                "recovery_plan_preparation_failed",
                                str(exc)[:1200],
                            )
                    self.shutdown.wait(0.75)
                    continue
                try:
                    source = self.project(row["project"])
                    with self.execution_lock:
                        run_plan_step(
                            self.settings, self.store, source, row, self.mode
                        )
                    current = self.store.get_plan(row["id"])
                    if current and current.get("status") == "failed_internal":
                        self._prepare_local_recovery_plan(current)
                except Exception as exc:
                    current = self.store.get_plan(row["id"])
                    if current and current["status"] == "running":
                        failure = classify_project_error(exc)
                        if failure["type"] in (
                            "codex_format",
                            "codex_worker",
                            "timeout",
                            "workspace_tool",
                            "policy_scope",
                        ):
                            context = dict(current.get("context", {}))
                            recoveries = int(
                                context.get("worker_internal_recoveries", 0)
                            ) + 1
                            context["worker_internal_recoveries"] = recoveries
                            context["next_guidance"] = (
                                "Qwen manager correction: this is an internal technical "
                                "failure, not an owner decision. Diagnose it with Codex "
                                "and retry or choose a supported approach. "
                                + str(failure.get("error", ""))[:1800]
                            )
                            if recoveries <= 3:
                                self.store.update_plan(
                                    row["id"],
                                    status="approved",
                                    attempts=0,
                                    context=context,
                                )
                                self.store.event(
                                    row["id"],
                                    "project_internal_failure_recovery",
                                    f"{recoveries}: "
                                    + str(failure.get("error", ""))[:1100],
                                )
                            else:
                                self.store.update_plan(
                                    row["id"],
                                    status="cancelled",
                                    result=(
                                        "Qwen and Codex repeatedly hit the same internal "
                                        "failure and stopped without requiring an owner "
                                        "decision."
                                    ),
                                    context=context,
                                )
                                self.store.event(
                                    row["id"],
                                    "project_internal_recovery_exhausted",
                                    str(failure.get("error", ""))[:1200],
                                )
                        else:
                            context = dict(current.get("context", {}))
                            context["next_guidance"] = (
                                "Qwen manager correction: an unclassified internal "
                                "project failure occurred. Diagnose it with Codex "
                                "before involving the owner. "
                                + str(failure.get("error", ""))[:1800]
                            )
                            self.store.update_plan(
                                row["id"],
                                status="cancelled",
                                context=context,
                                result=(
                                    "The project worker stopped on an unclassified "
                                    "internal failure without creating an owner decision."
                                ),
                            )
                            self.store.event(
                                row["id"],
                                "project_unclassified_internal_failure",
                                str(failure.get("error", ""))[:1200],
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
