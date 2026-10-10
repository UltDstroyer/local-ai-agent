"""Codex-led orchestration for owner-approved SSH targets."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .agent import ask
from .codex_bridge import bridge_call
from .remote import (
    RemoteBudget,
    RemoteContractError,
    RemotePlan,
    RemoteTarget,
    action_deviation,
    validate_remote_action,
)
from .ssh_bridge import SSHBridgeError, ssh_bridge_call


class RemoteOrchestrationError(RuntimeError):
    pass


def _json_object(text: str) -> dict:
    if not isinstance(text, str):
        raise RemoteContractError("model response was not text")
    value = text.strip()
    if value.startswith("~~~"):
        lines = value.splitlines()
        if lines and lines[0].startswith("~~~"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "~~~":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start, end = value.find("{"), value.rfind("}")
    if start >= 0 and end > start:
        parsed = json.loads(value[start:end + 1])
        if isinstance(parsed, dict):
            return parsed
    raise RemoteContractError("model did not return a JSON object")


def _remote_context_dir(settings, pid: str) -> Path:
    if settings.workspaces_dir is None:
        raise RemoteOrchestrationError("workspace directory is unavailable")
    path = settings.workspaces_dir / f"remote-{pid}"
    path.mkdir(mode=0o2770, parents=True, exist_ok=True)
    path.chmod(0o2770)
    return path


def _save_inspection(settings, pid: str, target: dict, inspection: dict) -> Path:
    root = _remote_context_dir(settings, pid)
    text = (
        "REMOTE TARGET\n"
        + json.dumps(target, indent=2)
        + "\n\nLATEST READ-ONLY INSPECTION\n"
        + inspection.get("output", "")[:70_000]
    )
    file = root / "remote-inspection.txt"
    file.write_text(text, encoding="utf-8")
    file.chmod(0o660)
    return root


def inspect_target(settings, target: RemoteTarget) -> dict:
    if settings.ssh_bridge_socket is None:
        raise RemoteOrchestrationError("SSH bridge is not configured")
    if not settings.ssh_bridge_socket.exists():
        raise RemoteOrchestrationError(
            "SSH bridge is not running in the operator user session"
        )
    result = ssh_bridge_call(
        settings.ssh_bridge_socket,
        {"op": "inspect", "target": target.to_dict()},
        timeout=150,
    )
    if not result.get("ok"):
        raise RemoteOrchestrationError(
            "SSH inspection failed: " + result.get("output", "")[-3000:]
        )
    return result


def _planner_prompt(goal: str, target: RemoteTarget, inspection: str) -> str:
    return f"""You are the primary systems engineer planning work on an SSH-only remote
machine. You cannot use SSH directly. The ctx-agent SSH broker executes only
typed actions after owner approval.

Owner goal:
{goal}

Target:
{json.dumps(target.to_dict())}

Read-only inspection:
{inspection[:30000]}

Create the smallest practical plan. Do not perform actions. Return JSON only:

{{
  "goal": "...",
  "milestones": ["..."],
  "verification": ["..."],
  "risks": ["..."],
  "assumptions": ["..."],
  "remote_budget": {{
    "allowed_read_paths": ["/absolute/path/or/pattern"],
    "allowed_write_paths": ["/absolute/path/or/pattern"],
    "allowed_services": ["service"],
    "allowed_packages": ["package"],
    "allowed_domains": ["example.com"],
    "allow_sudo": false,
    "allow_package_install": false,
    "allow_network": false,
    "max_actions": 30,
    "max_file_writes": 8,
    "max_attempts_per_milestone": 3,
    "backup_required": true
  }}
}}

Supported execution actions later are read_file, write_file, ensure_dir,
package_install, service, and check. Service mutations are limited to start,
restart, enable, and enable_now. Checks include systemctl_is_active, http_get,
http_head, nginx_test, journal_tail, listen_sockets, command_exists, and
path_exists.

Keep permissions narrow. Sudo, package installation, public network access,
and write paths must be visible in the contract whenever required. Do not
include SSH configuration changes, disk partitioning, firewall resets, user
credential changes, or secret values in the plan."""


def _admin_reconcile_prompt(
    goal: str, candidate: dict, target: RemoteTarget, inspection: str
) -> str:
    return f"""You are the local ctx-agent administrator. Codex is the primary systems
engineer. Reconcile its proposal into the narrowest viable remote mutation
contract. Do not execute anything. Return the complete remote plan JSON only.

Owner goal:
{goal}

Target:
{json.dumps(target.to_dict())}

Codex proposal:
{json.dumps(candidate)[:16000]}

Inspection:
{inspection[:20000]}

Preserve the technical plan when sound, but remove speculative paths, packages,
services, domains, and capabilities. Backups must remain required. Do not
authorize SSH daemon changes, credential changes, firewall resets, disk
partitioning, unrelated cleanup, or destructive work. The owner will review
every permission in the returned budget."""


def _agreement_prompt(plan: RemotePlan) -> str:
    return f"""The local administrator narrowed our SSH project contract. Do not execute
anything. Review whether the plan remains technically viable and minimal.

{json.dumps(plan.to_dict(), indent=2)}

Return JSON only:
{{
  "agreement": true,
  "concerns": ["..."]
}}

Do not request broader authority merely for convenience."""


def create_remote_plan(
    settings,
    remote_store,
    target: RemoteTarget,
    goal: str,
    mode: str,
) -> dict:
    if settings.codex_bridge_socket is None:
        raise RemoteOrchestrationError("Codex bridge is not configured")
    if not settings.codex_bridge_socket.exists():
        raise RemoteOrchestrationError("Codex bridge is not running")
    health = bridge_call(settings.codex_bridge_socket, {"op": "health"}, timeout=30)
    if not health.get("available") or not health.get("authenticated"):
        raise RemoteOrchestrationError(
            "Codex is not authenticated. Run ctx agent codex login first."
        )

    inspection = inspect_target(settings, target)
    pid = uuid.uuid4().hex[:12]
    context_dir = _save_inspection(
        settings, pid, target.to_dict(), inspection
    )

    codex = bridge_call(
        settings.codex_bridge_socket,
        {
            "op": "consult",
            "cwd": str(context_dir),
            "prompt": _planner_prompt(
                goal, target, inspection.get("output", "")
            ),
            "timeout": 900,
        },
        timeout=930,
    )
    if not codex.get("ok"):
        raise RemoteOrchestrationError(
            "Codex remote planning failed: "
            + codex.get("events_tail", "")[-3000:]
        )
    candidate = _json_object(codex.get("message", ""))
    if isinstance(candidate.get("plan"), dict):
        candidate = candidate["plan"]

    admin_message = ask(
        settings,
        [
            {
                "role": "system",
                "content": (
                    "Return strict JSON only. You are the remote-change "
                    "administrative authority, not the executor."
                ),
            },
            {
                "role": "user",
                "content": _admin_reconcile_prompt(
                    goal,
                    candidate,
                    target,
                    inspection.get("output", ""),
                ),
            },
        ],
        "balanced" if mode == "quiet" else mode,
        tools=[],
    )
    admin = _json_object(str(admin_message.get("content", "")))
    plan = RemotePlan.from_dict(admin, target.name, pid)

    agreement = bridge_call(
        settings.codex_bridge_socket,
        {
            "op": "consult",
            "cwd": str(context_dir),
            "thread_id": codex.get("thread_id", ""),
            "prompt": _agreement_prompt(plan),
            "timeout": 600,
        },
        timeout=630,
    )
    if not agreement.get("ok"):
        raise RemoteOrchestrationError("Codex could not review remote contract")
    agreed = _json_object(agreement.get("message", ""))
    if agreed.get("agreement") is not True:
        concerns = agreed.get("concerns", [])
        raise RemoteOrchestrationError(
            "Codex and administrator did not agree on a viable remote plan: "
            + "; ".join(str(x) for x in concerns)[:3000]
        )

    stored = plan.to_dict()
    stored["planning"] = {
        "codex_proposal": candidate,
        "codex_agreement": agreed,
        "inspection_excerpt": inspection.get("output", "")[:12000],
    }
    remote_store.create_plan(target.name, goal, stored)
    remote_store.update_plan(
        plan.id,
        codex_thread=codex.get("thread_id", ""),
        context={
            "inspection": inspection.get("output", "")[:18000],
            "action_count": 0,
            "file_write_count": 0,
            "history": [],
        },
    )
    return remote_store.get_plan(plan.id)


def _worker_prompt(plan: RemotePlan, row: dict, inspection: str) -> str:
    index = int(row["current_milestone"])
    return f"""You are the primary systems engineer for an owner-approved SSH project.
You cannot invoke SSH or a shell directly. Propose only typed actions for the
ctx-agent SSH broker.

Goal:
{plan.goal}

Milestone {index + 1}/{len(plan.milestones)}:
{plan.milestones[index]}

Approved remote budget:
{json.dumps(plan.budget.to_dict())}

Fresh remote inspection:
{inspection[:22000]}

Persistent admin context:
{json.dumps(row.get("context", {}))[-10000:]}

Return JSON only:
{{
  "actions": [
    {{"op": "read_file", "path": "/approved/path"}},
    {{"op": "write_file", "path": "/approved/path", "content": "...", "mode": "0644"}},
    {{"op": "ensure_dir", "path": "/approved/path", "mode": "0755"}},
    {{"op": "package_install", "packages": ["approved-package"]}},
    {{"op": "service", "service": "approved.service", "action": "restart"}},
    {{"op": "check", "check": "systemctl_is_active", "service": "approved.service"}}
  ],
  "summary": "what these actions accomplish",
  "expected_result": "what should be true afterward"
}}

Use only the actions actually needed for this milestone. Do not include an
action shape that is not supported. Never include secret tokens or passwords.
If the approved contract is insufficient, propose the minimum required action
anyway so ctx-agent can turn it into an owner-visible deviation request."""


def _admin_action_review(
    settings, plan: RemotePlan, milestone: str, actions: list[dict],
    inspection: str, mode: str,
) -> dict:
    prompt = f"""You are the local remote-change administrator supervising Codex.
Review the proposed typed SSH actions before execution.

Goal: {plan.goal}
Milestone: {milestone}
Approved budget: {json.dumps(plan.budget.to_dict())}
Actions: {json.dumps(actions)[:18000]}
Inspection: {inspection[:14000]}

Return JSON only:
{{
  "approve": true,
  "guidance": "short correction if not approved",
  "reason": "why"
}}

Approve only actions that are necessary, on-task, and minimal. Do not grant
new authority. Scope enforcement happens separately."""
    try:
        response = ask(
            settings,
            [
                {"role": "system", "content": "Return strict JSON only."},
                {"role": "user", "content": prompt},
            ],
            "balanced" if mode == "quiet" else mode,
            tools=[],
        )
        value = _json_object(str(response.get("content", "")))
        return {
            "approve": value.get("approve") is True,
            "guidance": str(value.get("guidance", ""))[:3000],
            "reason": str(value.get("reason", ""))[:3000],
        }
    except Exception:
        return {
            "approve": False,
            "guidance": "Re-propose a smaller set of necessary typed actions.",
            "reason": "administrator review was inconclusive",
        }


def _completion_review(
    settings, plan: RemotePlan, milestone: str, results: list[dict],
    inspection: str, mode: str,
) -> dict:
    prompt = f"""You are the local administrator. Decide whether the remote milestone is
complete after the broker executed the approved typed actions.

Goal: {plan.goal}
Milestone: {milestone}
Action results: {json.dumps(results)[-18000:]}
Fresh inspection: {inspection[:16000]}
Verification criteria: {json.dumps(plan.verification)}

Return JSON only:
{{
  "state": "complete" | "continue" | "blocked",
  "guidance": "what Codex should do next",
  "context_note": "durable short summary",
  "reason": "why"
}}"""
    try:
        response = ask(
            settings,
            [
                {"role": "system", "content": "Return strict JSON only."},
                {"role": "user", "content": prompt},
            ],
            "balanced" if mode == "quiet" else mode,
            tools=[],
        )
        value = _json_object(str(response.get("content", "")))
        if value.get("state") not in ("complete", "continue", "blocked"):
            raise ValueError("bad completion state")
        return value
    except Exception:
        return {
            "state": "continue",
            "guidance": "Inspect the latest results and verify the milestone again.",
            "context_note": "",
            "reason": "administrator completion review was inconclusive",
        }


def _append_history(context: dict, entry: dict) -> dict:
    value = dict(context) if isinstance(context, dict) else {}
    history = list(value.get("history", []))
    history.append(entry)
    value["history"] = history[-30:]
    return value


def _amend_budget_for_action(plan: RemotePlan, action: dict) -> RemotePlan:
    raw = plan.to_dict()
    budget = raw["remote_budget"]
    op = action.get("op")
    if op == "read_file":
        path = action.get("path")
        if path and path not in budget["allowed_read_paths"]:
            budget["allowed_read_paths"].append(path)
    elif op in ("write_file", "ensure_dir"):
        path = action.get("path")
        if path and path not in budget["allowed_write_paths"]:
            budget["allowed_write_paths"].append(path)
        budget["allow_sudo"] = True
        if op == "write_file":
            budget["max_file_writes"] = max(
                budget["max_file_writes"], 1
            )
    elif op == "package_install":
        for package in action.get("packages", []):
            if package not in budget["allowed_packages"]:
                budget["allowed_packages"].append(package)
        budget["allow_sudo"] = True
        budget["allow_package_install"] = True
        budget["allow_network"] = True
    elif op == "service":
        service = action.get("service")
        if service and service not in budget["allowed_services"]:
            budget["allowed_services"].append(service)
        if action.get("action") != "status":
            budget["allow_sudo"] = True
    elif op == "check":
        check = action.get("check")
        if check in ("systemctl_is_active", "journal_tail"):
            service = action.get("service")
            if service and service not in budget["allowed_services"]:
                budget["allowed_services"].append(service)
        elif check == "path_exists":
            path = action.get("path")
            if path and path not in budget["allowed_read_paths"]:
                budget["allowed_read_paths"].append(path)
        elif check in ("http_get", "http_head"):
            url = action.get("url", "")
            host = urlparse(url).hostname
            if host and host not in ("localhost",) and host not in budget["allowed_domains"]:
                budget["allowed_domains"].append(host)
                budget["allow_network"] = True
    return RemotePlan.from_dict(raw, plan.target, plan.id)


def run_remote_plan_step(settings, remote_store, row: dict, mode: str):
    target_raw = remote_store.get_target(row["target"])
    if not target_raw:
        raise RemoteOrchestrationError("remote target disappeared")
    target = RemoteTarget.from_dict(target_raw)
    plan = RemotePlan.from_dict(row["plan"], target.name, row["id"])
    index = int(row["current_milestone"])

    if index >= len(plan.milestones):
        inspection = inspect_target(settings, target)
        context_dir = _save_inspection(
            settings, plan.id, target.to_dict(), inspection
        )
        review = bridge_call(
            settings.codex_bridge_socket,
            {
                "op": "consult",
                "cwd": str(context_dir),
                "thread_id": row.get("codex_thread", ""),
                "prompt": (
                    "Perform a final read-only review of this remote project. "
                    "Do not propose or execute new mutations. Compare the goal, "
                    "verification criteria, and latest inspection. Return JSON only: "
                    "{\"ready\": true, \"problems\": [\"...\"], "
                    "\"summary\": \"...\"}.\nPLAN:\n"
                    + json.dumps(plan.to_dict())
                    + "\nINSPECTION:\n"
                    + inspection.get("output", "")[:26000]
                ),
                "timeout": 700,
            },
            timeout=730,
        )
        if not review.get("ok"):
            raise RemoteOrchestrationError("final Codex remote review failed")
        verdict = _json_object(review.get("message", ""))
        if verdict.get("ready") is not True:
            problems = verdict.get("problems", [])
            remote_store.create_deviation(
                plan.id,
                plan.target,
                "final remote verification found unresolved problems",
                {
                    "type": "final_verification",
                    "problems": problems if isinstance(problems, list) else [],
                    "suggested_action": (
                        "Review the reported verification problems and approve "
                        "a narrow retry if more remote work is appropriate."
                    ),
                },
            )
            return
        remote_store.update_plan(
            plan.id,
            status="completed",
            result=(
                "All approved remote milestones completed and final Codex "
                "verification passed.\n\n"
                + str(verdict.get("summary", ""))[:12000]
            ),
        )
        remote_store.event(plan.id, "remote_plan_completed")
        return

    inspection = inspect_target(settings, target)
    context_dir = _save_inspection(
        settings, plan.id, target.to_dict(), inspection
    )
    codex = bridge_call(
        settings.codex_bridge_socket,
        {
            "op": "consult",
            "cwd": str(context_dir),
            "thread_id": row.get("codex_thread", ""),
            "prompt": _worker_prompt(
                plan, row, inspection.get("output", "")
            ),
            "timeout": 900,
        },
        timeout=930,
    )
    if not codex.get("ok"):
        raise RemoteOrchestrationError(
            "Codex remote worker failed: "
            + codex.get("events_tail", "")[-3000:]
        )
    if codex.get("thread_id"):
        remote_store.update_plan(plan.id, codex_thread=codex["thread_id"])
    proposal = _json_object(codex.get("message", ""))
    actions = proposal.get("actions")
    if not isinstance(actions, list) or not actions or len(actions) > 20:
        raise RemoteOrchestrationError("Codex proposed invalid remote actions")

    context = dict(row.get("context", {}))
    action_count = int(context.get("action_count", 0))
    file_write_count = int(context.get("file_write_count", 0))
    if action_count + len(actions) > plan.budget.max_actions:
        remote_store.create_deviation(
            plan.id,
            plan.target,
            "remote action budget would be exceeded",
            {
                "type": "remote_budget",
                "requested_max_actions": action_count + len(actions),
                "current_budget": plan.budget.to_dict(),
            },
        )
        return

    validated = []
    for action in actions:
        try:
            checked = validate_remote_action(action, plan.budget)
        except RemoteContractError as exc:
            remote_store.create_deviation(
                plan.id,
                plan.target,
                str(exc),
                action_deviation(plan, action, str(exc)),
            )
            return
        if checked["op"] == "write_file":
            file_write_count += 1
        validated.append(checked)

    if file_write_count > plan.budget.max_file_writes:
        remote_store.create_deviation(
            plan.id,
            plan.target,
            "remote file-write budget would be exceeded",
            {
                "type": "remote_budget",
                "requested_max_file_writes": file_write_count,
                "current_budget": plan.budget.to_dict(),
            },
        )
        return

    admin = _admin_action_review(
        settings,
        plan,
        plan.milestones[index],
        validated,
        inspection.get("output", ""),
        mode,
    )
    if not admin["approve"]:
        attempts = int(row["attempts"]) + 1
        context["next_guidance"] = admin["guidance"]
        if attempts >= plan.budget.max_attempts_per_milestone:
            remote_store.create_deviation(
                plan.id,
                plan.target,
                "Codex and administrator could not agree on safe remote actions",
                {
                    "type": "stalled",
                    "milestone": plan.milestones[index],
                    "reason": admin["reason"],
                    "suggested_action": admin["guidance"],
                },
            )
        else:
            remote_store.update_plan(
                plan.id,
                status="approved",
                attempts=attempts,
                context=context,
            )
        return

    results = []
    mutations = list(row.get("mutations", []))
    for action in validated:
        result = ssh_bridge_call(
            settings.ssh_bridge_socket,
            {
                "op": "action",
                "target": target.to_dict(),
                "budget": plan.budget.to_dict(),
                "action": action,
                "plan": plan.id,
                "milestone": index,
            },
            timeout=1900,
        )
        results.append({
            "action": action,
            "ok": result.get("ok"),
            "returncode": result.get("returncode"),
            "output": result.get("output", "")[-6000:],
        })
        if result.get("mutation"):
            record = dict(result["mutation"])
            record["milestone"] = index
            mutations.append(record)
        if not result.get("ok"):
            break

    context["action_count"] = action_count + len(results)
    context["file_write_count"] = sum(
        1 for item in mutations if item.get("op") == "write_file"
    )
    context = _append_history(
        context,
        {
            "milestone": index,
            "codex_summary": str(proposal.get("summary", ""))[:2500],
            "actions": validated,
            "results": results,
        },
    )
    remote_store.update_plan(
        plan.id,
        mutations=mutations,
        context=context,
    )

    if not results or not all(item["ok"] for item in results):
        attempts = int(row["attempts"]) + 1
        context["next_guidance"] = (
            "A typed SSH action failed. Diagnose the returned output and "
            "propose the smallest in-contract correction."
        )
        if attempts >= plan.budget.max_attempts_per_milestone:
            remote_store.create_deviation(
                plan.id,
                plan.target,
                "remote milestone repeatedly failed after partial execution",
                {
                    "type": "stalled",
                    "milestone": plan.milestones[index],
                    "results": results[-3:],
                    "suggested_action": context["next_guidance"],
                },
            )
        else:
            remote_store.update_plan(
                plan.id,
                status="approved",
                attempts=attempts,
                context=context,
            )
        return

    fresh = inspect_target(settings, target)
    review = _completion_review(
        settings,
        plan,
        plan.milestones[index],
        results,
        fresh.get("output", ""),
        mode,
    )
    context["next_guidance"] = str(review.get("guidance", ""))[:3000]
    context = _append_history(
        context,
        {
            "milestone": index,
            "admin": str(review.get("context_note", ""))[:2500],
            "state": review["state"],
        },
    )

    if review["state"] == "complete":
        remote_store.update_plan(
            plan.id,
            status="approved",
            current_milestone=index + 1,
            attempts=0,
            context=context,
            mutations=mutations,
        )
        remote_store.event(
            plan.id,
            "remote_milestone_completed",
            f"{index + 1}: {plan.milestones[index]}",
        )
        return

    attempts = int(row["attempts"]) + 1
    if (
        review["state"] == "blocked"
        or attempts >= plan.budget.max_attempts_per_milestone
    ):
        remote_store.create_deviation(
            plan.id,
            plan.target,
            "remote milestone needs owner attention",
            {
                "type": "stalled",
                "milestone": plan.milestones[index],
                "reason": review.get("reason", ""),
                "suggested_action": review.get("guidance", ""),
            },
        )
    else:
        remote_store.update_plan(
            plan.id,
            status="approved",
            attempts=attempts,
            context=context,
            mutations=mutations,
        )


def approve_remote_deviation(settings, remote_store, did: str) -> dict:
    deviation = remote_store.get_deviation(did)
    if not deviation or deviation["status"] != "pending":
        raise RemoteOrchestrationError("no pending remote deviation")
    row = remote_store.get_plan(deviation["plan"])
    if not row:
        raise RemoteOrchestrationError("remote deviation has no plan")
    plan = RemotePlan.from_dict(row["plan"], row["target"], row["id"])
    request = deviation["request"]

    if request.get("type") == "remote_scope":
        action = request.get("proposed_action", {})
        plan = _amend_budget_for_action(plan, action)
        remote_store.update_plan(plan.id, plan=plan.to_dict())
    elif request.get("type") == "remote_budget":
        raw = plan.to_dict()
        budget = raw["remote_budget"]
        if "requested_max_actions" in request:
            budget["max_actions"] = max(
                budget["max_actions"],
                int(request["requested_max_actions"]),
            )
        if "requested_max_file_writes" in request:
            budget["max_file_writes"] = max(
                budget["max_file_writes"],
                int(request["requested_max_file_writes"]),
            )
        plan = RemotePlan.from_dict(raw, plan.target, plan.id)
        remote_store.update_plan(plan.id, plan=plan.to_dict())

    if not remote_store.resolve_deviation(did, True):
        raise RemoteOrchestrationError("remote deviation changed concurrently")
    context = dict(row.get("context", {}))
    context["next_guidance"] = str(
        request.get(
            "suggested_action",
            "Owner approved the exact requested remote deviation. Continue narrowly.",
        )
    )[:3000]
    remote_store.update_plan(
        plan.id, status="approved", attempts=0, context=context
    )
    remote_store.event(plan.id, "remote_deviation_approved_by_owner", did)
    return remote_store.get_plan(plan.id)


def reject_remote_deviation(settings, remote_store, did: str) -> dict:
    deviation = remote_store.get_deviation(did)
    if not deviation or deviation["status"] != "pending":
        raise RemoteOrchestrationError("no pending remote deviation")
    row = remote_store.get_plan(deviation["plan"])
    if not row:
        raise RemoteOrchestrationError("remote deviation has no plan")
    target_raw = remote_store.get_target(row["target"])
    if not target_raw:
        raise RemoteOrchestrationError("remote target disappeared")
    target = RemoteTarget.from_dict(target_raw)
    plan = RemotePlan.from_dict(row["plan"], target.name, row["id"])
    current = int(row["current_milestone"])
    all_mutations = list(row.get("mutations", []))
    rollback_records = [
        item for item in all_mutations
        if int(item.get("milestone", -1)) == current
    ]
    if rollback_records:
        result = ssh_bridge_call(
            settings.ssh_bridge_socket,
            {
                "op": "rollback",
                "target": target.to_dict(),
                "budget": plan.budget.to_dict(),
                "records": rollback_records,
            },
            timeout=300,
        )
        if not result.get("ok"):
            raise RemoteOrchestrationError(
                "could not safely roll back current remote milestone: "
                + result.get("output", "")[-2000:]
            )
        all_mutations = [
            item for item in all_mutations
            if int(item.get("milestone", -1)) != current
        ]

    if not remote_store.resolve_deviation(did, False):
        raise RemoteOrchestrationError("remote deviation changed concurrently")
    context = dict(row.get("context", {}))
    context["next_guidance"] = (
        "Owner rejected the deviation. Use only the original approved remote "
        "contract and find a smaller alternative."
    )
    remote_store.update_plan(
        row["id"],
        status="approved",
        attempts=0,
        context=context,
        mutations=all_mutations,
    )
    remote_store.event(row["id"], "remote_deviation_rejected_by_owner", did)
    return remote_store.get_plan(row["id"])
