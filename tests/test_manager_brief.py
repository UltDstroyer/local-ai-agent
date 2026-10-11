"""Tests for the Qwen manager-to-boss deviation boundary."""

import io
import json
import unittest
from pathlib import Path
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from ctx_agent.cli import _print_live_deviation, _prompt_human_input
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
                "request": "Allow a journal check for jellyfin.service.",
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
        self.assertEqual(
            brief["request"],
            "Allow a journal check for jellyfin.service.",
        )
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

    def test_failed_manager_summary_shows_only_current_deviation_fields(self):
        deviation = {
            "reason": "service restart needs approval",
            "request": {
                "error": "nginx restart failed once",
                "suggested_action": "Approve one retry.",
                "risk": "low",
                "history": ["old unrelated event", "another old event"],
            },
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
        self.assertIn("service restart needs approval", brief["summary"])
        self.assertIn("nginx restart failed once", brief["summary"])
        self.assertNotIn("old unrelated event", brief["summary"])
        self.assertEqual(brief["risk"], "low")
        self.assertEqual(brief["recommendation"], "Approve one retry.")


if __name__ == "__main__":
    unittest.main()


class ConcreteManagerDecisionTests(unittest.TestCase):
    def test_fallback_names_structured_service_request(self):
        deviation = {
            "reason": "check references unapproved service: jellyfin.service",
            "request": {
                "proposed_action": {
                    "op": "check",
                    "check": "journal_tail",
                    "service": "jellyfin.service",
                },
                "risk": "low",
            },
        }
        with patch(
            "ctx_agent.manager_brief.ask",
            side_effect=RuntimeError("ollama unavailable"),
        ):
            brief = manager_deviation_brief(
                SimpleNamespace(model="qwen3:8b"), deviation, "balanced"
            )
        self.assertEqual(
            brief["request"],
            "Allow the requested journal_tail on jellyfin.service.",
        )




class BossDecisionOutputTests(unittest.TestCase):
    def test_cli_reports_approval_before_watcher(self):
        text = (
            Path(__file__).resolve().parents[1]
            .joinpath("ctx_agent", "cli.py")
            .read_text()
        )
        approved = text.index('print("[Qwen manager] approved"')
        watcher = text.index("_follow_approved_plan(args.socket, plan, remote=remote)")
        self.assertLess(approved, watcher)


class HumanInputPromptTests(unittest.TestCase):
    def test_sensitive_prompt_uses_hidden_input(self):
        request = {
            "prompt": "Cloudflare token",
            "reason": "Needed for the existing tunnel.",
            "sensitive": True,
        }
        with patch("ctx_agent.cli.getpass.getpass", return_value="hidden") as mocked:
            value = _prompt_human_input(request)
        self.assertEqual(value, "hidden")
        mocked.assert_called_once()

    def test_non_sensitive_prompt_uses_standard_input(self):
        request = {
            "prompt": "Tunnel name",
            "reason": "Needed to select the existing tunnel.",
            "sensitive": False,
        }
        with patch("builtins.input", return_value="media") as mocked:
            value = _prompt_human_input(request)
        self.assertEqual(value, "media")
        mocked.assert_called_once()
