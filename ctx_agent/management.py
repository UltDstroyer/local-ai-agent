"""Shared manager policy helpers."""

from __future__ import annotations

from copy import deepcopy


DEFAULT_MANAGEMENT_CONTRACT = {
    "interrupt_on": [
        "new_owner_authority",
        "human_only_credential_or_input",
        "major_strategy_replacement",
        "destructive_or_high_risk_action",
        "genuine_owner_preference_choice",
    ],
    "keep_internal": [
        "technical_retries",
        "format_or_schema_repair",
        "inspectable_machine_state",
        "ordinary_diagnostics",
        "routine_restart_with_existing_authority",
        "bookkeeping_budget_adjustments",
    ],
    "recovery": {
        "minor_failure": "qwen_and_codex_handle_internally",
        "major_replan": "create_successor_and_require_owner_approval",
    },
    "reporting": {
        "style": "structured",
        "show_stage_states": True,
        "show_replan_reason": True,
        "show_replan_delta": True,
        "heartbeat": "sparse",
    },
}


def ensure_management_contract(context: dict | None) -> dict:
    value = dict(context or {})
    if not isinstance(value.get("management_contract"), dict):
        value["management_contract"] = deepcopy(DEFAULT_MANAGEMENT_CONTRACT)
    if not isinstance(value.get("owner_policy_evidence"), list):
        value["owner_policy_evidence"] = []
    return value


def deviation_severity(request: dict | None) -> str:
    request = request or {}
    risk = str(request.get("risk", "")).lower()
    kind = str(request.get("type", ""))
    if risk == "high" or kind in (
        "critical_remote_exec",
        "human_access",
    ):
        return "CRITICAL"
    return "DECISION"


def recovery_delta(
    *,
    failed_stage: str,
    completed: list[str],
    old_remaining: list[str],
    new_milestones: list[str],
    failure: str,
) -> dict:
    return {
        "why_replaced": str(failure)[:1600],
        "failed_stage": str(failed_stage)[:800],
        "preserved_completed_work": [str(x)[:800] for x in completed[-12:]],
        "old_remaining_strategy": [str(x)[:800] for x in old_remaining[:12]],
        "new_strategy": [str(x)[:800] for x in new_milestones[:12]],
    }
