import io
import unittest
from contextlib import redirect_stdout

from ctx_agent.cli import (
    _print_decision_result,
    _print_deviation_summary,
    _print_plan_summary,
    _print_progress_report,
)


class ConciseCliTests(unittest.TestCase):
    def test_plan_summary_hides_internal_contract_noise(self):
        result = {
            "plan": {
                "id": "abc123def456",
                "target": "pi",
                "status": "awaiting_approval",
                "plan": {
                    "goal": "Rebuild the media server",
                    "milestones": ["Inspect", "Clean", "Configure nginx"],
                    "risks": ["SSH outage", "Old OS", "Low disk"],
                    "remote_budget": {
                        "allow_remote_admin": True,
                        "allow_sudo": True,
                        "allow_package_install": True,
                        "allow_network": True,
                        "allowed_domains": ["example.com"],
                    },
                    "planning": {"large": "internal"},
                },
                "context": {"history": ["very large internal history"]},
                "progress": {"waiting_on": "owner_approval"},
            }
        }
        output = io.StringIO()
        with redirect_stdout(output):
            _print_plan_summary(result)
        text = output.getvalue()
        self.assertIn("Current plan [awaiting approval]", text)
        self.assertIn("Action: ctx agent approve", text)
        self.assertNotIn("abc123def456", text)
        self.assertIn("1. Inspect", text)
        self.assertIn("Authority: remote admin, sudo, packages, network", text)
        self.assertNotIn("allowed_domains", text)
        self.assertNotIn("very large internal history", text)

    def test_deviation_summary_uses_owner_summary_not_raw_results(self):
        result = {
            "deviations": [{
                "id": "52eb43d5925f",
                "status": "pending",
                "owner_summary": {
                    "summary": "Cleanup worked; only verification failed.",
                    "recommendation": "Approve a safe verification retry.",
                    "risk": "low",
                },
                "request": {
                    "results": [{"output": "many pages of raw command output"}]
                },
            }]
        }
        output = io.StringIO()
        with redirect_stdout(output):
            _print_deviation_summary(result)
        text = output.getvalue()
        self.assertIn("Current deviation [pending]", text)
        self.assertIn("Cleanup worked; only verification failed.", text)
        self.assertIn("Risk: low", text)
        self.assertNotIn("52eb43d5925f", text)
        self.assertNotIn("many pages of raw command output", text)


    def test_progress_dashboard_is_scan_friendly(self):
        progress = {
            "phase": "awaiting_deviation",
            "current_stage_index": 2,
            "current_stage_name": "Recover disk space and verify SSH",
            "stage_count": 4,
            "percent_complete": 35,
            "elapsed_seconds": 420,
            "estimated_work_remaining_seconds": 600,
            "waiting_on": "owner_deviation_decision",
            "stages": [
                {"index": 1, "name": "Inspect", "status": "complete", "estimated_seconds": 60},
                {"index": 2, "name": "Cleanup", "status": "queued", "estimated_seconds": 120},
                {"index": 3, "name": "Configure", "status": "pending", "estimated_seconds": 300},
            ],
        }
        output = io.StringIO()
        with redirect_stdout(output):
            _print_progress_report(progress)
        text = output.getvalue()
        self.assertIn("[########----------------] 35%  awaiting deviation", text)
        self.assertIn("Stage 2/4:", text)
        self.assertIn("Action: ctx agent approve", text)

    def test_approval_output_is_one_small_result(self):
        result = {
            "plan": {
                "status": "approved",
                "progress": {"waiting_on": "maximum_mode"},
            }
        }
        output = io.StringIO()
        with redirect_stdout(output):
            _print_decision_result("remote-plan-approve", result)
        text = output.getvalue()
        self.assertEqual(
            text,
            "Plan approved.\nNext: ctx agent mode maximum\n",
        )


if __name__ == "__main__":
    unittest.main()
