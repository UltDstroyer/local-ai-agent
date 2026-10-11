"""Tests for the Qwen manager-to-boss deviation boundary."""

import io
import json
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from ctx_agent.cli import _print_live_deviation
from ctx_agent.manager_brief import manager_deviation_brief


class ManagerBriefTests(unittest.TestCase):
    def test_qwen_brief_hides_raw_employee_noise(self):
        deviation = {
            "id": "abc123def456",
            "reason": "Codex proposed invalid remote actions",
            "request": {
                "error": "validator stack trace and low-level SSH output",
                "suggested_action": "debug internal bridge",
            },
            "target": "pi",
            "plan": "plan123",
        }
        response = {
            "content": json.dumps({
                "summary": "I need a slightly broader service inspection scope to continue.",
                "risk": "low",
                "recommendation": "I recommend approving it.",
                "decision": "May I approve this narrow scope increase?",
            })
        }
        with patch("ctx_agent.manager_brief.ask", return_value=response) as mocked:
            brief = manager_deviation_brief(
                SimpleNamespace(model="qwen3:8b"),
                deviation,
                "balanced",
            )
        self.assertEqual(brief["risk"], "low")
        self.assertIn("broader service inspection", brief["summary"])
        prompt = mocked.call_args.args[1][1]["content"]
        self.assertIn("Codex proposed invalid remote actions", prompt)

        output = io.StringIO()
        with redirect_stderr(output):
            _print_live_deviation(deviation, brief)
        text = output.getvalue()
        self.assertIn("[Qwen manager]", text)
        self.assertIn("broader service inspection", text)
        self.assertNotIn("Codex proposed invalid remote actions", text)
        self.assertNotIn("validator stack trace", text)
        self.assertNotIn("debug internal bridge", text)

    def test_failed_manager_summary_does_not_leak_raw_payload(self):
        deviation = {
            "reason": "secret low-level failure details",
            "request": {"error": "raw tool output"},
        }
        with patch(
            "ctx_agent.manager_brief.ask",
            side_effect=RuntimeError("ollama unavailable"),
        ):
            brief = manager_deviation_brief(
                SimpleNamespace(model="qwen3:8b"),
                deviation,
                "balanced",
            )
        self.assertNotIn("secret low-level failure", brief["summary"])
        self.assertNotIn("raw tool output", brief["summary"])
        self.assertEqual(brief["risk"], "medium")


if __name__ == "__main__":
    unittest.main()
