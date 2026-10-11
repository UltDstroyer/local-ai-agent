"""Codex-led orchestration for owner-approved SSH targets."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .agent import ask
from .codex_bridge import bridge_call
from .remote import (
    PACKAGE_RE,
    SERVICE_RE,
    RemoteBudget,
    RemoteContractError,
    RemotePlan,
    RemoteTarget,
    action_deviation,
    canonical_service_name,
    critical_remote_exec_reason,
    validate_remote_action,
)
from .ssh_bridge import SSHBridgeError, ssh_bridge_call


class RemoteOrchestrationError(RuntimeError):
    pass


def _json_object(text: str) -> dict:
    if not isinstance(text, str):
        raise RemoteContractError("model response was not text")
    value = text.strip()
    if value.startswith(("~~~", "```")):
        lines = value.splitlines()
        if lines and lines[0].startswith(("~~~", "```")):
            lines = lines[1:]
        if lines and lines[-1].strip() in ("~~~", "```"):
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    last_error = None
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError as exc:
        last_error = exc
    start, end = value.find("{"), value.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(value[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as exc:
            last_error = exc
    if last_error is not None:
        raise RemoteContractError(
            f"model returned malformed JSON at line {last_error.lineno} "
            f"column {last_error.colno}: {last_error.msg}"
        ) from last_error
    raise RemoteContractError("model did not return a JSON object")


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
        except RemoteContractError as exc:
            last_error = exc
    raise RemoteContractError(
        "model failed to return complete valid JSON after one retry: "
        + str(last_error)
    )


def _remote_context_dir(settings, pid: str) -> Path:
    if settings.workspaces_dir is None:
        raise RemoteOrchestrationError("workspace directory is unavailable")
    path = settings.workspaces_dir / f"remote-{pid}"
    # workspaces_dir is provisioned as setgid (02770), so new child
    # directories inherit the shared ctx-agent-users group and setgid bit.
    # Do not set SGID explicitly here: systemd RestrictSUIDSGID=true
    # intentionally rejects attempts by the service to add that bit.
    path.mkdir(mode=0o770, parents=True, exist_ok=True)
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
    "allow_remote_admin": false,
    "max_actions": 30,
    "max_file_writes": 8,
    "max_attempts_per_milestone": 3,
    "backup_required": true
  }}
}}

Supported execution actions later are read_file, write_file, ensure_dir,
package_install, service, check, and remote_exec. Service mutations are limited
to start, restart, enable, and enable_now. Checks include systemctl_is_active,
http_get, http_head, nginx_test, journal_tail, listen_sockets, command_exists,
and path_exists.

remote_exec is a high-trust SSH-only capability. When allow_remote_admin is
true it may run an arbitrary command on this named SSH target, optionally
through non-interactive sudo. It never grants command execution on the desktop.
Use it when the owner's goal explicitly asks for hands-off/full remote
administration or when typed actions would force unnecessary manual steps.
Its commands are not constrained by the typed path/package/service allowlists,
so make this authority obvious in risks and keep commands on-task.

Keep ordinary typed permissions narrow. Sudo, package installation, public
network access, remote-admin authority, and write paths must be visible in the
contract whenever required. Never include secret values in the plan."""


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
services, domains, and capabilities. Backups must remain required where
practical. If the owner explicitly requested hands-off administration, full
control of the SSH target, broad cleanup, or rebuilding services in place,
allow_remote_admin=true is permitted and should prevent avoidable manual
handoffs. It grants arbitrary commands only on the named SSH target and never
on the desktop. Keep destructive actions tied directly to the stated goal,
preserve SSH reachability until replacement access is verified, and never
request or embed secret values. In remote_budget.allowed_domains, use exact
bare hostnames only, for example pkg.cloudflare.com. Do not put URLs, schemes,
ports, paths, CIDRs, or wildcard domains in allowed_domains. The owner will
review the resulting authority before execution."""


def _remote_admin_final_reconcile_prompt(
    plan: RemotePlan,
    concerns: list[str],
    target: RemoteTarget,
    inspection: str,
) -> str:
    return f"""You are Qwen, the manager. Codex reviewed the narrowed remote
contract and raised the concerns below. Revise the contract only as much as
needed to make the plan technically viable while preserving the owner's
authority boundary. Do not execute anything. Return the complete remote plan
JSON only.

Current plan:
{json.dumps(plan.to_dict())[:16000]}

Target:
{json.dumps(target.to_dict())}

Codex concerns:
{json.dumps(concerns)[:6000]}

Fresh inspection:
{inspection[:12000]}

Do not hide new authority inside the revision. New packages, services, sudo,
network access, remote-admin authority, and write paths must remain visible in
the contract and will still be subject to their normal owner gates during
execution. Do not add speculative authority merely to satisfy Codex."""


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


def _repair_remote_plan_prompt(
    admin: dict,
    validation_error: str,
    goal: str,
    target: RemoteTarget,
) -> str:
    return f"""The remote plan below failed deterministic contract validation. Repair only
what is necessary to satisfy the schema while preserving the owner's goal and
the previously negotiated authority. Return the complete corrected plan JSON
only.

Owner goal:
{goal}

Target:
{json.dumps(target.to_dict())}

Validation error:
{validation_error}

Invalid plan:
{json.dumps(admin)[:18000]}

Important schema rules:
- allowed_domains contains exact bare hostnames only, such as
  pkg.cloudflare.com. No URLs, schemes, ports, paths, CIDRs, or wildcards.
- allowed_read_paths and allowed_write_paths are absolute traversal-free paths
  or patterns.
- allowed_services and allowed_packages contain bare names only.
- boolean capability fields must be true/false.
- numeric limits must remain within the documented bounds.
- preserve allow_remote_admin=true when broad SSH administration was explicitly
  authorized by the owner.

Do not add new authority merely to make validation pass."""


def _validated_remote_plan(
    settings,
    admin: dict,
    target: RemoteTarget,
    pid: str,
    goal: str,
    mode: str,
) -> RemotePlan:
    try:
        return RemotePlan.from_dict(admin, target.name, pid)
    except RemoteContractError as exc:
        repaired = _ask_json_object(
            settings,
            [
                {
                    "role": "system",
                    "content": (
                        "Return strict JSON only. Repair the remote plan to "
                        "satisfy deterministic contract validation."
                    ),
                },
                {
                    "role": "user",
                    "content": _repair_remote_plan_prompt(
                        admin, str(exc), goal, target
                    ),
                },
            ],
            mode,
            num_predict=3072,
            num_ctx=8192,
        )
        try:
            return RemotePlan.from_dict(repaired, target.name, pid)
        except RemoteContractError as repaired_exc:
            raise RemoteOrchestrationError(
                "remote plan remained invalid after automatic repair: "
                + str(repaired_exc)
            ) from repaired_exc


def create_remote_plan(
    settings,
    remote_store,
    target: RemoteTarget,
    goal: str,
    mode: str,
    progress=None,
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

    if progress:
        progress(0, None)
    inspection = inspect_target(settings, target)
    pid = uuid.uuid4().hex[:12]
    context_dir = _save_inspection(
        settings, pid, target.to_dict(), inspection
    )

    if progress:
        progress(1, None)
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
    if progress:
        progress(2, candidate.get("milestones", []))

    admin = _ask_json_object(
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
        mode,
        num_predict=3072,
        num_ctx=8192,
    )
    plan = _validated_remote_plan(
        settings, admin, target, pid, goal, mode
    )
    if progress:
        progress(3, list(plan.milestones))

    agreed = None
    for round_number in range(2):
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
            raise RemoteOrchestrationError(
                "Codex could not review the remote contract"
            )
        agreed = _json_object(agreement.get("message", ""))
        if agreed.get("agreement") is True:
            break
        concerns = agreed.get("concerns")
        if not isinstance(concerns, list) or not concerns:
            raise RemoteOrchestrationError(
                "Codex rejected the remote contract without actionable concerns"
            )
        if round_number == 1:
            raise RemoteOrchestrationError(
                "Codex and Qwen could not agree on a minimal viable remote "
                "contract after bounded negotiation"
            )
        revised = _ask_json_object(
            settings,
            [
                {
                    "role": "system",
                    "content": (
                        "Return strict JSON only. Revise the remote contract as "
                        "the manager without executing anything."
                    ),
                },
                {
                    "role": "user",
                    "content": _remote_admin_final_reconcile_prompt(
                        plan,
                        [str(x) for x in concerns],
                        target,
                        inspection.get("output", ""),
                    ),
                },
            ],
            mode,
            num_predict=3072,
            num_ctx=8192,
        )
        plan = _validated_remote_plan(
            settings,
            revised,
            target,
            pid,
            goal,
            mode,
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

Available human-input keys:
{json.dumps(list((row.get("context", {}) or {}).get("available_inputs", [])))}

If an available human input is needed in an action, reference it only as
{{{{input:KEY}}}}. Never ask to see or repeat the actual value.

Return JSON only:
{{
  "actions": [
    {{"op": "read_file", "path": "/approved/path"}},
    {{"op": "write_file", "path": "/approved/path", "content": "...", "mode": "0644"}},
    {{"op": "ensure_dir", "path": "/approved/path", "mode": "0755"}},
    {{"op": "package_install", "packages": ["approved-package"]}},
    {{"op": "service", "service": "approved.service", "action": "restart"}},
    {{"op": "check", "check": "systemctl_is_active", "service": "approved.service"}},
    {{"op": "remote_exec", "command": "apt-get autoremove -y", "as_root": true, "timeout": 900}}
  ],
  "summary": "what these actions accomplish",
  "expected_result": "what should be true afterward"
}}

Use only the actions actually needed for this milestone. Prefer typed actions
when they are sufficient. Package installation/removal, service start/restart/
enable/disable, and obvious system dependency downloads MUST use typed actions
so the owner dependency/service approval gate cannot be bypassed. Do not put
those operations inside remote_exec. If allow_remote_admin is true, remote_exec
may be used for other arbitrary maintenance on the SSH target, including
filesystem cleanup, repository inspection, migrations, and work not expressible
by typed actions. Set as_root=true only when root is needed. The remote command
runs on the SSH target, not on the desktop. Never include secret tokens or
passwords. If the approved contract is insufficient, propose the minimum
required typed action anyway so ctx-agent can turn it into an owner-visible
deviation request."""


def _human_input_error(input_request: object) -> str | None:
    if input_request is None:
        return None
    if not isinstance(input_request, dict):
        return "input_request must be null or an object"

    key = str(input_request.get("key", "")).strip()
    prompt_text = str(input_request.get("prompt", "")).strip()
    reason_text = str(input_request.get("reason", "")).strip()
    sensitive = input_request.get("sensitive", False)

    if (
        not key
        or len(key) > 64
        or not key.replace("_", "").replace("-", "").isalnum()
        or not prompt_text
        or not reason_text
        or type(sensitive) is not bool
    ):
        return "invalid input_request"

    combined = (key + " " + prompt_text + " " + reason_text).lower()
    observable_markers = (
        " status",
        "_status",
        "is running",
        "service running",
        "properly configured",
        "is configured",
        "check whether",
        "verify whether",
        "confirm whether",
        "what does ",
        "show me",
        "output of",
        "journal",
        "systemctl",
    )
    if any(marker in combined for marker in observable_markers):
        return (
            "input_request asks the owner for system state that Qwen/Codex "
            "must inspect themselves"
        )

    return None


def _codex_manager_checkin_remote(
    settings,
    plan: RemotePlan,
    row: dict,
    context_dir: Path,
    codex_thread: str,
    inspection: str,
    results: list[dict],
    manager_review: dict,
    mode: str,
) -> dict:
    """Let Codex and Qwen confer before any owner escalation."""
    manager_note = {
        "state": manager_review.get("state"),
        "guidance": manager_review.get("guidance"),
        "reason": manager_review.get("reason"),
        "owner_needed": manager_review.get("owner_needed", False),
        "owner_request": manager_review.get("owner_request", ""),
        "risk": manager_review.get("risk", "medium"),
    }
    prompt = f"""You are Codex, the senior technical engineer. Qwen is your manager.
You are generally the stronger technical planner. Before the owner is interrupted,
review Qwen's latest assessment and either agree with it or propose a better
technical path that stays inside the approved authority.

Goal:
{plan.goal}

Current milestone:
{plan.milestones[int(row["current_milestone"])]}

Approved authority:
{json.dumps(plan.budget.to_dict())}

Fresh inspection:
{inspection[:18000]}

Latest action results:
{json.dumps(results)[-12000:]}

Qwen manager assessment:
{json.dumps(manager_note)}

Persistent context:
{json.dumps(row.get("context", {}))[-10000:]}

Return JSON only:
{{
  "agreement": true,
  "better_strategy": "",
  "guidance_to_manager": "",
  "owner_needed": false,
  "owner_request": "",
  "risk": "low|medium|high",
  "dependency_request": null
}}

If a required dependency is absent, dependency_request must be exactly one object:
{{
  "kind": "package" | "service",
  "name": "exact package or service name",
  "action": "start|restart|enable|enable_now for services, empty for packages",
  "reason": "why this dependency is required for the current milestone"
}}

Rules:
- Prefer solving the issue together without involving the owner.
- If you can see a better technical route, set agreement=false and describe it
  concretely in better_strategy and guidance_to_manager.
- Set owner_needed=true only for genuine new authority, a new dependency/service
  approval, a credential/human-only input, or a human preference/choice.
- If a dependency required by the technical approach is missing, name it in
  dependency_request instead of vaguely describing an inactive/missing service.
- Never ask the owner for observable machine state.
- Do not bundle unrelated owner decisions.
"""
    reply = bridge_call(
        settings.codex_bridge_socket,
        {
            "op": "consult",
            "cwd": str(context_dir),
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
                "Codex check-in did not complete. Keep this internal and retry "
                "before asking the owner."
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
    dependency = value.get("dependency_request")
    if dependency is not None:
        invalid_dependency = False
        if not isinstance(dependency, dict):
            invalid_dependency = True
            dependency = None
        else:
            kind = str(dependency.get("kind", "")).strip()
            name = str(dependency.get("name", "")).strip()
            reason = str(dependency.get("reason", "")).strip()
            service_action = str(dependency.get("action", "")).strip()
            if (
                kind not in ("package", "service")
                or not name
                or not reason
                or (
                    kind == "service"
                    and service_action not in (
                        "start", "restart", "enable", "enable_now"
                    )
                )
            ):
                invalid_dependency = True
                dependency = None
            else:
                dependency = {
                    "kind": kind,
                    "name": name[:160],
                    "action": service_action if kind == "service" else "",
                    "reason": reason[:1200],
                }
                value["owner_needed"] = True
        value["dependency_request"] = dependency
        if invalid_dependency:
            value["owner_needed"] = False
            value["owner_request"] = ""
            value["agreement"] = False
            correction = (
                "Correct the dependency_request schema internally. It must name "
                "exactly one package or service and explain why it is required. "
                "Do not ask the owner to approve formatting."
            )
            value["guidance_to_manager"] = correction
            value["better_strategy"] = correction
    if (
        value.get("owner_needed") is True
        and not value.get("owner_request")
        and not isinstance(value.get("dependency_request"), dict)
    ):
        value["owner_needed"] = False
        value["agreement"] = False
        correction = (
            "A boss escalation requires exactly one concrete owner_request. "
            "Clarify the decision internally before escalating."
        )
        value["guidance_to_manager"] = correction
        value["better_strategy"] = correction
    return value


def _completion_review(
    settings, plan: RemotePlan, milestone: str, results: list[dict],
    inspection: str, mode: str,
) -> dict:
    prompt = f"""You are the local manager reviewing a completed Codex work cycle.
Do not re-litigate individual in-contract actions. Decide whether the milestone
is complete, should continue, or is blocked based on results and fresh state.

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
  "reason": "why",
  "owner_needed": false,
  "owner_request": "",
  "risk": "low|medium|high",
  "input_request": null
}}

Set owner_needed=true only when you genuinely need a new boss decision,
permission, or one human-only input. Technical debugging and choosing a new
implementation approach are your job with Codex. Never ask the boss for
observable machine state such as whether a service is running, configured,
reachable, healthy, or what a command returned. Inspect those yourself.

If a human-only input is required, input_request must be exactly one object:
{{
  "key": "short_safe_name",
  "prompt": "what the boss should enter",
  "reason": "why this exact value is needed now",
  "sensitive": true
}}
Never request multiple values together. Never include the value itself in your
response, plan, context, or logs.

If the current approach is failing repeatedly, use the full inspection, results,
verification criteria, and prior context to choose a materially different
strategy before escalating to the owner."""
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
            raise ValueError("bad completion state")
        value["owner_needed"] = value.get("owner_needed") is True
        owner_request = value.get("owner_request", "")
        if not isinstance(owner_request, str):
            owner_request = ""
        value["owner_request"] = owner_request.strip()[:500]
        risk = str(value.get("risk", "medium")).lower()
        value["risk"] = risk if risk in ("low", "medium", "high") else "medium"
        input_request = value.get("input_request")
        input_error = _human_input_error(input_request)
        if input_error:
            raise ValueError(input_error)
        if input_request is not None:
            value["input_request"] = {
                "key": str(input_request["key"]).strip(),
                "prompt": str(input_request["prompt"]).strip()[:300],
                "reason": str(input_request["reason"]).strip()[:600],
                "sensitive": input_request.get("sensitive", False),
            }
            value["owner_needed"] = True
            if not value["owner_request"]:
                value["owner_request"] = (
                    "Allow me to request one human-only input needed for this step."
                )
        if value["owner_needed"] and not value["owner_request"]:
            raise ValueError("owner_needed requires one concrete owner_request")
        return value
    except Exception:
        return {
            "state": "continue",
            "guidance": "Inspect the latest results and verify the milestone again.",
            "context_note": "",
            "reason": "administrator completion review was inconclusive",
            "owner_needed": False,
            "owner_request": "",
            "risk": "medium",
        }


def _reset_remote_transient_recovery(context: dict) -> dict:
    value = dict(context)
    for key in (
        "invalid_proposal_recoveries",
        "unsupported_action_recoveries",
        "dependency_review_recoveries",
        "dependency_disagreement_cycles",
        "critical_review_recoveries",
        "critical_action_disagreement_cycles",
        "manager_recovery_cycles",
        "manager_review_recoveries",
        "worker_internal_recoveries",
    ):
        value.pop(key, None)
    return value


def _append_history(context: dict, entry: dict) -> dict:
    value = dict(context) if isinstance(context, dict) else {}
    history = list(value.get("history", []))
    history.append(entry)
    value["history"] = history[-30:]
    return value


def _manager_replan_remote(
    settings,
    plan: RemotePlan,
    row: dict,
    inspection: str,
    failure: str,
    mode: str,
) -> dict:
    """Have Qwen synthesize a materially different strategy from full context."""
    context = dict(row.get("context", {}))
    prompt = f"""You are Qwen, the manager. The current approach is repeatedly failing.
Create a materially different strategy for Codex from the full available context.
Do not ask the owner unless new authority or one human-only input is genuinely required.

Goal: {plan.goal}
Current milestone: {plan.milestones[int(row["current_milestone"])]}
Verification: {json.dumps(plan.verification)}
Approved authority: {json.dumps(plan.budget.to_dict())}
Latest inspection: {inspection[:22000]}
Persistent manager context and prior attempts: {json.dumps(context)[-18000:]}
Latest failure: {failure[:5000]}

Return JSON only:
{{
  "strategy": "concise new approach",
  "codex_guidance": "specific next instruction",
  "why_different": "how this avoids the previous failure",
  "owner_needed": false,
  "owner_request": "",
  "risk": "low|medium|high",
  "input_request": null
}}

If one human-only input is required for the new strategy, input_request must
contain exactly key, prompt, reason, and sensitive. Ask for only one value.
Never include or infer the value itself. Never ask the owner for observable
machine state such as service status, configuration state, logs, reachability,
or command output. Qwen and Codex must inspect those themselves."""
    value = _ask_json_object(
        settings,
        [
            {
                "role": "system",
                "content": "Return strict JSON only. Replan as the manager.",
            },
            {"role": "user", "content": prompt},
        ],
        mode,
        num_predict=2048,
        num_ctx=12288,
    )
    input_error = _human_input_error(value.get("input_request"))
    if input_error:
        # Observable machine state is not a boss input. Convert this into an
        # internal replan and send Qwen/Codex back to inspect it themselves.
        value["owner_needed"] = False
        value["owner_request"] = ""
        value["input_request"] = None
        correction = (
            "Manager correction: do not ask the owner for observable system "
            "state. Inspect it directly and continue. "
        )
        value["codex_guidance"] = correction + str(
            value.get("codex_guidance", "")
        )[:2400]
    return value


def _repair_invalid_action_batch(settings, plan: RemotePlan, milestone: str, proposal: dict, error: str, mode: str) -> dict:
    """Let Qwen repair Codex's malformed action batch without involving the owner."""
    prompt = f"""You are Qwen, the manager. Codex returned an invalid action batch.
Repair it yourself. Do not ask the owner.

Milestone: {milestone}
Approved budget: {json.dumps(plan.budget.to_dict())}
Validation error: {error}
Codex proposal: {json.dumps(proposal)[:12000]}

Return JSON only with a non-empty actions array of at most 20 supported typed
actions. Supported ops: read_file, write_file, ensure_dir, package_install,
service, check, remote_exec. Preserve milestone intent and approved authority."""
    repaired = _ask_json_object(
        settings,
        [
            {"role": "system", "content": "Return strict JSON only. Repair the employee proposal."},
            {"role": "user", "content": prompt},
        ],
        mode,
        num_predict=2048,
        num_ctx=8192,
    )
    repaired_error = _remote_actions_error(repaired.get("actions"))
    if repaired_error:
        raise RemoteOrchestrationError(
            "manager could not repair invalid remote action batch: " + repaired_error
        )
    return repaired


def _remote_actions_error(actions: object) -> str | None:
    if not isinstance(actions, list):
        return (
            "Codex proposal field 'actions' must be a list; got "
            + type(actions).__name__
        )
    if not actions:
        return "Codex proposal field 'actions' must contain at least one action"
    if len(actions) > 20:
        return (
            f"Codex proposed {len(actions)} remote actions; maximum is 20 "
            "per worker step"
        )
    return None


HIGH_RISK_REMOTE_PATHS = (
    "/etc/ssh",
    "/etc/sudoers",
    "/etc/passwd",
    "/etc/shadow",
    "/boot",
    "/etc/fstab",
    "/etc/systemd/network",
    "/etc/NetworkManager",
)


def _scope_risk(action: object) -> str:
    if not isinstance(action, dict):
        return "high"
    op = action.get("op")
    if op == "remote_exec":
        return "high"
    path = str(action.get("path", ""))
    if path and any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in HIGH_RISK_REMOTE_PATHS
    ):
        return "high"
    if op in ("write_file", "ensure_dir", "package_install"):
        return "medium"
    if op == "service" and action.get("action") not in ("", "status"):
        return "medium"
    if op == "check" and action.get("check") in ("http_get", "http_head"):
        return "medium"
    return "low"


def _manager_scope_review(
    settings,
    plan: RemotePlan,
    milestone: str,
    action: object,
    reason: str,
    inspection: str,
    mode: str,
) -> dict:
    risk = _scope_risk(action)
    if isinstance(action, dict):
        op = action.get("op")
        if op in ("write_file", "ensure_dir"):
            return {
                "approve": False,
                "escalate": True,
                "risk": "medium",
                "reason": "expanding remote write/sudo authority requires owner approval",
            }
        if (
            op == "check"
            and action.get("check") in ("http_get", "http_head")
        ):
            return {
                "approve": False,
                "escalate": True,
                "risk": "medium",
                "reason": "expanding external network authority requires owner approval",
            }
        if action.get("op") == "package_install":
            return {
                "approve": False,
                "escalate": True,
                "risk": "medium",
                "reason": "downloading/installing a new dependency requires owner approval",
            }
        if (
            action.get("op") == "service"
            and action.get("action") in (
                "start", "restart", "enable", "enable_now"
            )
        ):
            return {
                "approve": False,
                "escalate": True,
                "risk": "medium",
                "reason": "activating a required service requires owner approval",
            }
    if risk == "high":
        return {
            "approve": False,
            "escalate": True,
            "risk": risk,
            "reason": "high-trust scope expansion requires owner approval",
        }
    prompt = f"""You are Qwen, the local manager for an autonomous Codex engineer.
Codex requested a small expansion to an owner-approved SSH contract.

Goal: {plan.goal}
Milestone: {milestone}
Current budget: {json.dumps(plan.budget.to_dict())}
Requested action: {json.dumps(action)}
Validator reason: {reason}
Risk tier: {risk}
Fresh inspection: {inspection[:10000]}

Decide whether this is a reasonable, minimal, on-task expansion that can be
approved without interrupting the owner. You may approve low/medium-risk typed
scope expansions only. Never approve arbitrary remote_exec, credential access,
SSH access changes, boot/disk/network lockout changes, or destructive unrelated
work. Escalate anything ambiguous.

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
            "risk": risk,
            "reason": str(value.get("reason", ""))[:2000],
        }
    except Exception as exc:
        return {
            "approve": False,
            "escalate": False,
            "internal_error": True,
            "risk": risk,
            "reason": "manager scope review failed: " + str(exc)[:1000],
        }


def _is_remote_action_format_error(reason: str) -> bool:
    lower = str(reason).lower()
    return (
        lower.startswith("invalid package_install format")
        or lower.startswith("invalid service format")
        or lower.startswith("remote action must be an object")
        or lower.startswith("invalid command existence check")
        or lower.startswith("unsupported remote service action format")
        or lower.startswith(
            "remote_exec dependency/service mutation must use a typed action"
        )
    )


def classify_remote_error(exc: Exception) -> dict:
    text = str(exc)
    lower = text.lower()
    if isinstance(exc, RemoteContractError):
        if _is_remote_action_format_error(text):
            kind = "action_format"
            action = "Repair the typed action shape internally and retry validation."
        else:
            kind = "policy_scope"
            action = "Review the exact contract violation or requested scope expansion."
    elif isinstance(exc, SSHBridgeError) or "ssh " in lower:
        if "timed out" in lower or "timeout" in lower or "exceeded" in lower:
            kind = "timeout"
            action = "Retry after checking target responsiveness; do not change scope."
        elif "permission denied" in lower or "authentication" in lower:
            kind = "credentials"
            action = "Verify SSH authentication/permissions for the existing target."
        else:
            kind = "ssh_transport"
            action = "Verify the SSH bridge and target transport, then retry."
    elif "codex" in lower and ("json" in lower or "proposal" in lower):
        kind = "codex_format"
        action = "Retry Codex with the exact formatting correction."
    elif "codex" in lower:
        kind = "codex_worker"
        action = "Retry the same Codex thread with the returned failure details."
    elif "timeout" in lower or "timed out" in lower or "exceeded" in lower:
        kind = "timeout"
        action = "Retry the failed step after checking target responsiveness."
    else:
        kind = "remote_command"
        action = "Inspect the exact remote failure and retry the smallest correction."
    return {
        "type": kind,
        "error": text[:4000],
        "suggested_action": action,
    }


def _auto_expand_execution_budget(
    remote_store,
    plan: RemotePlan,
    context: dict,
    *,
    requested_actions: int | None = None,
    requested_file_writes: int | None = None,
) -> tuple[RemotePlan, dict]:
    """Raise bookkeeping-only limits without expanding authority."""
    raw = plan.to_dict()
    budget = raw["remote_budget"]
    original = dict(context.get("original_execution_budget") or {})
    if not original:
        original = {
            "max_actions": int(budget["max_actions"]),
            "max_file_writes": int(budget["max_file_writes"]),
        }
        context["original_execution_budget"] = dict(original)

    action_cap = min(500, max(original["max_actions"] * 4, original["max_actions"] + 20))
    write_cap = min(100, max(original["max_file_writes"] * 4, original["max_file_writes"] + 8))

    changed = {}
    if requested_actions is not None and requested_actions > int(budget["max_actions"]):
        if requested_actions > action_cap:
            raise RemoteOrchestrationError(
                f"execution action budget needs {requested_actions}, above manager cap {action_cap}"
            )
        budget["max_actions"] = requested_actions
        changed["max_actions"] = requested_actions

    if (
        requested_file_writes is not None
        and requested_file_writes > int(budget["max_file_writes"])
    ):
        if requested_file_writes > write_cap:
            raise RemoteOrchestrationError(
                f"file-write budget needs {requested_file_writes}, above manager cap {write_cap}"
            )
        budget["max_file_writes"] = requested_file_writes
        changed["max_file_writes"] = requested_file_writes

    if not changed:
        return plan, context

    updated = RemotePlan.from_dict(raw, plan.target, plan.id)
    remote_store.update_plan(plan.id, plan=updated.to_dict(), context=context)
    remote_store.event(
        plan.id,
        "remote_execution_budget_expanded_by_manager",
        json.dumps(changed),
    )
    return updated, context


def _critical_exec_key(action: dict) -> str:
    payload = {
        "op": action.get("op"),
        "command": action.get("command"),
        "as_root": action.get("as_root", False),
        "timeout": action.get("timeout", 900),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:32]


def _manager_critical_exec_review(
    settings,
    plan: RemotePlan,
    milestone: str,
    action: dict,
    critical_reason: str,
    inspection: str,
    mode: str,
) -> dict:
    prompt = f"""You are Qwen, the manager. Codex proposes a critical arbitrary
remote command that requires fresh boss approval even though broad remote-admin
authority exists.

Goal: {plan.goal}
Milestone: {milestone}
Critical category: {critical_reason}
Command: {str(action.get("command", ""))[:5000]}
Run as root: {action.get("as_root", False)}
Fresh inspection: {inspection[:12000]}

Decide whether this exact critical operation is genuinely necessary now. You
cannot approve it yourself.

Return JSON only:
{{
  "needed": true,
  "reason": "why this exact critical operation is necessary or avoidable",
  "guidance": "safer/internal alternative if it is not necessary"
}}"""
    try:
        value = _ask_json_object(
            settings,
            [
                {
                    "role": "system",
                    "content": (
                        "Return strict JSON only. Review necessity; never grant "
                        "critical authority yourself."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "balanced" if mode == "quiet" else mode,
            num_predict=768,
            num_ctx=6144,
        )
        return {
            "needed": value.get("needed") is True,
            "reason": str(value.get("reason", "")).strip()[:1600],
            "guidance": str(value.get("guidance", "")).strip()[:2400],
            "internal_error": False,
        }
    except Exception as exc:
        return {
            "needed": False,
            "reason": "manager critical-action review failed: " + str(exc)[:1000],
            "guidance": (
                "Retry the internal critical-action necessity review before "
                "asking the owner."
            ),
            "internal_error": True,
        }


def _manager_dependency_review(
    settings,
    plan: RemotePlan,
    milestone: str,
    action: dict,
    proposal: dict,
    inspection: str,
    mode: str,
) -> dict:
    """Qwen verifies that Codex's proposed dependency is actually necessary."""
    prompt = f"""You are Qwen, the manager. Codex proposes adding or activating a
dependency that would require boss approval. Before interrupting the boss,
decide whether the dependency is genuinely necessary for the current milestone
or whether Codex should use an existing supported route inside current authority.

Goal: {plan.goal}
Milestone: {milestone}
Proposed dependency action: {json.dumps(action)}
Codex summary: {str(proposal.get("summary", ""))[:5000]}
Approved authority: {json.dumps(plan.budget.to_dict())}
Fresh inspection: {inspection[:14000]}

Return JSON only:
{{
  "needed": true,
  "reason": "why this exact dependency is or is not necessary",
  "guidance": "what Codex should do next if it is not necessary"
}}

Do not approve the dependency yourself. If needed=true, the boss must still
approve it. Prefer an existing supported approach when it is technically sound.
"""
    try:
        value = _ask_json_object(
            settings,
            [
                {
                    "role": "system",
                    "content": (
                        "Return strict JSON only. You are reviewing dependency "
                        "necessity, not granting authority."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "balanced" if mode == "quiet" else mode,
            num_predict=768,
            num_ctx=6144,
        )
        return {
            "needed": value.get("needed") is True,
            "reason": str(value.get("reason", "")).strip()[:1600],
            "guidance": str(value.get("guidance", "")).strip()[:2400],
            "internal_error": False,
        }
    except Exception as exc:
        return {
            "needed": False,
            "reason": "manager dependency review failed: " + str(exc)[:1000],
            "guidance": (
                "Retry the internal dependency-necessity review before asking "
                "the owner."
            ),
            "internal_error": True,
        }


def _dependency_request_from_checkin(
    plan: RemotePlan,
    dependency: object,
) -> dict | None:
    if not isinstance(dependency, dict):
        return None
    kind = str(dependency.get("kind", "")).strip()
    name = str(dependency.get("name", "")).strip()
    reason = str(dependency.get("reason", "")).strip()
    if kind == "package" and name and reason:
        action = {"op": "package_install", "packages": [name]}
        if _dependency_key(action) is None:
            return None
        request = _dependency_owner_request(
            plan,
            action,
            {"summary": reason},
            plan.milestones[0] if plan.milestones else plan.goal,
        )
        request["milestone"] = plan.milestones[0] if plan.milestones else plan.goal
        return request
    if kind == "service" and name and reason:
        service_action = str(dependency.get("action", "")).strip()
        if service_action not in ("start", "restart", "enable", "enable_now"):
            return None
        action = {
            "op": "service",
            "service": name,
            "action": service_action,
        }
        if _dependency_key(action) is None:
            return None
        request = _dependency_owner_request(
            plan,
            action,
            {"summary": reason},
        )
        request["milestone"] = plan.milestones[0] if plan.milestones else plan.goal
        return request
    return None


def _review_checkin_dependency(
    settings,
    plan: RemotePlan,
    milestone: str,
    dependency: object,
    inspection: str,
    mode: str,
) -> dict:
    """Require Qwen to confirm a dependency Codex identified in a check-in."""
    request = _dependency_request_from_checkin(plan, dependency)
    if request is None:
        return {"state": "none"}
    action = request.get("proposed_action") or {}
    review = _manager_dependency_review(
        settings,
        plan,
        milestone,
        action,
        {"summary": str((dependency or {}).get("reason", ""))},
        inspection,
        mode,
    )
    if review.get("internal_error"):
        return {
            "state": "internal_error",
            "guidance": review.get("guidance", ""),
            "reason": review.get("reason", ""),
        }
    if not review.get("needed"):
        return {
            "state": "not_needed",
            "guidance": review.get("guidance", ""),
            "reason": review.get("reason", ""),
        }
    approved_request = _dependency_owner_request(
        plan,
        action,
        {"summary": review.get("reason", "")},
        milestone,
    )
    approved_request["milestone"] = milestone
    return {"state": "needed", "request": approved_request}


def _dependency_key(action: dict) -> str | None:
    if not isinstance(action, dict):
        return None
    op = action.get("op")
    if op == "package_install":
        packages = action.get("packages")
        if (
            not isinstance(packages, list)
            or not packages
            or any(
                not isinstance(item, str)
                or not PACKAGE_RE.fullmatch(item.strip())
                for item in packages
            )
        ):
            return None
        normalized = sorted(item.strip() for item in packages)
        return "packages:" + ",".join(normalized)
    if op == "service" and action.get("action") in (
        "start", "restart", "enable", "enable_now"
    ):
        service = action.get("service")
        if (
            not isinstance(service, str)
            or not SERVICE_RE.fullmatch(service.strip())
        ):
            return None
        return "service:" + canonical_service_name(service.strip())
    return None


def _dependency_owner_request(
    plan: RemotePlan,
    action: dict,
    proposal: dict,
    milestone: str = "",
) -> dict:
    summary = str(proposal.get("summary", "")).strip()
    op = action.get("op")
    if op == "package_install":
        packages = [str(x) for x in action.get("packages", []) if str(x)]
        label = ", ".join(packages) or "the required package"
        return {
            "type": "dependency_install",
            "dependency_key": _dependency_key(action),
            "proposed_action": action,
            "boss_request": f"Allow me to download and install {label}.",
            "risk": "medium",
            "reason": (
                f"I need {label} for the current milestone: "
                f"{milestone or (plan.milestones[0] if plan.milestones else plan.goal)}. "
                + (summary if summary else "The dependency is not currently available on the target.")
            )[:1800],
            "suggested_action": (
                f"Install {label} only if you want this dependency added to the server."
            ),
        }
    service = str(action.get("service", "")).strip()
    verb = str(action.get("action", "start"))
    return {
        "type": "service_activation",
        "dependency_key": _dependency_key(action),
        "proposed_action": action,
        "boss_request": f"Allow me to {verb} the {service} service.",
        "risk": "medium",
        "reason": (
            f"I need the {service} service active for the current milestone"
            f"{': ' + milestone if milestone else ''}. "
            + (summary if summary else "This service is required by the chosen approach.")
        )[:1800],
        "suggested_action": (
            f"{verb.capitalize()} {service} only if you want this service used for the task."
        ),
    }


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
        if service:
            canonical = canonical_service_name(service)
            approved = {
                canonical_service_name(item)
                for item in budget["allowed_services"]
            }
            if canonical not in approved:
                budget["allowed_services"].append(service)
        if action.get("action") != "status":
            budget["allow_sudo"] = True
    elif op == "remote_exec":
        budget["allow_remote_admin"] = True
        if action.get("as_root"):
            budget["allow_sudo"] = True
        budget["allow_network"] = True
    elif op == "check":
        check = action.get("check")
        if check in ("systemctl_is_active", "journal_tail"):
            service = action.get("service")
            if service:
                canonical = canonical_service_name(service)
                approved = {
                    canonical_service_name(item)
                    for item in budget["allowed_services"]
                }
                if canonical not in approved:
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


def run_remote_plan_step(
    settings, remote_store, row: dict, mode: str, inputs: dict[str, str] | None = None
):
    inputs = dict(inputs or {})
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
            verdict = {
                "ready": False,
                "problems": [
                    "Codex final review failed; rerun final technical verification."
                ],
                "summary": "Final technical review did not complete.",
            }
        else:
            verdict = _json_object(review.get("message", ""))
        if verdict.get("ready") is not True:
            problems = verdict.get("problems", [])
            context = dict(row.get("context", {}))
            cycles = int(context.get("final_verification_recoveries", 0)) + 1
            context["final_verification_recoveries"] = cycles
            problem_text = (
                "; ".join(str(x) for x in problems[:8])
                if isinstance(problems, list)
                else str(problems)
            )
            context["next_guidance"] = (
                "Final verification found technical problems. Qwen and Codex should "
                "repair them inside existing authority before involving the owner: "
                + problem_text
            )[:3000]
            if cycles <= 3 and plan.milestones:
                remote_store.update_plan(
                    plan.id,
                    status="approved",
                    current_milestone=max(0, len(plan.milestones) - 1),
                    attempts=0,
                    codex_thread="",
                    context=context,
                )
                remote_store.event(
                    plan.id,
                    "remote_final_verification_rework",
                    f"{cycles}: {problem_text}"[:1500],
                )
            else:
                remote_store.update_plan(
                    plan.id,
                    status="cancelled",
                    result=(
                        "Final verification repeatedly failed without identifying "
                        "a genuine owner decision."
                    ),
                    context=context,
                )
                remote_store.event(
                    plan.id,
                    "remote_final_verification_exhausted",
                    problem_text[:1500],
                )
            return
        manager_final = _completion_review(
            settings,
            plan,
            "Final remote verification",
            [{
                "action": {"op": "final_review"},
                "ok": True,
                "returncode": 0,
                "output": str(verdict.get("summary", ""))[:6000],
            }],
            inspection.get("output", ""),
            mode,
        )
        final_context = dict(row.get("context", {}))
        final_context = _append_history(
            final_context,
            {
                "milestone": len(plan.milestones),
                "admin": str(manager_final.get("context_note", ""))[:2500],
                "state": manager_final.get("state", "continue"),
                "codex_final_summary": str(verdict.get("summary", ""))[:2500],
            },
        )
        if (
            manager_final.get("state") != "complete"
            or manager_final.get("owner_needed") is True
        ):
            cycles = int(
                final_context.get("final_manager_review_recoveries", 0)
            ) + 1
            final_context["final_manager_review_recoveries"] = cycles
            final_context["next_guidance"] = (
                "Qwen did not sign off on final remote verification. Return to "
                "the final implementation milestone, resolve the manager's "
                "technical concerns, and turn any genuine new authority need "
                "into one concrete structured action before involving the owner. "
                + str(manager_final.get("guidance", ""))
            )[:3000]
            if cycles <= 3 and plan.milestones:
                remote_store.update_plan(
                    plan.id,
                    status="approved",
                    current_milestone=max(0, len(plan.milestones) - 1),
                    attempts=0,
                    codex_thread="",
                    context=final_context,
                )
                remote_store.event(
                    plan.id,
                    "remote_final_manager_rework",
                    (
                        f"{cycles}: "
                        + str(manager_final.get("reason", ""))[:1200]
                    ),
                )
            else:
                remote_store.update_plan(
                    plan.id,
                    status="cancelled",
                    context=final_context,
                    result=(
                        "Codex and Qwen could not jointly verify the final remote "
                        "state after bounded internal recovery."
                    ),
                )
                remote_store.event(
                    plan.id,
                    "remote_final_manager_recovery_exhausted",
                    str(manager_final.get("reason", ""))[:1200],
                )
            return

        final_context["next_guidance"] = ""
        remote_store.update_plan(
            plan.id,
            status="completed",
            context=final_context,
            result=(
                "Codex and Qwen completed final remote verification.\n\n"
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
    proposal_error = _remote_actions_error(actions)

    context = dict(row.get("context", {}))
    if proposal_error:
        attempts = int(row["attempts"]) + 1
        context["next_guidance"] = (
            proposal_error
            + ". Return a concise JSON object whose actions field is a "
              "non-empty list of at most 20 typed remote actions."
        )
        context = _append_history(
            context,
            {
                "milestone": index,
                "proposal_rejected": proposal_error,
                "proposal_excerpt": json.dumps(proposal)[:3000],
            },
        )
        remote_store.event(plan.id, "remote_action_proposal_rejected", proposal_error)

        if attempts < plan.budget.max_attempts_per_milestone:
            remote_store.update_plan(
                plan.id,
                status="approved",
                attempts=attempts,
                context=context,
            )
            return

        recoveries = int(context.get("invalid_proposal_recoveries", 0)) + 1
        context["invalid_proposal_recoveries"] = recoveries
        try:
            repaired = _repair_invalid_action_batch(
                settings,
                plan,
                plan.milestones[index],
                proposal,
                proposal_error,
                mode,
            )
        except Exception as exc:
            if recoveries >= 2:
                remote_store.update_plan(
                    plan.id,
                    status="cancelled",
                    result=(
                        "Qwen stopped this milestone after repeated malformed Codex "
                        "action batches and failed manager repair: " + str(exc)[:1200]
                    ),
                    context=context,
                )
                remote_store.event(
                    plan.id,
                    "remote_manager_stopped_invalid_proposal_loop",
                    str(exc)[:1200],
                )
                return
            remote_store.update_plan(
                plan.id,
                status="approved",
                attempts=0,
                codex_thread="",
                context=context,
            )
            remote_store.event(
                plan.id,
                "remote_manager_retry_after_failed_repair",
                str(exc)[:1200],
            )
            return

        proposal = repaired
        actions = repaired["actions"]
        context["next_guidance"] = ""
        remote_store.update_plan(plan.id, attempts=0, context=context)
        remote_store.event(
            plan.id,
            "remote_manager_repaired_invalid_proposal",
            f"recovery {recoveries}",
        )

    action_count = int(context.get("action_count", 0))
    file_write_count = int(context.get("file_write_count", 0))
    if action_count + len(actions) > plan.budget.max_actions:
        try:
            plan, context = _auto_expand_execution_budget(
                remote_store,
                plan,
                context,
                requested_actions=action_count + len(actions),
            )
        except RemoteOrchestrationError as exc:
            context["next_guidance"] = (
                "The execution-count ceiling was reached. Replan into fewer, more "
                "efficient actions without expanding authority. " + str(exc)
            )[:3000]
            remote_store.update_plan(
                plan.id,
                status="approved",
                attempts=0,
                codex_thread="",
                context=context,
            )
            remote_store.event(
                plan.id,
                "remote_manager_replanned_execution_budget",
                str(exc)[:1200],
            )
            return

    validated = []
    approved_dependencies = set(context.get("approved_dependencies", []))
    approved_critical_execs = set(context.get("approved_critical_execs", []))
    critical_batch_seen = set()
    for action in actions:
        critical_key = None
        critical_approved = False
        if isinstance(action, dict) and action.get("op") == "remote_exec":
            critical_reason = critical_remote_exec_reason(
                str(action.get("command", ""))
            )
            if critical_reason:
                critical_key = _critical_exec_key(action)
                critical_approved = critical_key in approved_critical_execs
                if critical_approved:
                    if critical_key in critical_batch_seen:
                        context["next_guidance"] = (
                            "Do not repeat the same critical remote action more than "
                            "once in a single batch. Split the work and re-evaluate "
                            "after the first critical action."
                        )
                        remote_store.update_plan(
                            plan.id,
                            status="approved",
                            attempts=0,
                            context=context,
                        )
                        remote_store.event(
                            plan.id,
                            "remote_duplicate_critical_action_rejected",
                            critical_reason,
                        )
                        return
                    critical_batch_seen.add(critical_key)
                if not critical_approved:
                    critical_review = _manager_critical_exec_review(
                        settings,
                        plan,
                        plan.milestones[index],
                        action,
                        critical_reason,
                        inspection.get("output", ""),
                        mode,
                    )
                    if critical_review.get("internal_error"):
                        recoveries = int(
                            context.get("critical_review_recoveries", 0)
                        ) + 1
                        context["critical_review_recoveries"] = recoveries
                        context["next_guidance"] = critical_review.get(
                            "guidance", ""
                        )[:3000]
                        if recoveries <= 3:
                            remote_store.update_plan(
                                plan.id,
                                status="approved",
                                attempts=0,
                                context=context,
                            )
                        else:
                            remote_store.update_plan(
                                plan.id,
                                status="cancelled",
                                context=context,
                                result=(
                                    "Qwen repeatedly failed to review a critical "
                                    "remote action without producing a boss decision."
                                ),
                            )
                        remote_store.event(
                            plan.id,
                            "remote_critical_review_recovery",
                            f"{recoveries}: "
                            + str(critical_review.get("reason", ""))[:1100],
                        )
                        return
                    if not critical_review.get("needed"):
                        disagreements = int(
                            context.get("critical_action_disagreement_cycles", 0)
                        ) + 1
                        context["critical_action_disagreement_cycles"] = disagreements
                        context["next_guidance"] = (
                            critical_review.get("guidance")
                            or "Choose a non-critical approach inside existing authority."
                        )[:3000]
                        if disagreements <= 3:
                            remote_store.update_plan(
                                plan.id,
                                status="approved",
                                attempts=0,
                                context=context,
                            )
                        else:
                            remote_store.update_plan(
                                plan.id,
                                status="cancelled",
                                context=context,
                                result=(
                                    "Codex and Qwen repeatedly disagreed about a "
                                    "critical remote action without converging on "
                                    "a boss decision."
                                ),
                            )
                        remote_store.event(
                            plan.id,
                            "remote_critical_action_rejected_by_manager",
                            f"{disagreements}: {critical_reason}",
                        )
                        return
                    remote_store.create_deviation(
                        plan.id,
                        plan.target,
                        "Qwen and Codex need approval for one critical remote action",
                        {
                            "type": "critical_remote_exec",
                            "critical_key": critical_key,
                            "milestone": plan.milestones[index],
                            "reason": critical_review.get("reason", critical_reason),
                            "boss_request": (
                                "Allow one critical remote administration action: "
                                + critical_reason + "."
                            ),
                            "risk": "high",
                            "proposed_action": action,
                            "suggested_action": (
                                "Approve only this exact critical action if you want "
                                "Qwen and Codex to proceed with it."
                            ),
                        },
                    )
                    return
        dependency_key = _dependency_key(action)
        if dependency_key and dependency_key not in approved_dependencies:
            dependency_review = _manager_dependency_review(
                settings,
                plan,
                plan.milestones[index],
                action,
                proposal,
                inspection.get("output", ""),
                mode,
            )
            remote_store.event(
                plan.id,
                "remote_manager_dependency_review",
                json.dumps({
                    "needed": dependency_review.get("needed"),
                    "internal_error": dependency_review.get("internal_error"),
                    "reason": dependency_review.get("reason", ""),
                })[:1600],
            )
            if dependency_review.get("internal_error"):
                recoveries = int(
                    context.get("dependency_review_recoveries", 0)
                ) + 1
                context["dependency_review_recoveries"] = recoveries
                context["next_guidance"] = dependency_review.get(
                    "guidance", ""
                )[:3000]
                if recoveries <= 3:
                    remote_store.update_plan(
                        plan.id,
                        status="approved",
                        attempts=0,
                        context=context,
                    )
                else:
                    remote_store.update_plan(
                        plan.id,
                        status="cancelled",
                        result=(
                            "Qwen repeatedly failed to review dependency necessity "
                            "without producing a genuine owner decision."
                        ),
                        context=context,
                    )
                return
            if not dependency_review.get("needed"):
                disagreements = int(
                    context.get("dependency_disagreement_cycles", 0)
                ) + 1
                context["dependency_disagreement_cycles"] = disagreements
                context["next_guidance"] = (
                    dependency_review.get("guidance")
                    or "Choose an approach that stays inside existing authority."
                )[:3000]
                context = _append_history(
                    context,
                    {
                        "milestone": index,
                        "dependency_rejected_by_manager": dependency_key,
                        "reason": dependency_review.get("reason", ""),
                        "disagreement_cycle": disagreements,
                    },
                )
                if disagreements <= 3:
                    remote_store.update_plan(
                        plan.id,
                        status="approved",
                        attempts=0,
                        context=context,
                    )
                else:
                    remote_store.update_plan(
                        plan.id,
                        status="cancelled",
                        context=context,
                        result=(
                            "Codex and Qwen repeatedly disagreed on the necessity "
                            "of a new dependency without converging on a boss decision."
                        ),
                    )
                    remote_store.event(
                        plan.id,
                        "remote_dependency_disagreement_exhausted",
                        dependency_key,
                    )
                return
            request = _dependency_owner_request(
                plan,
                action,
                {
                    **proposal,
                    "summary": dependency_review.get("reason")
                    or proposal.get("summary", ""),
                },
                plan.milestones[index],
            )
            request["milestone"] = plan.milestones[index]
            remote_store.create_deviation(
                plan.id,
                plan.target,
                "Qwen and Codex need approval for a required remote dependency",
                request,
            )
            return
        try:
            checked = validate_remote_action(
                action,
                plan.budget,
                allow_critical_remote_exec=critical_approved,
            )
        except RemoteContractError as exc:
            reason = str(exc)
            if _is_remote_action_format_error(reason):
                attempts = int(row["attempts"]) + 1
                context["next_guidance"] = (
                    "Qwen manager correction: the proposed typed remote action has an "
                    "invalid package/service/action format. Correct the action schema "
                    "and retry without involving the owner. Validator error: " + reason
                )[:3000]
                context = _append_history(
                    context,
                    {
                        "milestone": index,
                        "manager_recovery": "typed_action_format",
                        "reason": reason[:1200],
                        "action": action if isinstance(action, dict) else {},
                    },
                )
                remote_store.event(
                    plan.id,
                    "remote_manager_recovered_action_format",
                    reason[:1200],
                )
                remote_store.update_plan(
                    plan.id,
                    status="approved",
                    attempts=min(attempts, plan.budget.max_attempts_per_milestone - 1),
                    context=context,
                )
                return
            if reason.startswith("unsupported remote action"):
                recoveries = int(context.get("unsupported_action_recoveries", 0)) + 1
                context["unsupported_action_recoveries"] = recoveries
                context["next_guidance"] = (
                    "Qwen manager correction: the broker does not support that action. "
                    "Choose a supported typed action or a previously approved remote_exec "
                    "route. Do not ask the owner merely because an implementation primitive "
                    "is unsupported. Validator error: " + reason
                )[:3000]
                context = _append_history(
                    context,
                    {
                        "milestone": index,
                        "manager_recovery": "unsupported_remote_action",
                        "recovery_count": recoveries,
                        "action": action if isinstance(action, dict) else {},
                    },
                )
                remote_store.event(
                    plan.id,
                    "remote_manager_recovered_unsupported_action",
                    reason[:1200],
                )
                if recoveries <= 3:
                    remote_store.update_plan(
                        plan.id,
                        status="approved",
                        attempts=0,
                        context=context,
                    )
                else:
                    remote_store.update_plan(
                        plan.id,
                        status="cancelled",
                        result=(
                            "Qwen and Codex repeatedly chose unsupported broker actions "
                            "without identifying a genuine owner decision."
                        ),
                        context=context,
                    )
                    remote_store.event(
                        plan.id,
                        "remote_manager_stopped_unsupported_action_loop",
                        reason[:1200],
                    )
                return

            manager = _manager_scope_review(
                settings,
                plan,
                plan.milestones[index],
                action,
                reason,
                inspection.get("output", ""),
                mode,
            )
            if manager.get("internal_error"):
                recoveries = int(context.get("manager_review_recoveries", 0)) + 1
                context["manager_review_recoveries"] = recoveries
                context["next_guidance"] = (
                    "Qwen scope review failed internally. Retry the manager review "
                    "and do not ask the owner to compensate for manager failure. "
                    + str(manager.get("reason", ""))
                )[:3000]
                if recoveries <= 3:
                    remote_store.update_plan(
                        plan.id,
                        status="approved",
                        attempts=0,
                        context=context,
                    )
                else:
                    remote_store.update_plan(
                        plan.id,
                        status="cancelled",
                        result=(
                            "Qwen scope review repeatedly failed internally without "
                            "producing a genuine owner decision."
                        ),
                        context=context,
                    )
                remote_store.event(
                    plan.id,
                    "remote_manager_scope_review_recovery",
                    f"{recoveries}: {str(manager.get('reason', ''))[:1000]}",
                )
                return

            if manager["approve"]:
                plan = _amend_budget_for_action(plan, action)
                remote_store.update_plan(plan.id, plan=plan.to_dict())
                remote_store.event(
                    plan.id,
                    "remote_scope_approved_by_manager",
                    json.dumps({
                        "risk": manager["risk"],
                        "reason": manager["reason"],
                        "action": action,
                    })[:5000],
                )
                try:
                    checked = validate_remote_action(
                        action,
                        plan.budget,
                        allow_critical_remote_exec=critical_approved,
                    )
                except RemoteContractError as retry_exc:
                    reason = str(retry_exc)
                    manager = {
                        "approve": False,
                        "escalate": True,
                        "risk": _scope_risk(action),
                        "reason": "approved expansion did not satisfy validator",
                    }
                else:
                    context = _append_history(
                        context,
                        {
                            "milestone": index,
                            "manager_scope_approval": manager,
                            "action": action,
                        },
                    )
                    if checked["op"] == "write_file":
                        file_write_count += 1
                    validated.append(checked)
                    continue

            request = action_deviation(plan, action, reason)
            request["manager_review"] = manager
            request["suggested_action"] = (
                "Qwen escalated this scope request to the owner. Approve only "
                "if the requested expansion matches the intended task."
            )
            remote_store.create_deviation(
                plan.id,
                plan.target,
                reason,
                request,
            )
            return
        if checked["op"] == "write_file":
            file_write_count += 1
        validated.append(checked)

    if file_write_count > plan.budget.max_file_writes:
        try:
            plan, context = _auto_expand_execution_budget(
                remote_store,
                plan,
                context,
                requested_file_writes=file_write_count,
            )
        except RemoteOrchestrationError as exc:
            context["next_guidance"] = (
                "The file-write count ceiling was reached. Replan into fewer writes "
                "without expanding paths or capabilities. " + str(exc)
            )[:3000]
            remote_store.update_plan(
                plan.id,
                status="approved",
                attempts=0,
                codex_thread="",
                context=context,
            )
            remote_store.event(
                plan.id,
                "remote_manager_replanned_file_budget",
                str(exc)[:1200],
            )
            return

    # The owner-approved remote contract plus deterministic validation is the
    # execution gate. The local model manages plan scope and milestone outcomes,
    # but does not second-guess every in-contract Codex action batch.
    remote_store.event(
        plan.id,
        "remote_actions_validated",
        f"{len(validated)} in-contract action(s) for milestone {index + 1}",
    )

    results = []
    mutations = list(row.get("mutations", []))
    for action in validated:
        critical_owner_approved = False
        if action.get("op") == "remote_exec":
            action_key = _critical_exec_key(action)
            if action_key in approved_critical_execs:
                critical_owner_approved = True
                approved_critical_execs.remove(action_key)
                context["approved_critical_execs"] = sorted(
                    approved_critical_execs
                )[-30:]
                remote_store.update_plan(plan.id, context=context)
                remote_store.event(
                    plan.id,
                    "remote_critical_exec_lease_consumed",
                    action_key,
                )
        result = ssh_bridge_call(
            settings.ssh_bridge_socket,
            {
                "op": "action",
                "target": target.to_dict(),
                "budget": plan.budget.to_dict(),
                "action": action,
                "inputs": inputs,
                "critical_owner_approved": critical_owner_approved,
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
        failed = next((item for item in results if not item["ok"]), None)
        failure_detail = (
            (failed or {}).get("output", "")[-3000:]
            if failed else "No action result was produced."
        )
        context["next_guidance"] = (
            "The previous remote action failed. Diagnose this exact output and "
            "self-repair inside the existing contract before asking for more "
            "authority:\n" + failure_detail
        )
        if attempts >= plan.budget.max_attempts_per_milestone:
            replan = _manager_replan_remote(
                settings,
                plan,
                row,
                inspection.get("output", ""),
                failure_detail,
                mode,
            )
            context["manager_strategy"] = str(replan.get("strategy", ""))[:3000]
            context["next_guidance"] = str(
                replan.get("codex_guidance", context["next_guidance"])
            )[:3000]
            context = _append_history(
                context,
                {
                    "milestone": index,
                    "manager_replan": context["manager_strategy"],
                    "why_different": str(replan.get("why_different", ""))[:1800],
                },
            )
            manager_review = {
                "state": "blocked" if replan.get("owner_needed") is True else "continue",
                "guidance": context["next_guidance"],
                "reason": str(replan.get("why_different", ""))[:2000],
                "owner_needed": replan.get("owner_needed") is True,
                "owner_request": str(replan.get("owner_request", ""))[:500],
                "risk": str(replan.get("risk", "medium"))[:16],
            }
            checkin = _codex_manager_checkin_remote(
                settings,
                plan,
                row,
                context_dir,
                row.get("codex_thread", ""),
                inspection.get("output", ""),
                results,
                manager_review,
                mode,
            )
            remote_store.event(
                plan.id,
                "remote_codex_manager_replan_checkin",
                json.dumps({
                    "agreement": checkin.get("agreement"),
                    "owner_needed": checkin.get("owner_needed"),
                    "risk": checkin.get("risk"),
                })[:1200],
            )
            dependency_decision = _review_checkin_dependency(
                settings,
                plan,
                plan.milestones[index],
                checkin.get("dependency_request"),
                inspection.get("output", ""),
                mode,
            )
            if dependency_decision["state"] == "needed":
                remote_store.create_deviation(
                    plan.id,
                    plan.target,
                    "Qwen and Codex need approval for a required remote dependency",
                    dependency_decision["request"],
                )
            elif dependency_decision["state"] in ("internal_error", "not_needed"):
                context["next_guidance"] = (
                    dependency_decision.get("guidance")
                    or "Reconsider the dependency and continue inside existing authority."
                )[:3000]
                remote_store.update_plan(
                    plan.id,
                    status="approved",
                    attempts=0,
                    context=context,
                )
                remote_store.event(
                    plan.id,
                    "remote_checkin_dependency_review",
                    (
                        dependency_decision["state"] + ": "
                        + str(dependency_decision.get("reason", ""))[:1200]
                    ),
                )
            elif (
                checkin.get("agreement") is False
                and checkin.get("better_strategy")
            ):
                context["manager_strategy"] = str(
                    checkin.get("better_strategy", "")
                )[:3000]
                context["next_guidance"] = str(
                    checkin.get("guidance_to_manager")
                    or checkin.get("better_strategy")
                )[:3000]
                remote_store.update_plan(
                    plan.id,
                    status="approved",
                    attempts=0,
                    codex_thread="",
                    context=context,
                )
                remote_store.event(
                    plan.id,
                    "remote_codex_overrode_manager_replan",
                    context["manager_strategy"][:1800],
                )
            elif checkin.get("owner_needed") is True:
                remote_store.create_deviation(
                    plan.id,
                    plan.target,
                    "Qwen and Codex need one owner decision",
                    {
                        "type": "manager_escalation",
                        "milestone": plan.milestones[index],
                        "reason": str(replan.get("why_different", ""))[:2000],
                        "boss_request": str(
                            checkin.get("owner_request")
                            or replan.get("owner_request", "")
                        )[:500],
                        "risk": str(
                            checkin.get("risk", replan.get("risk", "medium"))
                        )[:16],
                        "input_request": replan.get("input_request"),
                        "suggested_action": context["next_guidance"],
                    },
                )
            else:
                remote_store.update_plan(
                    plan.id,
                    status="approved",
                    attempts=0,
                    codex_thread="",
                    context=context,
                )
                remote_store.event(
                    plan.id,
                    "remote_manager_replanned",
                    context["manager_strategy"][:1800],
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
    review_mode = "deep" if int(row["attempts"]) > 0 else "light"
    if review_mode == "deep":
        context["next_guidance"] = (
            context.get("next_guidance", "")
            + " Manager review depth is elevated because this milestone needed retries."
        )[-3000:]
    review = _completion_review(
        settings,
        plan,
        plan.milestones[index],
        results,
        fresh.get("output", ""),
        mode,
    )
    remote_store.event(
        plan.id,
        "remote_manager_review",
        review_mode + ": " + str(review.get("reason", ""))[:1500],
    )
    checkin = _codex_manager_checkin_remote(
        settings,
        plan,
        row,
        context_dir,
        row.get("codex_thread", ""),
        fresh.get("output", ""),
        results,
        review,
        mode,
    )
    remote_store.event(
        plan.id,
        "remote_codex_manager_checkin",
        json.dumps({
            "agreement": checkin.get("agreement"),
            "owner_needed": checkin.get("owner_needed"),
            "risk": checkin.get("risk"),
        })[:1200],
    )
    dependency_decision = _review_checkin_dependency(
        settings,
        plan,
        plan.milestones[index],
        checkin.get("dependency_request"),
        inspection.get("output", ""),
        mode,
    )
    if dependency_decision["state"] == "needed":
        remote_store.create_deviation(
            plan.id,
            plan.target,
            "Qwen and Codex need approval for a required remote dependency",
            dependency_decision["request"],
        )
        return
    if dependency_decision["state"] in ("internal_error", "not_needed"):
        context["next_guidance"] = (
            dependency_decision.get("guidance")
            or "Reconsider the dependency and continue inside existing authority."
        )[:3000]
        remote_store.update_plan(
            plan.id,
            status="approved",
            attempts=0,
            context=context,
        )
        remote_store.event(
            plan.id,
            "remote_checkin_dependency_review",
            (
                dependency_decision["state"] + ": "
                + str(dependency_decision.get("reason", ""))[:1200]
            ),
        )
        return
    if checkin.get("agreement") is False and checkin.get("better_strategy"):
        context["manager_strategy"] = str(checkin.get("better_strategy", ""))[:3000]
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
        review["owner_needed"] = True
        if checkin.get("owner_request"):
            review["owner_request"] = checkin["owner_request"]
        review["risk"] = checkin.get("risk", review.get("risk", "medium"))
    elif review.get("owner_needed") is True:
        review["owner_needed"] = False
        review["owner_request"] = ""
        review["state"] = "continue"
        review["guidance"] = (
            "Codex did not confirm that an owner decision is required. Continue "
            "technical investigation internally and only escalate a concrete need "
            "that both manager and engineer agree on."
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
        context = _reset_remote_transient_recovery(context)
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
    if review.get("owner_needed") is True:
        remote_store.create_deviation(
            plan.id,
            plan.target,
            "Qwen requests one owner decision",
            {
                "type": "manager_escalation",
                "milestone": plan.milestones[index],
                "reason": review.get("reason", ""),
                "boss_request": review.get("owner_request", ""),
                "risk": review.get("risk", "medium"),
                "input_request": review.get("input_request"),
                "suggested_action": review.get("guidance", ""),
            },
        )
    elif attempts >= plan.budget.max_attempts_per_milestone:
        cycles = int(context.get("manager_recovery_cycles", 0)) + 1
        context["manager_recovery_cycles"] = cycles
        context["next_guidance"] = str(review.get("guidance", ""))[:3000]
        if cycles <= 3:
            remote_store.update_plan(
                plan.id,
                status="approved",
                attempts=0,
                codex_thread="",
                context=context,
            )
            remote_store.event(
                plan.id,
                "remote_manager_internal_recovery_cycle",
                f"{cycles}: {str(review.get('reason', ''))[:1000]}",
            )
        else:
            remote_store.update_plan(
                plan.id,
                status="cancelled",
                result=(
                    "Qwen and Codex exhausted bounded internal recovery cycles "
                    "without identifying a genuine owner decision."
                ),
                context=context,
            )
            remote_store.event(
                plan.id,
                "remote_manager_stopped_unproductive_loop",
                plan.milestones[index],
            )
    else:
        context["next_guidance"] = str(review.get("guidance", ""))[:3000]
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

    if not remote_store.resolve_deviation(did, True):
        raise RemoteOrchestrationError("remote deviation changed concurrently")

    current_row = remote_store.get_plan(deviation["plan"])
    if not current_row:
        raise RemoteOrchestrationError("remote plan disappeared after deviation approval")
    plan = RemotePlan.from_dict(
        current_row["plan"], current_row["target"], current_row["id"]
    )

    if request.get("type") == "remote_scope":
        action = request.get("proposed_action", {})
        plan = _amend_budget_for_action(plan, action)
        remote_store.update_plan(plan.id, plan=plan.to_dict())
    elif request.get("type") == "manual_required":
        pass
    elif request.get("type") in ("dependency_install", "service_activation"):
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

    context = dict(current_row.get("context", {}))
    if request.get("type") == "critical_remote_exec":
        approved_critical_execs = list(
            context.get("approved_critical_execs", [])
        )
        critical_key = str(request.get("critical_key", "")).strip()
        if critical_key and critical_key not in approved_critical_execs:
            approved_critical_execs.append(critical_key)
        context["approved_critical_execs"] = approved_critical_execs[-30:]
    if request.get("type") in ("dependency_install", "service_activation"):
        approved_dependencies = list(context.get("approved_dependencies", []))
        dependency_key = str(request.get("dependency_key", "")).strip()
        if dependency_key and dependency_key not in approved_dependencies:
            approved_dependencies.append(dependency_key)
        context["approved_dependencies"] = approved_dependencies[-50:]
    if request.get("type") == "manual_required":
        context["next_guidance"] = (
            "Owner handled or accepted the manual dependency. Re-inspect the "
            "target and continue without retrying the unsupported broker action."
        )
    else:
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

    if not remote_store.resolve_deviation(did, False):
        raise RemoteOrchestrationError("remote deviation changed concurrently")

    current = remote_store.get_plan(deviation["plan"])
    if not current:
        raise RemoteOrchestrationError("remote plan disappeared after rejection")
    context = dict(current.get("context", {}))
    context["next_guidance"] = (
        "Owner rejected the deviation. Preserve completed in-contract work and "
        "find an alternative that stays inside the approved remote contract."
    )
    remote_store.update_plan(
        current["id"],
        status="approved",
        attempts=0,
        context=context,
    )
    remote_store.event(
        current["id"], "remote_deviation_rejected_by_owner", did
    )
    return remote_store.get_plan(current["id"])
