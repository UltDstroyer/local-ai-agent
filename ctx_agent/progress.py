"""Progress snapshots and rough ETA estimates for ctx-agent plans."""

from __future__ import annotations

import time
from statistics import mean

LOCAL_PLANNING_STAGES = (
    ("Inspect project", 15),
    ("Codex drafts plan", 180),
    ("Qwen narrows scope", 90),
    ("Codex confirms contract", 150),
)

REMOTE_PLANNING_STAGES = (
    ("Inspect target", 30),
    ("Codex drafts plan", 180),
    ("Qwen narrows scope", 90),
    ("Codex confirms contract", 150),
)


def _round_seconds(value: float | int) -> int:
    return max(0, int(round(float(value))))


def _stage_estimate(name: str, remote: bool) -> int:
    text = name.lower()
    if any(word in text for word in ("verify", "check", "inspect", "test", "validate")):
        return 150 if remote else 180
    if any(word in text for word in ("install", "upgrade", "migrate", "package")):
        return 600 if remote else 480
    if any(word in text for word in ("configure", "setup", "deploy", "service", "proxy", "tunnel")):
        return 420 if remote else 420
    if any(word in text for word in ("implement", "refactor", "build", "integrate")):
        return 480 if remote else 600
    if any(word in text for word in ("document", "docs", "readme")):
        return 180
    return 300 if remote else 420


def planning_snapshot(state: dict, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    stages = list(state.get("stages") or [])
    index = int(state.get("stage_index", 0))
    started = float(state.get("started_at", now))
    stage_started = float(state.get("stage_started_at", started))
    elapsed = _round_seconds(now - started)
    stage_elapsed = _round_seconds(now - stage_started)
    active = bool(state.get("active", True))

    rendered = []
    total_estimate = 0
    completed_estimate = 0
    remaining = 0
    for i, item in enumerate(stages):
        name = str(item["name"])
        base = int(item["estimated_seconds"])
        estimate = base
        status = "pending"
        item_elapsed = 0
        if i < index:
            status = "complete"
            completed_estimate += base
        elif i == index and active:
            status = "running"
            item_elapsed = stage_elapsed
            estimate = max(base, int(stage_elapsed * 1.5))
            remaining += max(0, estimate - stage_elapsed)
        elif i > index or not active:
            remaining += base
        total_estimate += estimate
        rendered.append({
            "index": i + 1,
            "name": name,
            "status": status,
            "estimated_seconds": estimate,
            "elapsed_seconds": item_elapsed if status == "running" else None,
        })

    if not active:
        remaining = 0

    progress_weight = completed_estimate
    if active and 0 <= index < len(stages):
        current = rendered[index]
        progress_weight += min(
            current["estimated_seconds"],
            stage_elapsed,
        )
    denominator = max(1, sum(x["estimated_seconds"] for x in rendered))
    percent = min(100, int(round(progress_weight * 100 / denominator)))

    return {
        "kind": state.get("kind", "planning"),
        "scope": state.get("scope", ""),
        "phase": "planning" if active else state.get("phase", "finished"),
        "status": state.get("status", "running" if active else "finished"),
        "current_stage_index": index + 1 if stages and active else None,
        "current_stage_name": (
            rendered[index]["name"]
            if stages and active and 0 <= index < len(rendered)
            else None
        ),
        "stage_count": len(rendered),
        "planning_stages": rendered,
        "planned_execution_stages": list(state.get("planned_stages") or []),
        "elapsed_seconds": elapsed,
        "estimated_total_seconds": max(elapsed + remaining, total_estimate),
        "estimated_remaining_seconds": remaining,
        "percent_complete": percent,
        "estimate_note": "Rough estimate based on stage defaults and live elapsed time.",
    }


def _event_times(events: list[dict], name: str) -> list[float]:
    return [float(e["at"]) for e in events if e.get("event") == name]


def plan_snapshot(
    row: dict,
    events: list[dict],
    *,
    remote: bool,
    mode: str,
    paused: bool,
    now: float | None = None,
) -> dict:
    now = time.time() if now is None else now
    plan = row.get("plan") or {}
    milestones = [str(x) for x in plan.get("milestones", [])]
    final_name = "Final remote verification" if remote else "Final Codex review"
    names = milestones + [final_name]
    status = str(row.get("status", ""))
    current = int(row.get("current_milestone", 0))
    created = float(row.get("created", now))
    elapsed = _round_seconds(now - created)

    approval_name = "remote_plan_approved_by_owner" if remote else "plan_approved_by_owner"
    milestone_name = "remote_milestone_completed" if remote else "milestone_completed"
    final_name_event = "remote_plan_completed" if remote else "ready_for_owner_review"

    approvals = _event_times(events, approval_name)
    milestone_times = _event_times(events, milestone_name)
    final_times = _event_times(events, final_name_event)
    work_start = approvals[0] if approvals else None

    actuals: list[int | None] = [None] * len(names)
    previous = work_start
    for i, ended in enumerate(milestone_times[: len(milestones)]):
        if previous is not None:
            actuals[i] = _round_seconds(ended - previous)
        previous = ended
    if final_times and previous is not None and names:
        actuals[-1] = _round_seconds(final_times[0] - previous)

    completed_actuals = [x for x in actuals if x is not None and x > 0]
    historical = int(mean(completed_actuals)) if completed_actuals else None

    estimates = []
    for name in names:
        base = _stage_estimate(name, remote)
        if historical is not None:
            base = max(60, int(round((base + historical) / 2)))
        estimates.append(base)

    final_complete = status == "completed" or (not remote and status == "ready_for_review")
    active_index: int | None = None
    if status in ("approved", "running", "awaiting_deviation"):
        active_index = min(current, len(names) - 1) if names else None
    elif remote and status == "running" and current >= len(milestones):
        active_index = len(names) - 1 if names else None

    stage_start = None
    if active_index is not None and work_start is not None:
        if active_index == 0:
            stage_start = work_start
        elif active_index - 1 < len(milestone_times):
            stage_start = milestone_times[active_index - 1]
        elif active_index == len(milestones) and milestone_times:
            stage_start = milestone_times[-1]

    stage_elapsed = _round_seconds(now - stage_start) if stage_start is not None else 0
    rendered = []
    total_work = 0
    remaining_work = 0
    completed_weight = 0
    for i, name in enumerate(names):
        estimate = estimates[i]
        stage_status = "pending"
        if i < current:
            stage_status = "complete"
        if i == len(names) - 1 and final_complete:
            stage_status = "complete"
        if active_index == i:
            stage_status = "running" if status == "running" else "queued"
        actual = actuals[i]
        if actual is not None:
            stage_status = "complete"
            estimate = max(1, actual)
        if stage_status == "complete":
            completed_weight += estimate
        elif stage_status == "running":
            estimate = max(estimate, int(stage_elapsed * 1.5))
            remaining_work += max(0, estimate - stage_elapsed)
        else:
            remaining_work += estimate
        total_work += estimate
        rendered.append({
            "index": i + 1,
            "name": name,
            "status": stage_status,
            "estimated_seconds": estimate,
            "actual_seconds": actual,
            "elapsed_seconds": stage_elapsed if stage_status == "running" else None,
        })

    waiting_on = None
    eta_available = status == "running" and not paused
    phase = "executing"
    if status == "awaiting_approval":
        phase = "awaiting_approval"
        waiting_on = "owner_approval"
    elif status == "approved":
        if paused:
            phase = "paused"
            waiting_on = "resume"
        elif mode != "maximum":
            phase = "waiting_for_maximum_mode"
            waiting_on = "maximum_mode"
        else:
            phase = "queued"
    elif status == "awaiting_deviation":
        phase = "awaiting_deviation"
        waiting_on = "owner_deviation_decision"
    elif status == "ready_for_review":
        phase = "ready_for_owner_review"
        waiting_on = "owner_final_review"
    elif status in ("completed", "cancelled", "rejected"):
        phase = status
        remaining_work = 0
        eta_available = status == "completed"

    current_name = None
    current_index = None
    if active_index is not None and 0 <= active_index < len(rendered):
        current_name = rendered[active_index]["name"]
        current_index = active_index + 1
    elif status == "awaiting_approval":
        current_name = "Owner approval"
    elif status == "ready_for_review":
        current_name = "Owner final review / apply"
    elif status == "awaiting_deviation":
        current_name = "Owner deviation decision"

    # Percentage is deliberately separate from ETA. Dynamic ETA estimates
    # grow when a stage takes longer than expected; using those moving values
    # for completion made progress regress (for example 70% -> 63%) when a
    # worker cycle returned to the queued/manager-review state. Use fixed
    # stage weights and stable half-credit for the active stage instead.
    progress_weights = [_stage_estimate(name, remote) for name in names]
    progress_total = max(1, sum(progress_weights))
    progress_done = 0.0
    for i, weight in enumerate(progress_weights):
        if i < current:
            progress_done += weight
        elif i == len(progress_weights) - 1 and final_complete:
            progress_done += weight
    if (
        active_index is not None
        and active_index < len(progress_weights)
        and status in ("approved", "running", "awaiting_deviation")
        and active_index >= current
    ):
        progress_done += progress_weights[active_index] * 0.5
    percent = min(100, int(round(progress_done * 100 / progress_total)))
    if status == "completed":
        percent = 100

    note = "Rough work-time estimate. It updates from completed stage durations."
    if waiting_on:
        note += " Wall-clock ETA excludes time waiting on " + waiting_on.replace("_", " ") + "."

    return {
        "kind": "remote_plan" if remote else "project_plan",
        "id": row.get("id"),
        "phase": phase,
        "status": status,
        "current_stage_index": current_index,
        "current_stage_name": current_name,
        "stage_count": len(rendered),
        "completed_stages": sum(1 for x in rendered if x["status"] == "complete"),
        "stages": rendered,
        "percent_complete": percent,
        "elapsed_seconds": elapsed,
        "current_stage_elapsed_seconds": stage_elapsed if status == "running" else None,
        "estimated_total_work_seconds": total_work,
        "estimated_work_remaining_seconds": remaining_work,
        "estimated_remaining_seconds": remaining_work if eta_available else None,
        "waiting_on": waiting_on,
        "estimate_note": note,
    }
