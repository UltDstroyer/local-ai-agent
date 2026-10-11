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
from .governance import valid_local_owner_request


class OrchestrationError(RuntimeError):
    pass


def classify_project_error(exc: Exception) -> dict:
    text = str(exc)
    lower = text.lower()
    if isinstance(exc, ContractError):
        lower_contract = text.lower()
        if (
            "malformed json" in lower_contract
            or "did not return a json object" in lower_contract
            or "response was not text" in lower_contract
            or "format" in lower_contract
        ):
            kind = "codex_format"
            action = "Repair the structured response internally and retry."
        else:
            kind = "policy_scope"
            action = "Review the exact project contract violation."
    elif "codex" in lower and ("json" in lower or "format" in lower):
        kind = "codex_format"
        action = "Retry Codex with the exact formatting correction."
    elif "timeout" in lower or "timed out" in lower:
        kind = "timeout"
        action = "Retry after checking worker responsiveness."
    elif "codex" in lower or isinstance(exc, RuntimeError):
        kind = "codex_worker"
        action = "Retry the same Codex thread with the failure details."
    else:
        kind = "workspace_tool"
        action = "Inspect the exact workspace/tool failure before retrying."
    return {
        "type": kind,
        "error": text[:4000],
        "suggested_action": action,
    }


def _json_object(text: str) -> dict:
    if not isinstance(text, str):
        raise ContractError("model response was not text")
    stripped = text.strip()
    if stripped.startswith(("~~~", "```")):
        lines = stripped.splitlines()
        if lines and lines[0].startswith(("~~~", "```")):
            lines = lines[1:]
        if lines and lines[-1].strip() in ("~~~", "```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    last_error = None
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError as exc:
        last_error = exc
    start, end = stripped.find("{"), stripped.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(stripped[start:end + 1])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError as exc:
            last_error = exc
    if last_error is not None:
        raise ContractError(
            f"model returned malformed JSON at line {last_error.lineno} "
            f"column {last_error.colno}: {last_error.msg}"
        ) from last_error
    raise ContractError("model did not return a JSON object")


def _ask_json_object(
    settings,
    messages: list[dict],
    mode: str,
    *,
    num_predict: int = 3072,
    num_ctx: int = 8192,
) -> dict:
    """Ask Ollama for a structured object, retrying once on malformed/truncated JSON."""
    last_error = None
    for attempt in range(2):
        attempt_messages = [dict(message) for message in messages]
        if attempt:
            if attempt_messages and attempt_messages[0].get("role") == "system":
                attempt_messages[0]["content"] = (
                    str(attempt_messages[0].get("content", ""))
                    + " The previous generation was malformed or truncated. "
                    "Return a complete, concise JSON object with every required field."
                )
        response = ask(
            settings,
            attempt_messages,
            "balanced" if mode == "quiet" else mode,
            tools=[],
            json_mode=True,
            num_predict=num_predict,
            num_ctx=num_ctx,
        )
        try:
            return _json_object(str(response.get("content", "")))
        except ContractError as exc:
            last_error = exc
    raise ContractError(
        "model failed to return complete valid JSON after one retry: "
        + str(last_error)
    )


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
{json.dumps(notes)[:3000]}

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
{json.dumps(codex_plan)[:10000]}

Project inventory:
{json.dumps(files)[:6000]}

Owner notes:
{json.dumps(notes)[:5000]}

Return the same plan schema as Codex. Keep the technical plan when sound, but
reduce unnecessary write scope and budgets. Every milestone must be concrete.
Important deviations later require owner approval. Never silently authorize
credential access, host-wide changes, unrelated cleanup, or speculative
refactors."""


def _codex_agreement_prompt(plan: Plan) -> str:
    return f"""The local administrator reconciled our proposed project plan.
Do not edit files. Review the contract below as the primary engineer.

{json.dumps(plan.to_dict(), indent=2)}

Return JSON only:
{{
  "agreement": true,
  "concerns": ["..."]
}}

Set agreement=true only if the plan is technically viable, sufficiently scoped
to accomplish the owner goal, and still reasonably minimal. Do not ask for
broader authority merely for convenience."""


def _admin_final_reconcile_prompt(plan: Plan, concerns: list[str]) -> str:
    return f"""You are the local project administrator. Codex reviewed your reconciled
plan and raised the concerns below. Make the smallest revision needed to reach
technical agreement without granting speculative authority. Return the complete
plan JSON only.

Current plan:
{json.dumps(plan.to_dict())[:14000]}

Codex concerns:
{json.dumps(concerns)[:6000]}

Do not silently expand credential access, host-wide changes, unrelated cleanup,
or destructive authority. If a concern truly requires a scope or budget change,
make that exact change visible in the returned contract so the owner can approve
it."""


def create_negotiated_plan(
    settings, store, source: Path, project: str, goal: str, mode: str,
    progress=None,
) -> dict:
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

    if progress:
        progress(0, None)
    files = _inventory_summary(source)
    notes = store.notes(project)
    if progress:
        progress(1, None)
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
    if progress:
        progress(2, candidate.get("milestones", []))

    admin = _ask_json_object(
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
        mode,
        num_predict=3072,
        num_ctx=8192,
    )
    plan = Plan.from_dict(admin, project)
    if progress:
        progress(3, list(plan.milestones))

    agreement = None
    for round_number in range(2):
        agreement_reply = bridge_call(
            settings.codex_bridge_socket,
            {
                "op": "consult",
                "cwd": str(source),
                "thread_id": codex.get("thread_id", ""),
                "prompt": _codex_agreement_prompt(plan),
                "timeout": 600,
            },
            timeout=630,
        )
        if not agreement_reply.get("ok"):
            raise OrchestrationError(
                "Codex could not review the reconciled plan: "
                + agreement_reply.get("events_tail", "")[-2000:]
            )
        agreement = _json_object(agreement_reply.get("message", ""))
        if agreement.get("agreement") is True:
            break
        concerns = agreement.get("concerns")
        if not isinstance(concerns, list) or not concerns:
            raise OrchestrationError(
                "Codex did not agree to the plan and supplied no actionable concerns"
            )
        if round_number == 1:
            raise OrchestrationError(
                "Codex and the local administrator could not agree on a minimal plan: "
                + "; ".join(str(x) for x in concerns)[:3000]
            )
        final_admin = _ask_json_object(
            settings,
            [
                {
                    "role": "system",
                    "content": (
                        "Return strict JSON only. You are the administrative "
                        "authority negotiating the smallest viable project contract."
                    ),
                },
                {
                    "role": "user",
                    "content": _admin_final_reconcile_prompt(
                        plan, [str(x) for x in concerns]
                    ),
                },
            ],
            mode,
            num_predict=3072,
            num_ctx=8192,
        )
        plan = Plan.from_dict(final_admin, project, plan.id)

    stored = plan.to_dict()
    stored["planning"] = {
        "codex_thread": codex.get("thread_id", ""),
        "codex_proposal": candidate,
        "admin_reconciled": True,
        "codex_agreed": True,
        "codex_agreement": agreement,
    }
    store.create_plan(project, goal, stored)
    if codex.get("thread_id"):
        store.update_plan(plan.id, codex_thread=codex["thread_id"])
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


def _codex_manager_checkin_local(
    settings,
    plan: Plan,
    milestone: str,
    workspace: Path,
    codex_thread: str,
    codex_message: str,
    report: dict,
    patch: str,
    manager_review: dict,
) -> dict:
    """Let Codex challenge Qwen's assessment before the owner is interrupted."""
    prompt = f"""You are Codex, the senior technical engineer. Qwen is your manager.
Review Qwen's assessment before anything is escalated to the owner.

Project goal:
{plan.goal}

Milestone:
{milestone}

Approved plan:
{json.dumps(plan.to_dict())[:12000]}

Your latest report:
{codex_message[:7000]}

Observed mutation report:
{json.dumps(report)}

Diff excerpt:
{patch[:10000]}

Qwen manager assessment:
{json.dumps(manager_review)}

Return JSON only:
{{
  "agreement": true,
  "better_strategy": "",
  "guidance_to_manager": "",
  "owner_needed": false,
  "owner_request": "",
  "risk": "low|medium|high"
}}

Prefer solving technical problems with Qwen internally. If you have a better
technical route, set agreement=false and explain it. Escalate only for new
authority, dependency/config/network approval, a credential/human-only input,
or a real human preference. Never ask the owner for something you can inspect.
"""
    reply = bridge_call(
        settings.codex_bridge_socket,
        {
            "op": "consult",
            "cwd": str(workspace),
            "thread_id": codex_thread,
            "prompt": prompt,
            "timeout": 700,
        },
        timeout=730,
    )
    if not reply.get("ok"):
        return {
            "agreement": False,
            "better_strategy": "Retry the internal Codex-Qwen check-in.",
            "guidance_to_manager": (
                "Codex check-in did not complete. Keep this internal and retry."
            ),
            "owner_needed": False,
            "owner_request": "",
            "risk": manager_review.get("risk", "medium"),
        }
    try:
        value = _json_object(reply.get("message", ""))
    except Exception:
        return {
            "agreement": False,
            "better_strategy": "Repair the Codex check-in response and retry.",
            "guidance_to_manager": (
                "Codex returned an invalid check-in response. Repair it internally "
                "before asking the owner."
            ),
            "owner_needed": False,
            "owner_request": "",
            "risk": manager_review.get("risk", "medium"),
        }
    value["owner_needed"] = value.get("owner_needed") is True
    value["owner_request"] = str(value.get("owner_request", "")).strip()[:500]
    risk = str(value.get("risk", "medium")).lower()
    value["risk"] = risk if risk in ("low", "medium", "high") else "medium"
    if value["owner_needed"] and not value["owner_request"]:
        value["owner_needed"] = False
        value["agreement"] = False
        correction = (
            "A boss escalation requires exactly one concrete owner_request. "
            "Clarify the decision internally before escalating."
        )
        value["guidance_to_manager"] = correction
        value["better_strategy"] = correction
    return value


def _admin_review(settings, plan: Plan, milestone: str, codex_message: str,
                  report: dict, patch: str, context: dict, mode: str) -> dict:
    prompt = f"""You are the ctx-agent administrator supervising Codex. Determine whether
the current milestone is complete and on-task. Return JSON only.

Project goal: {plan.goal}
Milestone: {milestone}
Approved plan: {json.dumps(plan.to_dict())[:12000]}
Manager review depth: {context.get("manager_review_depth", "light")}
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
  "reason": "why",
  "owner_needed": false,
  "owner_request": "",
  "risk": "low|medium|high"
}}

Use complete only when the milestone is actually satisfied. Prefer guidance
within the approved lease. Technical diagnosis, implementation choices, and
ordinary retries stay between you and Codex. Set owner_needed=true only when a
new owner decision, permission, credential, destructive choice, or authority
expansion is genuinely required. If true, owner_request must be exactly one
concrete decision. Never bundle multiple issues into one boss request."""
    try:
        response = ask(
            settings,
            [
                {"role": "system", "content": "Return strict JSON only."},
                {"role": "user", "content": prompt},
            ],
            "balanced" if mode == "quiet" else mode,
            tools=[],
            json_mode=True,
        )
        value = _json_object(str(response.get("content", "")))
        if value.get("state") not in ("complete", "continue", "blocked"):
            raise ContractError("bad review state")
        value["owner_needed"] = value.get("owner_needed") is True
        value["owner_request"] = str(value.get("owner_request", "")).strip()[:500]
        risk = str(value.get("risk", "medium")).lower()
        value["risk"] = risk if risk in ("low", "medium", "high") else "medium"
        if value["owner_needed"] and not value["owner_request"]:
            raise ContractError("owner_needed requires one concrete owner_request")
        return value
    except (RuntimeError, ContractError, json.JSONDecodeError):
        return {
            "state": "continue",
            "guidance": "Re-check the milestone, tests, and actual diff; finish only approved work.",
            "context_note": "",
            "reason": "administrator review was inconclusive",
            "owner_needed": False,
            "owner_request": "",
            "risk": "medium",
        }


def _manager_amendment_review(
    settings,
    plan: Plan,
    amendment: dict,
    milestone: str,
    mode: str,
) -> dict:
    budget = amendment.get("mutation_budget") or {}
    high_risk = bool(
        budget.get("allow_file_deletion")
        or budget.get("allow_dependency_changes")
        or budget.get("allow_config_changes")
        or budget.get("allow_network")
    )
    if high_risk:
        return {
            "approve": False,
            "escalate": True,
            "reason": "mutation changes dependencies/config/deletions/network authority",
        }
    prompt = f"""You are Qwen, the manager for a Codex implementation worker.
Codex exceeded the current project mutation lease inside an isolated workspace.

Goal: {plan.goal}
Milestone: {milestone}
Current plan: {json.dumps(plan.to_dict())[:12000]}
Requested amendment: {json.dumps(amendment)[:8000]}

Approve only if the expansion is minimal, clearly on-task, and limited to
ordinary write-scope/file-count/line-count/new-file growth. Escalate anything
ambiguous or architectural.

Return JSON only:
{{
  "approve": true,
  "escalate": false,
  "reason": "short reason"
}}"""
    try:
        value = _ask_json_object(
            settings,
            [
                {"role": "system", "content": "Return strict JSON only."},
                {"role": "user", "content": prompt},
            ],
            "balanced" if mode == "quiet" else mode,
            num_predict=768,
            num_ctx=4096,
        )
        approved = value.get("approve") is True and value.get("escalate") is not True
        return {
            "approve": approved,
            "escalate": not approved,
            "reason": str(value.get("reason", ""))[:2000],
        }
    except Exception as exc:
        return {
            "approve": False,
            "escalate": False,
            "internal_error": True,
            "reason": "manager amendment review failed: " + str(exc)[:1000],
        }


def _internal_recovery_cycle(
    store,
    plan: Plan,
    context: dict,
    *,
    reason: str,
    guidance: str,
) -> bool:
    """Consume a bounded manager recovery cycle without involving the owner."""
    value = dict(context)
    cycles = int(value.get("manager_recovery_cycles", 0)) + 1
    value["manager_recovery_cycles"] = cycles
    value["next_guidance"] = guidance[:4000]
    store.event(plan.id, "manager_internal_recovery_cycle", f"{cycles}: {reason}"[:1500])
    if cycles <= 3:
        store.update_plan(
            plan.id,
            status="approved",
            attempts=0,
            context=value,
        )
        return True
    return False


def _reset_local_transient_recovery(context: dict) -> dict:
    value = dict(context)
    for key in (
        "manager_recovery_cycles",
        "manager_review_recoveries",
        "worker_internal_recoveries",
    ):
        value.pop(key, None)
    return value


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
            reason = "; ".join(violations)
            request = deviation_amendment(plan, report, reason)
            manager = _manager_amendment_review(
                settings,
                plan,
                request,
                "final scope verification",
                mode,
            )
            if manager.get("internal_error"):
                context = dict(row.get("context", {}))
                context["next_guidance"] = (
                    "Qwen final scope review failed internally. Retry the manager "
                    "review rather than asking the owner."
                )
                store.update_plan(
                    plan.id,
                    status="approved",
                    current_milestone=max(0, len(plan.milestones) - 1)
                    if plan.milestones else 0,
                    attempts=0,
                    context=context,
                )
                store.event(
                    plan.id,
                    "final_scope_manager_review_recovery",
                    str(manager.get("reason", ""))[:1200],
                )
                return
            if manager["approve"]:
                plan = apply_amendment(plan, request)
                store.update_plan(plan.id, plan=plan.to_dict())
                store.event(
                    plan.id,
                    "final_scope_approved_by_manager",
                    json.dumps({
                        "reason": manager["reason"],
                        "amendment": request,
                    })[:5000],
                )
                report = analyze_changes(source, workspace)
                violations = contract_violations(plan, report)
            if violations:
                request["manager_review"] = manager
                request["suggested_action"] = (
                    "Qwen reviewed the final scope change and determined that "
                    "it requires owner authority."
                )
                store.create_deviation(
                    plan.id, plan.project, "; ".join(violations), request
                )
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
        if not review.get("ok"):
            final_state = {
                "state": "continue",
                "guidance": (
                    "Codex final review did not complete. Revisit the final milestone "
                    "and rerun technical verification."
                ),
                "context_note": "Final Codex review failed.",
                "reason": "final Codex review did not complete",
                "owner_needed": False,
            }
        else:
            final_state = _admin_review(
                settings,
                plan,
                "Final project verification",
                review.get("message", ""),
                report.to_dict(),
                patch,
                context,
                mode,
            )

        if final_state.get("state") != "complete":
            cycles = int(context.get("final_review_recoveries", 0)) + 1
            context["final_review_recoveries"] = cycles
            context["next_guidance"] = str(
                final_state.get("guidance", "")
            )[:4000]
            if final_state.get("owner_needed") is True:
                context["next_guidance"] = (
                    "Qwen believes final verification may require new owner authority. "
                    "Return to implementation and turn that need into one concrete "
                    "structured action or dependency request before escalating. "
                    + context["next_guidance"]
                )[:4000]
            if cycles <= 3 and plan.milestones:
                store.update_plan(
                    plan.id,
                    status="approved",
                    current_milestone=max(0, len(plan.milestones) - 1),
                    attempts=0,
                    context=context,
                )
                store.event(
                    plan.id,
                    "final_review_rework",
                    f"{cycles}: {str(final_state.get('reason', ''))[:1200]}",
                )
            else:
                store.update_plan(
                    plan.id,
                    status="cancelled",
                    context=context,
                    result=(
                        "Final project verification repeatedly failed without "
                        "identifying a concrete owner decision."
                    ),
                )
                store.event(
                    plan.id,
                    "final_review_recovery_exhausted",
                    str(final_state.get("reason", ""))[:1200],
                )
            return

        context["next_guidance"] = ""
        manager_note = str(
            final_state.get("context_note")
            or final_state.get("reason")
            or "Final verification passed within the approved mutation contract."
        )[:3000]
        store.update_plan(
            plan.id,
            status="ready_for_review",
            context=context,
            result=(
                "Qwen manager: final verification passed within the approved "
                "mutation contract. Final owner review and explicit apply are required.\n\n"
                + manager_note
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
        guidance = (
            "The previous Codex turn failed. Qwen and Codex should diagnose the "
            "failure together, choose a technically different route when useful, "
            "and retry without expanding authority."
        )
        if attempts >= plan.budget.max_attempts_per_milestone:
            if _internal_recovery_cycle(
                store,
                plan,
                context,
                reason="Codex work turn repeatedly failed",
                guidance=guidance,
            ):
                return
            store.update_plan(
                plan.id,
                status="cancelled",
                result=(
                    "Qwen and Codex exhausted bounded internal recovery cycles "
                    "without identifying a new owner decision or authority need."
                ),
            )
            store.event(plan.id, "manager_stopped_unproductive_loop", plan.milestones[index])
        else:
            context = dict(context)
            context["next_guidance"] = guidance
            store.update_plan(plan.id, status="approved", attempts=attempts, context=context)
        return

    report = analyze_changes(source, workspace)
    violations = contract_violations(plan, report)
    if violations:
        reason = "; ".join(violations)
        request = deviation_amendment(plan, report, reason)
        request["codex_summary"] = reply.get("message", "")[:4000]
        manager = _manager_amendment_review(
            settings,
            plan,
            request,
            plan.milestones[index],
            mode,
        )
        if manager.get("internal_error"):
            context = dict(context)
            recoveries = int(context.get("manager_review_recoveries", 0)) + 1
            context["manager_review_recoveries"] = recoveries
            context["next_guidance"] = (
                "Qwen mutation-scope review failed internally. Retry manager review "
                "without asking the owner. " + str(manager.get("reason", ""))
            )[:4000]
            if recoveries <= 3:
                store.update_plan(
                    plan.id,
                    status="approved",
                    attempts=0,
                    context=context,
                )
            else:
                store.update_plan(
                    plan.id,
                    status="cancelled",
                    result=(
                        "Qwen mutation-scope review repeatedly failed internally "
                        "without producing a genuine owner decision."
                    ),
                    context=context,
                )
            store.event(
                plan.id,
                "manager_amendment_review_recovery",
                f"{recoveries}: {str(manager.get('reason', ''))[:1000]}",
            )
            return
        if manager["approve"]:
            plan = apply_amendment(plan, request)
            store.update_plan(plan.id, plan=plan.to_dict())
            store.event(
                plan.id,
                "scope_approved_by_manager",
                json.dumps({
                    "reason": manager["reason"],
                    "amendment": request,
                })[:5000],
            )
            context = dict(context)
            context["next_guidance"] = (
                "Qwen approved the narrow mutation-lease expansion. Continue "
                "the current milestone under the updated contract."
            )
            store.update_plan(plan.id, context=context)
            report = analyze_changes(source, workspace)
            violations = contract_violations(plan, report)
        if violations:
            request["manager_review"] = manager
            request["suggested_action"] = (
                "Qwen escalated this mutation-lease expansion to the owner."
            )
            store.create_deviation(plan.id, plan.project, reason, request)
            return

    _, patch = diff(source, workspace)
    context = dict(context)
    context["manager_review_depth"] = (
        "deep" if int(row["attempts"]) > 0 else "light"
    )
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
    checkin = _codex_manager_checkin_local(
        settings,
        plan,
        plan.milestones[index],
        workspace,
        row.get("codex_thread", ""),
        reply.get("message", ""),
        report.to_dict(),
        patch,
        review,
    )
    store.event(
        plan.id,
        "codex_manager_checkin",
        json.dumps({
            "agreement": checkin.get("agreement"),
            "owner_needed": checkin.get("owner_needed"),
            "risk": checkin.get("risk"),
        })[:1200],
    )
    if checkin.get("agreement") is False and checkin.get("better_strategy"):
        review["state"] = "continue"
        review["owner_needed"] = False
        review["owner_request"] = ""
        review["guidance"] = str(
            checkin.get("guidance_to_manager") or checkin.get("better_strategy")
        )[:3000]
        review["reason"] = (
            "Codex proposed a stronger technical approach during manager check-in."
        )
    elif checkin.get("owner_needed") is True:
        if review.get("owner_needed") is True:
            if checkin.get("owner_request"):
                review["owner_request"] = checkin["owner_request"]
            review["risk"] = checkin.get("risk", review.get("risk", "medium"))
        else:
            review["owner_needed"] = False
            review["owner_request"] = ""
            review["state"] = "continue"
            review["guidance"] = (
                "Codex believes a boss decision may be required, but Qwen has not "
                "independently confirmed it. Reassess together before escalating."
            )
    elif review.get("owner_needed") is True:
        review["owner_needed"] = False
        review["owner_request"] = ""
        review["state"] = "continue"
        review["guidance"] = (
            "Qwen believes a boss decision may be required, but Codex did not "
            "confirm it. Continue technical investigation internally and escalate "
            "only when both agree on one concrete owner decision."
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
        updated_context = _reset_local_transient_recovery(updated_context)
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
    if review.get("owner_needed") is True:
        request = {
            "type": "manager_escalation",
            "milestone": plan.milestones[index],
            "attempts": attempts,
            "reason": review.get("reason", ""),
            "boss_request": review.get("owner_request", ""),
            "risk": review.get("risk", "medium"),
            "suggested_action": review.get("guidance", ""),
        }
        if valid_local_owner_request(request):
            store.create_deviation(
                plan.id, plan.project,
                "Qwen requests one owner decision",
                request,
            )
        else:
            updated_context["next_guidance"] = (
                "Governance rejected the proposed boss escalation as internal or "
                "underspecified. Qwen and Codex must resolve it internally or "
                "formulate one concrete owner authority/preference decision."
            )
            store.update_plan(
                plan.id,
                status="approved",
                attempts=0,
                context=updated_context,
            )
            store.event(
                plan.id,
                "project_invalid_escalation_blocked",
                str(request.get("boss_request", ""))[:1000],
            )
    elif attempts >= plan.budget.max_attempts_per_milestone:
        if _internal_recovery_cycle(
            store,
            plan,
            updated_context,
            reason=str(review.get("reason", "")),
            guidance=str(review.get("guidance", "")),
        ):
            return
        store.update_plan(
            plan.id,
            status="cancelled",
            result=(
                "Qwen and Codex exhausted bounded internal recovery cycles "
                "without identifying a genuine owner decision."
            ),
        )
        store.event(plan.id, "manager_stopped_unproductive_loop", plan.milestones[index])
    else:
        updated_context["next_guidance"] = str(review.get("guidance", ""))[:3000]
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
    if row.get("status") != "awaiting_deviation":
        raise OrchestrationError(
            "deviation is stale because the plan is no longer waiting for it"
        )
    request = deviation["request"]
    if not valid_local_owner_request(request):
        raise OrchestrationError(
            "this request is not eligible for boss approval under current governance"
        )
    request_type = request.get("type")
    if request_type not in ("mutation_amendment", "manager_escalation"):
        raise OrchestrationError(
            "this local deviation type is obsolete or unsupported; reject it and "
            "let Qwen/Codex re-evaluate under the current policy"
        )
    if not store.resolve_deviation(did, True):
        raise OrchestrationError("deviation changed concurrently")
    current_row = store.get_plan(deviation["plan"])
    if not current_row:
        raise OrchestrationError("plan disappeared after deviation approval")
    plan = _plan_from_row(current_row)
    if request_type == "mutation_amendment":
        plan = apply_amendment(plan, request)
        store.update_plan(plan.id, plan=plan.to_dict())
    context = dict(current_row.get("context", {}))
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
    if not store.resolve_deviation(did, False):
        raise OrchestrationError("deviation changed concurrently")
    current_row = store.get_plan(deviation["plan"])
    if not current_row:
        raise OrchestrationError("plan disappeared after deviation rejection")
    restore_checkpoint(settings, source, current_row)
    context = dict(current_row.get("context", {}))
    context["next_guidance"] = (
        "Owner rejected the requested deviation. Find an alternative that stays "
        "strictly inside the original approved mutation lease."
    )
    store.update_plan(
        current_row["id"], status="approved", attempts=0, context=context
    )
    store.event(current_row["id"], "deviation_rejected_by_owner", did)
    return store.get_plan(current_row["id"])
