"""Boss-facing deviation summaries written by the local manager model."""

from __future__ import annotations

import json

from .agent import ask


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
        risk = str(value.get("risk", "medium")).lower()
        if risk not in ("low", "medium", "high"):
            risk = "medium"
        return {
            "summary": str(value.get("summary", "")).strip()[:700],
            "risk": risk,
            "recommendation": str(value.get("recommendation", "")).strip()[:500],
            "decision": str(value.get("decision", "")).strip()[:500],
        }
    except Exception:
        # Never leak employee/tool internals to the boss if manager summarization
        # is unavailable. Keep the raw payload in the audit store for later review.
        return {
            "summary": (
                "I hit a deviation that needs your decision, but I could not "
                "produce a reliable manager summary."
            ),
            "risk": "medium",
            "recommendation": "I recommend holding this step until I can summarize it cleanly.",
            "decision": "Reject or defer this deviation for now?",
        }
