"""Concise boss-facing project retrospectives produced by Qwen."""

from __future__ import annotations

from datetime import datetime
import json

from .agent import ask


NOISY_EVENT_MARKERS = (
    "codex_manager_checkin",
    "admin_guidance",
    "manager_review_depth",
    "scope_approved_by_manager",
)

MEANINGFUL_EVENT_MARKERS = (
    "created",
    "approved",
    "rejected",
    "cancel",
    "milestone",
    "deviation",
    "input",
    "install",
    "service",
    "failure",
    "failed",
    "recovery",
    "replan",
    "completed",
    "ready_for_owner_review",
    "owner_manager_chat",
)


def _when(value) -> str:
    try:
        return datetime.fromtimestamp(float(value)).astimezone().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except Exception:
        return ""


def _event_summary(event: dict) -> dict | None:
    name = str(event.get("event", "")).strip()
    if not name:
        return None
    lowered = name.lower()
    if any(marker in lowered for marker in NOISY_EVENT_MARKERS):
        return None
    if not any(marker in lowered for marker in MEANINGFUL_EVENT_MARKERS):
        return None
    detail = str(event.get("details", "")).strip()
    # Event details are internal evidence, not a boss-facing log dump. Keep a
    # short excerpt for Qwen to reason over and never expose it directly.
    return {
        "at": _when(event.get("at")),
        "event": name,
        "detail": detail[:600],
    }


def _deviation_summary(item: dict) -> dict:
    request = item.get("request") or {}
    boss = request.get("boss_request", "")
    proposed = request.get("proposed_action") or request.get("action") or {}
    return {
        "status": item.get("status", ""),
        "reason": str(item.get("reason", ""))[:500],
        "type": str(request.get("type", ""))[:80],
        "boss_request": boss if isinstance(boss, str) else "",
        "risk": str(request.get("risk", ""))[:30],
        "operation": (
            str(proposed.get("op", ""))[:80]
            if isinstance(proposed, dict) else ""
        ),
        "created": _when(item.get("created")),
    }


def _fallback_report(plans: list[dict], events: list[dict], deviations: list[dict]) -> dict:
    current = plans[-1] if plans else {}
    spec = current.get("plan") or {}
    milestones = [str(x) for x in spec.get("milestones", [])]
    current_index = int(current.get("current_milestone", 0) or 0)
    done = milestones[:max(0, current_index)]
    remaining = milestones[max(0, current_index):]
    failures = []
    for event in events:
        name = str(event.get("event", "")).lower()
        if any(word in name for word in ("fail", "recovery", "replan")):
            failures.append(
                {
                    "attempt": str(event.get("event", "")).replace("_", " "),
                    "why": str(event.get("detail", ""))[:240],
                    "response": "Qwen and Codex adjusted the approach or stopped the unsafe/unproductive path.",
                }
            )
    decisions = []
    for item in deviations:
        if item.get("boss_request"):
            decisions.append(
                f"{item.get('status', 'unknown')}: {item.get('boss_request')}"
            )
    return {
        "headline": "Manager progress report",
        "status": str(current.get("status", "unknown")),
        "summary": (
            f"{len(done)} milestone(s) are complete. "
            f"{len(remaining)} milestone(s) remain."
        ),
        "done": done[-8:],
        "did_not_work": failures[-6:],
        "decisions": decisions[-8:],
        "remaining": remaining[:8],
        "justification": (
            "Work has been kept inside the approved contract, with technical "
            "failures handled internally and owner decisions escalated only "
            "when new authority or human judgment was required."
        ),
        "timeline": events[-12:],
    }


def manager_progress_report(
    settings,
    plans: list[dict],
    events: list[dict],
    deviations: list[dict],
    mode: str,
    owner_policy: dict | None = None,
) -> dict:
    """Return one concise retrospective spanning a recovery-plan lineage."""
    compact_events = [
        value for value in (_event_summary(item) for item in events)
        if value is not None
    ][-60:]
    compact_deviations = [_deviation_summary(item) for item in deviations][-30:]
    fallback = _fallback_report(plans, compact_events, compact_deviations)
    payload_plans = []
    for row in plans[-8:]:
        context = row.get("context") or {}
        spec = row.get("plan") or {}
        payload_plans.append({
            "id": row.get("id", ""),
            "status": row.get("status", ""),
            "goal": row.get("goal", ""),
            "milestones": list(spec.get("milestones") or []),
            "current_milestone": int(row.get("current_milestone", 0) or 0),
            "result": str(row.get("result", ""))[:1200],
            "created": _when(row.get("created")),
            "updated": _when(row.get("updated")),
            "recovery_of": str(context.get("recovery_of_plan_id", "")),
            "recovery_delta": context.get("recovery_delta") or {},
            "owner_guidance": list(context.get("owner_guidance") or [])[-6:],
        })

    prompt = f"""You are Qwen, the manager reporting to your human boss.
Give a concise retrospective for the whole project lineage, not just the newest
plan. Explain what has been done, what failed, why it failed, why the chosen
responses were reasonable, what owner decisions changed the work, and what
remains.

Plans:
{json.dumps(payload_plans, ensure_ascii=False)[:22000]}

Meaningful timeline events:
{json.dumps(compact_events, ensure_ascii=False)[:18000]}

Owner decisions/deviations:
{json.dumps(compact_deviations, ensure_ascii=False)[:12000]}

Known owner policy:
{json.dumps(owner_policy or {}, ensure_ascii=False)[:6000]}

Rules:
- Be concise and managerial.
- Never dump raw commands, stack traces, secret values, tokens, or low-level logs.
- Distinguish verified success from attempts.
- For failed attempts, say what did not work and why, then what changed.
- Justify the work across the full timeline, including why replans were necessary.
- Do not pretend unfinished work is complete.
- Keep the timeline to the most important 8-12 events.

Return JSON only:
{{
  "headline": "one short sentence",
  "status": "current status in plain English",
  "summary": "2-4 short sentences",
  "done": ["completed outcome", "..."],
  "did_not_work": [
    {{"attempt": "...", "why": "...", "response": "..."}}
  ],
  "decisions": ["owner/manager decision and impact", "..."],
  "remaining": ["remaining outcome", "..."],
  "justification": "concise explanation of why the overall work/replans made sense",
  "timeline": [
    {{"at": "time if useful", "event": "...", "why": "..."}}
  ]
}}
"""
    try:
        message = ask(
            settings,
            [
                {
                    "role": "system",
                    "content": (
                        "Return strict JSON only. You are Qwen giving your boss "
                        "a concise executive project retrospective."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "balanced" if mode == "quiet" else mode,
            tools=[],
            json_mode=True,
            num_predict=1800,
            num_ctx=16384,
        )
        value = json.loads(str(message.get("content", "")))
        if not isinstance(value, dict):
            raise ValueError("manager report was not an object")
        result = {
            "headline": str(value.get("headline", "")).strip()[:300],
            "status": str(value.get("status", "")).strip()[:300],
            "summary": str(value.get("summary", "")).strip()[:1800],
            "done": [str(x)[:500] for x in (value.get("done") or [])[:10]],
            "did_not_work": [
                {
                    "attempt": str(item.get("attempt", ""))[:500],
                    "why": str(item.get("why", ""))[:700],
                    "response": str(item.get("response", ""))[:700],
                }
                for item in (value.get("did_not_work") or [])[:8]
                if isinstance(item, dict)
            ],
            "decisions": [str(x)[:600] for x in (value.get("decisions") or [])[:10]],
            "remaining": [str(x)[:500] for x in (value.get("remaining") or [])[:10]],
            "justification": str(value.get("justification", "")).strip()[:2200],
            "timeline": [
                {
                    "at": str(item.get("at", ""))[:80],
                    "event": str(item.get("event", ""))[:500],
                    "why": str(item.get("why", ""))[:700],
                }
                for item in (value.get("timeline") or [])[:12]
                if isinstance(item, dict)
            ],
        }
        if not result["summary"]:
            raise ValueError("manager report omitted summary")
        return result
    except Exception:
        return fallback
