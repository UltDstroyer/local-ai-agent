"""Versioned, read-only evaluation event export for external observers.

The core agent remains authoritative for execution.  This module only mirrors a
small, redacted subset of lifecycle events into an append-only JSONL stream so
tools such as context-evals can score decisions without receiving execution
capabilities.

Set CTX_EVAL_EVENTS_PATH to opt in.  When unset, emission is a no-op.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = "1"

_WRITE_LOCK = threading.Lock()

_KIND_MAP = {
    "plan_created": "plan.created",
    "remote_plan_created": "plan.created",
    "plan_approved_by_owner": "plan.approved",
    "remote_plan_approved_by_owner": "plan.approved",
    "plan_rejected_by_owner": "plan.rejected",
    "remote_plan_rejected_by_owner": "plan.rejected",
    "plan_cancelled_by_owner": "plan.cancelled",
    "remote_plan_cancelled_by_owner": "plan.cancelled",
    "deviation_requested": "decision.requested",
    "remote_deviation_requested": "decision.requested",
    "deviation_approved_by_owner": "decision.resolved",
    "remote_deviation_approved_by_owner": "decision.resolved",
    "deviation_rejected_by_owner": "decision.resolved",
    "remote_deviation_rejected_by_owner": "decision.resolved",
    "plan_failed_internal": "outcome.failed",
    "remote_plan_failed_internal": "outcome.failed",
    "completed": "outcome.completed",
}

_OWNER_EVENTS = {
    "plan_approved_by_owner",
    "remote_plan_approved_by_owner",
    "plan_rejected_by_owner",
    "remote_plan_rejected_by_owner",
    "plan_cancelled_by_owner",
    "remote_plan_cancelled_by_owner",
    "deviation_approved_by_owner",
    "remote_deviation_approved_by_owner",
    "deviation_rejected_by_owner",
    "remote_deviation_rejected_by_owner",
}


def _decision_id(details: object) -> str:
    value = str(details or "").strip()
    if not value:
        return ""
    # Existing deviation-request audit entries are "id: explanation".  Export
    # only the stable identifier so evaluator logs cannot accidentally inherit
    # credentials, prompts, or other owner-sensitive prose.
    return value.split(":", 1)[0].strip()[:128]


def _payload(raw_kind: str, details: object) -> dict:
    payload = {"raw_event": raw_kind}

    if raw_kind in {"deviation_requested", "remote_deviation_requested"}:
        ident = _decision_id(details)
        if ident:
            payload["decision_id"] = ident
        return payload

    if raw_kind in {
        "deviation_approved_by_owner",
        "remote_deviation_approved_by_owner",
        "deviation_rejected_by_owner",
        "remote_deviation_rejected_by_owner",
    }:
        ident = _decision_id(details)
        if ident:
            payload["decision_id"] = ident
        payload["decision"] = (
            "approved" if "approved_by_owner" in raw_kind else "rejected"
        )
        return payload

    if raw_kind in {"plan_failed_internal", "remote_plan_failed_internal"}:
        payload["outcome"] = "failed_internal"
        return payload

    if raw_kind == "completed":
        payload["outcome"] = "completed"
        return payload

    # Deliberately exclude raw details for all other events.  The evaluator is
    # for behavior measurement, not a second copy of potentially sensitive
    # execution logs.
    return payload


def build_eval_event(
    source: str,
    scope_id: str,
    raw_kind: str,
    details: object = "",
    *,
    at: float | None = None,
) -> dict:
    """Build one schema-v1 event with a stable privacy boundary."""
    when = float(time.time() if at is None else at)
    kind = _KIND_MAP.get(raw_kind, "audit.event")
    actor = "owner" if raw_kind in _OWNER_EVENTS else "agent"
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": uuid.uuid4().hex,
        "at": when,
        "source": str(source),
        "scope_id": str(scope_id),
        "kind": kind,
        "actor": actor,
        "payload": _payload(str(raw_kind), details),
    }


def emit_eval_event(
    source: str,
    scope_id: str,
    raw_kind: str,
    details: object = "",
    *,
    at: float | None = None,
    path: str | os.PathLike[str] | None = None,
) -> bool:
    """Append one event to the evaluator stream.

    Emission is best effort by design.  Evaluation can observe the agent, but
    it must never be able to make the agent fail or alter an execution result.
    """
    configured = path if path is not None else os.environ.get("CTX_EVAL_EVENTS_PATH")
    if not configured:
        return False

    event = build_eval_event(source, scope_id, raw_kind, details, at=at)
    target = Path(configured).expanduser()
    line = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode()

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _WRITE_LOCK:
            fd = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        return True
    except (OSError, ValueError, TypeError):
        return False


def iter_eval_events(path: str | os.PathLike[str]):
    """Yield valid exported events, skipping malformed or foreign records."""
    target = Path(path).expanduser()
    if not target.exists():
        return
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(event, dict)
                and event.get("schema_version") == SCHEMA_VERSION
                and isinstance(event.get("kind"), str)
                and isinstance(event.get("payload"), dict)
            ):
                yield event
