"""Boss-facing deviation summaries written by the local manager model."""

from __future__ import annotations

import json

from .agent import ask


def _fallback_brief(deviation: dict) -> dict:
    request = deviation.get("request") or {}
    reason = str(deviation.get("reason", "")).strip()
    error = str(request.get("error", "")).strip()
    suggested = str(request.get("suggested_action", "")).strip()
    manager = request.get("manager_review") or {}
    manager_reason = str(manager.get("reason", "")).strip()
    risk = str(request.get("risk") or manager.get("risk") or "medium").lower()
    if risk not in ("low", "medium", "high"):
        risk = "medium"

    summary_parts = []
    if reason:
        summary_parts.append(reason)
    if error and error != reason:
        summary_parts.append(error)
    if manager_reason and manager_reason not in summary_parts:
        summary_parts.append(manager_reason)
    summary = " | ".join(summary_parts)[:900]
    if not summary:
        summary = "This deviation needs your decision."

    recommendation = suggested[:500] if suggested else "Review this deviation and decide whether to approve it."
    proposed = request.get("proposed_action") or request.get("action") or {}
    request_text = str(request.get("boss_request", "")).strip()
    if not request_text and isinstance(proposed, dict) and proposed:
        op = str(proposed.get("op", "change"))
        if op == "check":
            target = proposed.get("service") or proposed.get("path") or proposed.get("url") or proposed.get("check")
            request_text = f"Allow the requested {proposed.get('check', 'check')} on {target}."
        elif op == "service":
            request_text = f"Allow {proposed.get('action', 'a change')} for service {proposed.get('service', '')}."
        elif op in ("write_file", "ensure_dir", "read_file"):
            request_text = f"Allow {op.replace('_', ' ')} on {proposed.get('path', '')}."
        elif op == "package_install":
            request_text = "Allow installation of: " + ", ".join(str(x) for x in proposed.get("packages", []))
        elif op == "remote_exec":
            request_text = "Allow a high-trust remote administration command for this step."
        else:
            request_text = f"Allow the requested {op} operation."
    return {
        "summary": summary,
        "request": request_text[:500],
        "risk": risk,
        "recommendation": recommendation,
        "decision": "Approve or reject this deviation?",
    }


def manager_deviation_brief(settings, deviation: dict, mode: str) -> dict:
    """Turn an internal deviation payload into a concise manager-to-boss brief."""
    payload = {
        "reason": deviation.get("reason", ""),
        "request": deviation.get("request", {}),
        "target": deviation.get("target", ""),
        "project": deviation.get("project", ""),
        "plan": deviation.get("plan", ""),
    }
    prompt = f"""You are Qwen, the manager. The human user is your boss.
Codex and low-level tools report to you, not directly to the boss.

An internal deviation needs a boss decision:
{json.dumps(payload, ensure_ascii=False)[:14000]}

Translate this into a short executive briefing. Hide low-level implementation
noise unless it is necessary for the decision. Do not dump commands, JSON,
stack traces, validator internals, or raw employee/tool output. Do not tell the
boss to debug Codex, SSH, or internal plumbing. You own that investigation.

Speak in first person as the manager. State only what the boss needs to know:
what is blocked, why it matters, what you recommend, and the decision you need.
If the issue is safe and routine, say so. If it is risky, name the concrete
risk in plain language.

Return JSON only:
{{
  "summary": "1-2 short sentences",
  "risk": "low|medium|high",
  "recommendation": "one short sentence",
  "decision": "one short approval/rejection question"
}}"""
    try:
        message = ask(
            settings,
            [
                {
                    "role": "system",
                    "content": (
                        "Return strict JSON only. You are the manager speaking "
                        "to your boss, never a raw tool or employee."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "balanced" if mode == "quiet" else mode,
            tools=[],
            json_mode=True,
            num_predict=512,
            num_ctx=4096,
        )
        value = json.loads(str(message.get("content", "")))
        if not isinstance(value, dict):
            raise ValueError("manager brief was not an object")
        concrete = str(value.get("request", "")).strip()
        stored = str((deviation.get("request") or {}).get("boss_request", "")).strip()
        if stored:
            concrete = stored
        if not concrete:
            raise ValueError("manager brief omitted the concrete request")
        value["request"] = concrete
        risk = str(value.get("risk", "medium")).lower()
        if risk not in ("low", "medium", "high"):
            risk = "medium"
        return {
            "summary": str(value.get("summary", "")).strip()[:700],
            "request": str(value.get("request", "")).strip()[:500],
            "risk": risk,
            "recommendation": str(value.get("recommendation", "")).strip()[:500],
            "decision": str(value.get("decision", "")).strip()[:500],
        }
    except Exception:
        # If the manager model is unavailable, show only this deviation's
        # relevant fields. Never dump plan history, unrelated events, or the
        # complete stored payload into the boss-facing terminal.
        return _fallback_brief(deviation)
