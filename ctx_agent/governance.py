"""Pure boss/manager/employee governance invariants.

This module deliberately has no model or store dependencies. It is the final
deterministic gate deciding whether a request is even eligible to interrupt the
owner.
"""

from __future__ import annotations


REMOTE_OWNER_TYPES = {
    "remote_scope",
    "dependency_install",
    "service_activation",
    "critical_remote_exec",
    "manager_escalation",
    "human_access",
    "human_input",
    "human_input_refresh",
}

LOCAL_OWNER_TYPES = {
    "mutation_amendment",
    "manager_escalation",
}

_INTERNAL_DECISION_MARKERS = (
    "invalid format",
    "format specifier",
    "corrected format",
    "format specification",
    "malformed json",
    "json format",
    "schema correction",
    "schema error",
    "syntax error",
    "validator error",
    "validation format",
    "retry with corrected",
    "retrying with corrected",
    "action budget",
    "execution budget",
    "attempt budget",
    "file-write budget",
    "file write budget",
)


def owner_request_is_internal(text: object) -> bool:
    if not isinstance(text, str):
        return True
    value = " ".join(text.lower().split())
    if not value:
        return True
    return any(marker in value for marker in _INTERNAL_DECISION_MARKERS)


def _plain_request(request: dict) -> str:
    value = request.get("boss_request", "")
    return value.strip() if isinstance(value, str) else ""


def _valid_input_request(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    key = value.get("key")
    prompt = value.get("prompt")
    reason = value.get("reason")
    sensitive = value.get("sensitive", False)
    return (
        isinstance(key, str)
        and bool(key.strip())
        and isinstance(prompt, str)
        and bool(prompt.strip())
        and isinstance(reason, str)
        and bool(reason.strip())
        and type(sensitive) is bool
    )


def valid_remote_owner_request(request: object) -> bool:
    if not isinstance(request, dict):
        return False
    kind = request.get("type")
    if kind not in REMOTE_OWNER_TYPES:
        return False

    boss_request = _plain_request(request)
    if owner_request_is_internal(boss_request):
        return False

    if kind in {
        "remote_scope",
        "dependency_install",
        "service_activation",
        "critical_remote_exec",
    }:
        action = request.get("proposed_action")
        if not isinstance(action, dict) or not action:
            return False
        op = action.get("op")
        if kind == "dependency_install":
            packages = action.get("packages")
            return (
                op == "package_install"
                and isinstance(packages, list)
                and bool(packages)
                and all(isinstance(item, str) and item.strip() for item in packages)
            )
        if kind == "service_activation":
            return (
                op == "service"
                and isinstance(action.get("service"), str)
                and bool(action.get("service", "").strip())
                and action.get("action") in ("start", "restart", "enable", "enable_now")
            )
        if kind == "critical_remote_exec":
            return (
                op == "remote_exec"
                and isinstance(request.get("critical_key"), str)
                and bool(request.get("critical_key", "").strip())
            )
        return isinstance(op, str) and bool(op)

    if kind in {"human_input", "human_input_refresh"}:
        return _valid_input_request(request.get("input_request"))

    if kind == "manager_escalation":
        input_request = request.get("input_request")
        return input_request is None or _valid_input_request(input_request)

    # human_access has a concrete boss_request but no action payload.
    return True


def valid_local_owner_request(request: object) -> bool:
    if not isinstance(request, dict):
        return False
    kind = request.get("type")
    if kind not in LOCAL_OWNER_TYPES:
        return False

    if kind == "mutation_amendment":
        mutation = request.get("mutation_budget")
        add_scope = request.get("add_write_scope")
        return isinstance(mutation, dict) or (
            isinstance(add_scope, list) and bool(add_scope)
        )

    boss_request = _plain_request(request)
    return not owner_request_is_internal(boss_request)
