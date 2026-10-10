"""Codex-led project orchestration with a local administrative agent."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .agent import ask
from .codex_bridge import bridge_call
from .contracts import (
    ContractError,
    Plan,
    analyze_changes,
    apply_amendment,
    contract_violations,
    deviation_amendment,
)
from .snapshot import create, diff, inventory


class OrchestrationError(RuntimeError):
    pass


def _json_object(text: str) -> dict:
    if not isinstance(text, str):
        raise ContractError("model response was not text")
    stripped = text.strip()
    if stripped.startswith("~~~"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("~~~"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "~~~":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start, end = stripped.find("{"), stripped.rfind("}")
    if start >= 0 and end > start:
        value = json.loads(stripped[start:end + 1])
        if isinstance(value, dict):
            return value
    raise ContractError("model did not return a JSON object")


def _inventory_summary(root: Path) -> list[str]:
    return sorted(inventory(root))[:500]


def _planner_prompt(goal: str, files: list[str], notes: dict) -> str:
    return f"""You are the primary engineer negotiating a project execution plan with a
local administrative agent. Do not edit files. Produce JSON only.

User goal:
{goal}

Project files:
{json.dumps(files)}

Owner notes:
{json.dumps(notes)[:5000]}

Design the smallest practical plan that satisfies the goal. Minimize touched
files and lines. Include every file/pattern that may need modification, but do
not add speculative scope.

Return exactly:
{{
  "goal": "...",
  "milestones": ["..."],
  "read_scope": ["pattern"],
  "write_scope": ["path-or-pattern"],
  "verification": ["acceptance criterion"],
  "risks": ["..."],
  "assumptions": ["..."],
  "mutation_budget": {{
    "max_files_changed": 4,
    "max_lines_changed": 300,
    "allow_new_files": true,
    "allow_file_deletion": false,
    "allow_dependency_changes": false,
    "allow_config_changes": false,
    "allow_network": false,
    "max_attempts_per_milestone": 3
  }}
}}

Configuration, dependency, deletion, and network permissions must be false
unless the goal clearly requires them. The budget should be tight rather than
generous."""


def _admin_reconcile_prompt(goal: str, codex_plan: dict, files: list[str], notes: dict) -> str:
    return f"""You are the local project administrator. Codex is the main engineer.
Your job is to narrow scope, preserve owner intent, and create a mutation lease
that Codex cannot expand by itself. Do not edit files. Return JSON only.

Owner goal:
{goal}

Codex proposal:
{json.dumps(codex_plan)[:14000]}

Project inventory:
{json.dumps(files)[:10000]}

Owner notes:
{json.dumps(notes)[:5000]}

Return the same plan schema as Codex. Keep the technical plan when sound, but
reduce unnecessary write scope and budgets. Every milestone must be concrete.
Important deviations later require owner approval. Never silently authorize
credential access, host-wide changes, unrelated cleanup, or speculative
refactors."""


def create_negotiated_plan(settings, store, source: Path, project: str, goal: str, mode: str) -> dict:
    if settings.codex_bridge_socket is None:
        raise OrchestrationError("Codex bridge is not configured")
    if not settings.codex_bridge_socket.exists():
        raise OrchestrationError(
            "Codex bridge is not running; log into the desktop session and run "
            "'ctx agent codex status'"
        )
    health = bridge_call(settings.codex_bridge_socket, {"op": "health"}, timeout=30)
    if not health.get("available") or not health.get("authenticated"):
        raise OrchestrationError(
            "Codex is not authenticated. Run 'ctx agent codex login' and choose "
            "Sign in with ChatGPT."
        )

    files = _inventory_summary(source)
    notes = store.notes(project)
    codex = bridge_call(
        settings.codex_bridge_socket,
        {
            "op": "consult",
            "cwd": str(source),
            "prompt": _planner_prompt(goal, files, notes),
            "timeout": 900,
        },
        timeout=930,
    )
    if not codex.get("ok"):
        raise OrchestrationError("Codex planning failed: " + codex.get("events_tail", "")[-2000:])
    candidate = _json_object(codex.get("message", ""))
    if isinstance(candidate.get("plan"), dict):
        candidate = candidate["plan"]

    admin_message = ask(
        settings,
        [
            {
                "role": "system",
                "content": (
                    "Return strict JSON only. You are an administrative planner, "
                    "not a code editor."
                ),
            },
            {
                "role": "user",
                "content": _admin_reconcile_prompt(goal, candidate, files, notes),
            },
        ],
        "balanced" if mode == "quiet" else mode,
        tools=[],
    )
    admin = _json_object(str(admin_message.get("content", "")))
    plan = Plan.from_dict(admin, project)
    stored = plan.to_dict()
    stored["planning"] = {
        "codex_thread": codex.get("thread_id", ""),
        "codex_proposal": candidate,
        "admin_reconciled": True,
    }
    store.create_plan(project, goal, stored)
    return store.get_plan(plan.id)


def _plan_from_row(row: dict) -> Plan:
    return Plan.from_dict(row["plan"], row["project"], row["id"])


def _workspace(settings, pid: str) -> Path:
    if settings.workspaces_dir is None:
        raise OrchestrationError("workspace directory is not configured")
    return settings.workspaces_dir / pid


def _checkpoint(settings, pid: str, completed_count: int) -> Path:
    if settings.checkpoints_dir is None:
        raise OrchestrationError("checkpoint directory is not configured")
    return settings.checkpoints_dir / pid / f"m{completed_count:03d}"


def ensure_workspace(settings, source: Path, pid: str) -> Path:
    workspace = _workspace(settings, pid)
    if not workspace.exists():
        create(source, workspace, dir_mode=0o2770, file_mode=0o660)
    return workspace


def save_checkpoint(settings, workspace: Path, pid: str, completed_count: int):
    target = _checkpoint(settings, pid, completed_count)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    create(workspace, target)


def restore_checkpoint(settings, source: Path, row: dict):
    workspace = _workspace(settings, row["id"])
    if workspace.exists():
        shutil.rmtree(workspace)
    completed = int(row["current_milestone"])
    if completed > 0:
        checkpoint = _checkpoint(settings, row["id"], completed)
        if checkpoint.exists():
            create(checkpoint, workspace, dir_mode=0o2770, file_mode=0o660)
            return
    create(source, workspace, dir_mode=0o2770, file_mode=0o660)


def _lease_prompt(plan: Plan, milestone_index: int, context: dict, guidance: str = "") -> str:
    milestone = plan.milestones[milestone_index]
    return f"""You are the primary implementation worker. The local ctx-agent is the
administrator and policy authority. Work only on this milestone.

PROJECT GOAL:
{plan.goal}

CURRENT MILESTONE ({milestone_index + 1}/{len(plan.milestones)}):
{milestone}

APPROVED WRITE SCOPE:
{json.dumps(plan.write_scope)}

MUTATION BUDGET FOR THE WHOLE PROJECT:
{json.dumps(plan.budget.to_dict())}

VERIFICATION / ACCEPTANCE CRITERIA:
{json.dumps(plan.verification)}

ADMIN CONTEXT:
{json.dumps(context)[-8000:]}

ADMIN GUIDANCE:
{guidance or "None"}

Rules:
- Make the smallest changes necessary.
- Do not modify files outside APPROVED WRITE SCOPE.
- Do not expand dependencies, config, network access, deletions, or architecture
  unless explicitly authorized by the mutation budget.
- Inspect freely inside the managed project copy as needed.
- Run relevant tests when possible.
- If the approved scope is insufficient, STOP and explain the exact deviation
  required rather than making the unauthorized change.
- Do not perform unrelated cleanup or refactoring.
- Finish this milestone only, then summarize changes, tests, and blockers.
"""


def _admin_review(settings, plan: Plan, milestone: str, codex_message: str,
                  report: dict, patch: str, context: dict, mode: str) -> dict:
    prompt = f"""You are the ctx-agent administrator supervising Codex. Determine whether
the current milestone is complete and on-task. Return JSON only.

Project goal: {plan.goal}
Milestone: {milestone}
Approved plan: {json.dumps(plan.to_dict())[:12000]}
Persistent context: {json.dumps(context)[-6000:]}
Codex report: {codex_message[:8000]}
Observed mutation report: {json.dumps(report)}
Diff excerpt:
{patch[:12000]}

Return:
{{
  "state": "complete" | "continue" | "blocked",
  "guidance": "short instruction to Codex for the next attempt",
  "context_note": "durable summary for later milestones",
  "reason": "why"
}}

Use complete only when the milestone is actually satisfied. Prefer guidance
within the approved lease. Do not authorize scope expansion yourself."""
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
            raise ContractError("bad review state")
        return value
    except (RuntimeError, ContractError, json.JSONDecodeError):
        return {
            "state": "continue",
            "guidance": "Re-check the milestone, tests, and actual diff; finish only approved work.",
            "context_note": "",
            "reason": "administrator review was inconclusive",
        }


def _append_context(context: dict, note: str, codex_message: str) -> dict:
    value = dict(context) if isinstance(context, dict) else {}
    history = list(value.get("history", []))
    entry = {}
    if note:
        entry["admin"] = note[:2000]
    if codex_message:
        entry["codex"] = codex_message[:3000]
    if entry:
        history.append(entry)
    value["history"] = history[-24:]
    return value


def run_plan_step(settings, store, source: Path, row: dict, mode: str):
    plan = _plan_from_row(row)
    index = int(row["current_milestone"])
    if settings.codex_bridge_socket is None:
        raise OrchestrationError("Codex bridge is not configured")

    if index >= len(plan.milestones):
        workspace = ensure_workspace(settings, source, plan.id)
        report = analyze_changes(source, workspace)
        violations = contract_violations(plan, report)
        if violations:
            request = deviation_amendment(plan, report, "; ".join(violations))
            store.create_deviation(plan.id, plan.project, "; ".join(violations), request)
            return
        _, patch = diff(source, workspace)
        review = bridge_call(
            settings.codex_bridge_socket,
            {
                "op": "consult",
                "cwd": str(workspace),
                "prompt": (
                    "Review the completed project against this approved plan. Do not "
                    "edit files. Identify correctness problems, missing acceptance "
                    "criteria, or unnecessary changes.\nPLAN:\n"
                    + json.dumps(plan.to_dict())
                    + "\nDIFF:\n"
                    + patch[:20000]
                ),
                "timeout": 900,
            },
            timeout=930,
        )
        context = _append_context(
            row.get("context", {}),
            "Final scope verification passed.",
            review.get("message", ""),
        )
        store.update_plan(
            plan.id,
            status="ready_for_review",
            context=context,
            result=(
                "All milestones completed within the approved mutation contract. "
                "Final owner review and explicit apply are required.\n\nCodex review:\n"
                + review.get("message", "")[:12000]
            ),
            attempts=0,
        )
        store.event(plan.id, "ready_for_owner_review", json.dumps(report.to_dict()))
        return

    workspace = ensure_workspace(settings, source, plan.id)
    context = row.get("context", {})
    guidance = str(context.get("next_guidance", ""))[:4000]
    reply = bridge_call(
        settings.codex_bridge_socket,
        {
            "op": "work",
            "cwd": str(workspace),
            "thread_id": row.get("codex_thread", ""),
            "prompt": _lease_prompt(plan, index, context, guidance),
            "network": plan.budget.allow_network,
            "timeout": 1800,
        },
        timeout=1830,
    )
    if reply.get("thread_id") and reply.get("thread_id") != row.get("codex_thread", ""):
        store.update_plan(plan.id, codex_thread=reply["thread_id"])
    if not reply.get("ok"):
        attempts = int(row["attempts"]) + 1
        if attempts >= plan.budget.max_attempts_per_milestone:
            request = {
                "type": "stalled",
                "milestone": plan.milestones[index],
                "attempts": attempts,
                "codex_output": reply.get("events_tail", "")[-4000:],
                "suggested_action": "Let the administrator replan or provide owner guidance.",
            }
            store.create_deviation(
                plan.id, plan.project,
                f"Codex stalled on milestone {index + 1} after {attempts} attempts",
                request,
            )
        else:
            context = dict(context)
            context["next_guidance"] = (
                "The previous Codex turn failed. Diagnose the failure without expanding "
                "scope, then retry the milestone."
            )
            store.update_plan(plan.id, status="approved", attempts=attempts, context=context)
        return

    report = analyze_changes(source, workspace)
    violations = contract_violations(plan, report)
    if violations:
        reason = "; ".join(violations)
        request = deviation_amendment(plan, report, reason)
        request["codex_summary"] = reply.get("message", "")[:4000]
        store.create_deviation(plan.id, plan.project, reason, request)
        return

    _, patch = diff(source, workspace)
    review = _admin_review(
        settings,
        plan,
        plan.milestones[index],
        reply.get("message", ""),
        report.to_dict(),
        patch,
        context,
        mode,
    )
    updated_context = _append_context(
        context,
        str(review.get("context_note", "")),
        reply.get("message", ""),
    )
    updated_context["next_guidance"] = str(review.get("guidance", ""))[:4000]
    if review["state"] == "complete":
        completed = index + 1
        save_checkpoint(settings, workspace, plan.id, completed)
        updated_context["next_guidance"] = ""
        store.update_plan(
            plan.id,
            status="approved",
            current_milestone=completed,
            attempts=0,
            context=updated_context,
        )
        store.event(plan.id, "milestone_completed", f"{completed}: {plan.milestones[index]}")
        return

    attempts = int(row["attempts"]) + 1
    if review["state"] == "blocked" or attempts >= plan.budget.max_attempts_per_milestone:
        request = {
            "type": "stalled",
            "milestone": plan.milestones[index],
            "attempts": attempts,
            "reason": review.get("reason", ""),
            "codex_summary": reply.get("message", "")[:4000],
            "suggested_action": review.get("guidance", ""),
        }
        store.create_deviation(
            plan.id, plan.project,
            f"Milestone {index + 1} needs owner attention",
            request,
        )
    else:
        store.update_plan(
            plan.id,
            status="approved",
            attempts=attempts,
            context=updated_context,
        )
        store.event(plan.id, "admin_guidance", review.get("guidance", ""))


def approve_deviation(settings, store, source: Path, did: str) -> dict:
    deviation = store.get_deviation(did)
    if not deviation or deviation["status"] != "pending":
        raise OrchestrationError("no pending deviation with that id")
    row = store.get_plan(deviation["plan"])
    if not row:
        raise OrchestrationError("deviation refers to missing plan")
    plan = _plan_from_row(row)
    request = deviation["request"]
    if request.get("type") != "stalled":
        plan = apply_amendment(plan, request)
        store.update_plan(plan.id, plan=plan.to_dict())
    if not store.resolve_deviation(did, True):
        raise OrchestrationError("deviation changed concurrently")
    context = dict(row.get("context", {}))
    context["next_guidance"] = str(request.get(
        "suggested_action",
        "Continue under the newly approved mutation lease.",
    ))[:4000]
    store.update_plan(plan.id, status="approved", attempts=0, context=context)
    store.event(plan.id, "deviation_approved_by_owner", did)
    return store.get_plan(plan.id)


def reject_deviation(settings, store, source: Path, did: str) -> dict:
    deviation = store.get_deviation(did)
    if not deviation or deviation["status"] != "pending":
        raise OrchestrationError("no pending deviation with that id")
    row = store.get_plan(deviation["plan"])
    if not row:
        raise OrchestrationError("deviation refers to missing plan")
    restore_checkpoint(settings, source, row)
    if not store.resolve_deviation(did, False):
        raise OrchestrationError("deviation changed concurrently")
    context = dict(row.get("context", {}))
    context["next_guidance"] = (
        "Owner rejected the requested deviation. Find an alternative that stays "
        "strictly inside the original approved mutation lease."
    )
    store.update_plan(row["id"], status="approved", attempts=0, context=context)
    store.event(row["id"], "deviation_rejected_by_owner", did)
    return store.get_plan(row["id"])
