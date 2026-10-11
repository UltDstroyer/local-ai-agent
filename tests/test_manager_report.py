"""Tests for Qwen's boss-facing project reports and management policy."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ctx_agent.management import (
    DEFAULT_MANAGEMENT_CONTRACT,
    deviation_severity,
    recovery_delta,
)
from ctx_agent.manager_report import manager_progress_report


class ManagerReportTests(unittest.TestCase):
    def test_report_receives_entire_recovery_lineage(self):
        plans = [
            {
                "id": "oldplan12345",
                "status": "failed_internal",
                "goal": "Set up server",
                "current_milestone": 1,
                "created": 10,
                "updated": 20,
                "result": "nginx route failed verification",
                "plan": {"milestones": ["Inspect", "Configure nginx", "Verify"]},
                "context": {},
            },
            {
                "id": "newplan12345",
                "status": "running",
                "goal": "Recover server setup",
                "current_milestone": 1,
                "created": 21,
                "updated": 30,
                "result": "",
                "plan": {"milestones": ["Reinspect", "Use alternate route", "Verify"]},
                "context": {
                    "recovery_of_plan_id": "oldplan12345",
                    "recovery_delta": {
                        "why_replaced": "nginx route failed verification",
                    },
                },
            },
        ]
        events = [
            {"at": 11, "event": "remote_milestone_completed", "details": "Inspect"},
            {"at": 20, "event": "remote_internal_recovery_exhausted", "details": "verification failed"},
            {"at": 22, "event": "remote_recovery_plan_proposed", "details": "newplan12345"},
        ]
        deviations = [{
            "status": "approved",
            "reason": "install needed",
            "created": 15,
            "request": {
                "type": "dependency_install",
                "boss_request": "Allow cloudflared installation.",
                "risk": "medium",
                "proposed_action": {"op": "package_install"},
            },
        }]
        response = {
            "content": json.dumps({
                "headline": "Recovery is progressing",
                "status": "executing replacement plan",
                "summary": "The first strategy failed verification; the replacement preserves completed inspection work.",
                "done": ["Inspected the server"],
                "did_not_work": [{
                    "attempt": "Original nginx route",
                    "why": "Verification failed",
                    "response": "Created a replacement plan",
                }],
                "decisions": ["Owner approved cloudflared installation"],
                "remaining": ["Verify the replacement route"],
                "justification": "The replacement plan avoids repeating the failed route.",
                "timeline": [
                    {"at": "time", "event": "Inspection completed", "why": "Established current state"}
                ],
            })
        }
        with patch("ctx_agent.manager_report.ask", return_value=response) as mocked:
            report = manager_progress_report(
                SimpleNamespace(),
                plans,
                events,
                deviations,
                "balanced",
                owner_policy={"explicit_preferences": []},
            )
        self.assertEqual(report["headline"], "Recovery is progressing")
        self.assertIn("Original nginx route", report["did_not_work"][0]["attempt"])
        prompt = mocked.call_args.args[1][1]["content"]
        self.assertIn("oldplan12345", prompt)
        self.assertIn("newplan12345", prompt)
        self.assertIn("Allow cloudflared installation.", prompt)

    def test_report_has_deterministic_fallback(self):
        plans = [{
            "id": "plan123",
            "status": "running",
            "goal": "demo",
            "current_milestone": 1,
            "plan": {"milestones": ["Inspect", "Configure", "Verify"]},
            "context": {},
        }]
        with patch(
            "ctx_agent.manager_report.ask",
            side_effect=RuntimeError("qwen unavailable"),
        ):
            report = manager_progress_report(
                SimpleNamespace(),
                plans,
                [{"at": 1, "event": "milestone_completed", "details": "Inspect"}],
                [],
                "balanced",
            )
        self.assertEqual(report["done"], ["Inspect"])
        self.assertEqual(report["remaining"], ["Configure", "Verify"])
        self.assertTrue(report["justification"])


class ManagementPolicyTests(unittest.TestCase):
    def test_major_replans_require_owner_approval(self):
        self.assertEqual(
            DEFAULT_MANAGEMENT_CONTRACT["recovery"]["major_replan"],
            "create_successor_and_require_owner_approval",
        )
        self.assertTrue(
            DEFAULT_MANAGEMENT_CONTRACT["escalation"]["require_qwen_codex_consensus"]
        )

    def test_deviation_severity_distinguishes_critical(self):
        self.assertEqual(
            deviation_severity({"type": "dependency_install", "risk": "medium"}),
            "DECISION",
        )
        self.assertEqual(
            deviation_severity({"type": "critical_remote_exec", "risk": "high"}),
            "CRITICAL",
        )

    def test_recovery_delta_preserves_old_and_new_strategy(self):
        delta = recovery_delta(
            failed_stage="Configure nginx",
            completed=["Inspect"],
            old_remaining=["Configure nginx", "Verify"],
            new_milestones=["Reinspect", "Configure tunnel differently", "Verify"],
            failure="old route failed",
        )
        self.assertEqual(delta["preserved_completed_work"], ["Inspect"])
        self.assertIn("Configure nginx", delta["old_remaining_strategy"])
        self.assertIn("Configure tunnel differently", delta["new_strategy"])
        self.assertEqual(delta["why_replaced"], "old route failed")


if __name__ == "__main__":
    unittest.main()
