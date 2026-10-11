import io
import unittest
from contextlib import redirect_stdout

from ctx_agent.cli import _print_deviation_summary, _print_plan_summary


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
        self.assertIn("Plan abc123def456 [awaiting_approval]", text)
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
        self.assertIn("Cleanup worked; only verification failed.", text)
        self.assertIn("Risk: low", text)
        self.assertNotIn("many pages of raw command output", text)


if __name__ == "__main__":
    unittest.main()
