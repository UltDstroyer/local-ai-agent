"""Tests for the Qwen manager-to-boss deviation boundary."""

import io
import json
import unittest
from pathlib import Path
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from ctx_agent.cli import (
    _follow_approved_plan,
    _print_live_deviation,
    _print_progress_report,
    _prompt_human_input,
    _show_pending_deviation,
    _wait_for_recovery_successor,
    main as cli_main,
)
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


class PlainBossRequestTests(unittest.TestCase):
    def test_raw_input_request_never_becomes_request_line(self):
        deviation = {
            "reason": "Qwen requests one owner decision",
            "request": {
                "boss_request": {
                    "key": "cloudflared_status",
                    "prompt": "Is cloudflared running?",
                },
                "input_request": {
                    "key": "cloudflare_token",
                    "prompt": "Cloudflare token",
                    "reason": "Needed to authenticate.",
                    "sensitive": True,
                },
            },
        }
        response = {
            "content": json.dumps({
                "summary": "Authentication requires one human-only credential.",
                "request": "Allow me to request the Cloudflare credential.",
                "risk": "medium",
                "recommendation": "Approve if this is the intended tunnel.",
                "decision": "May I request that credential?",
            })
        }
        with patch("ctx_agent.manager_brief.ask", return_value=response):
            brief = manager_deviation_brief(
                SimpleNamespace(model="qwen3:8b"), deviation, "balanced"
            )
        self.assertIsInstance(brief["request"], str)
        self.assertNotIn("{", brief["request"])
        self.assertNotIn("cloudflared_status", brief["request"])


class LiveDeviationWatcherTests(unittest.TestCase):
    def test_auto_retired_deviation_does_not_stop_live_watcher(self):
        snapshots = [
            {
                "phase": "awaiting_deviation",
                "current_stage_index": 1,
                "percent_complete": 50,
                "waiting_on": "owner_deviation_decision",
            },
            {
                "phase": "running",
                "current_stage_index": 1,
                "percent_complete": 50,
                "waiting_on": None,
            },
            {
                "phase": "awaiting_deviation",
                "current_stage_index": 1,
                "percent_complete": 50,
                "waiting_on": "owner_deviation_decision",
            },
        ]
        calls = {"progress": 0, "show": 0}

        def fake_call(path, payload, timeout=900):
            if payload["op"] == "remote_plan_progress":
                idx = min(calls["progress"], len(snapshots) - 1)
                calls["progress"] += 1
                return {"progress": snapshots[idx]}
            raise AssertionError(payload)

        def fake_show(*args, **kwargs):
            calls["show"] += 1
            return calls["show"] >= 2

        with patch("ctx_agent.cli.call", side_effect=fake_call), patch(
            "ctx_agent.cli._show_pending_deviation", side_effect=fake_show
        ), patch("ctx_agent.cli.time.sleep", return_value=None):
            _follow_approved_plan(
                "/tmp/fake.sock",
                {"id": "plan123", "target": "pi"},
                remote=True,
            )

        self.assertEqual(calls["show"], 2)
        self.assertGreaterEqual(calls["progress"], 3)


    def test_sanitized_deviation_summary_still_matches_live_plan(self):
        calls = []

        def fake_call(path, payload, timeout=900):
            calls.append(payload)
            if payload["op"] == "remote_deviation_list":
                return {
                    "deviations": [{
                        "id": "abc123def456",
                        "plan": "plan123",
                        "target": "pi",
                        "status": "pending",
                    }]
                }
            if payload["op"] == "remote_deviation_show":
                return {
                    "deviation": {
                        "id": "abc123def456",
                        "plan": "plan123",
                        "target": "pi",
                        "status": "pending",
                    },
                    "brief": {
                        "summary": "Need one owner decision.",
                        "request": "Allow the requested action.",
                        "risk": "low",
                        "recommendation": "Approve.",
                        "decision": "May I continue?",
                    },
                }
            raise AssertionError(payload)

        with patch("ctx_agent.cli.call", side_effect=fake_call), patch(
            "ctx_agent.cli._print_live_deviation"
        ) as shown:
            result = _show_pending_deviation(
                "/tmp/fake.sock",
                remote=True,
                scope="pi",
                plan_id="plan123",
            )

        self.assertTrue(result)
        shown.assert_called_once()
        self.assertEqual(calls[0]["op"], "remote_deviation_list")
        self.assertEqual(calls[1]["op"], "remote_deviation_show")

    def test_deviation_approval_does_not_attach_second_follower(self):
        with patch(
            "ctx_agent.cli.call",
            return_value={
                "plan": {
                    "id": "plan123",
                    "target": "pi",
                    "status": "approved",
                }
            },
        ), patch("ctx_agent.cli._follow_approved_plan") as follow:
            rc = cli_main([
                "--socket",
                "/tmp/fake.sock",
                "remote-deviation-approve",
                "abc123def456",
            ])

        self.assertEqual(rc, 0)
        follow.assert_not_called()


class StructuredProgressReportTests(unittest.TestCase):
    def test_live_report_shows_all_stage_states_cleanly(self):
        progress = {
            "phase": "executing",
            "current_stage_index": 2,
            "current_stage_name": "Configure nginx",
            "stage_count": 4,
            "percent_complete": 50,
            "elapsed_seconds": 90,
            "estimated_remaining_seconds": 180,
            "stages": [
                {"index": 1, "name": "Inspect", "status": "complete", "estimated_seconds": 30},
                {"index": 2, "name": "Configure nginx", "status": "running", "estimated_seconds": 90},
                {"index": 3, "name": "Verify tunnel", "status": "queued", "estimated_seconds": 60},
                {"index": 4, "name": "Final verification", "status": "pending", "estimated_seconds": 60},
            ],
            "estimate_note": "Rough work-time estimate.",
        }
        output = io.StringIO()
        _print_progress_report(progress, file=output)
        text = output.getvalue()
        self.assertIn("[Qwen manager] progress", text)
        self.assertIn("phase: executing", text)
        self.assertIn("[done] 1. Inspect", text)
        self.assertIn("[active] 2. Configure nginx", text)
        self.assertIn("[queued] 3. Verify tunnel", text)
        self.assertIn("[pending] 4. Final verification", text)



class RecoveryPlanningProgressTests(unittest.TestCase):
    def test_recovery_handoff_explains_failure_then_shows_new_plan_progress(self):
        calls = {"progress": 0, "list": 0}

        failed = {
            "id": "oldplan12345",
            "target": "pi",
            "status": "failed_internal",
            "result": "nginx verification repeatedly failed",
            "current_milestone": 1,
            "plan": {
                "milestones": [
                    "Inspect server",
                    "Repair nginx",
                    "Verify tunnel",
                ]
            },
            "context": {},
        }
        successor = {
            "id": "newplan12345",
            "target": "pi",
            "status": "awaiting_approval",
            "goal": "Failed plan ID: oldplan12345 recovery",
            "current_milestone": 0,
            "plan": {
                "milestones": ["Reinspect", "Apply different repair"],
                "remote_budget": {},
            },
            "context": {"recovery_of_plan_id": "oldplan12345"},
        }

        def fake_call(path, payload, timeout=900):
            if payload["op"] == "remote_plan_list":
                calls["list"] += 1
                if calls["list"] == 1:
                    return {"plans": [failed]}
                return {"plans": [successor, failed]}
            if payload["op"] == "remote_plan_progress":
                calls["progress"] += 1
                return {
                    "progress": {
                        "phase": "planning",
                        "current_stage_index": 2,
                        "current_stage_name": "Codex drafts plan",
                        "stage_count": 4,
                        "percent_complete": 25,
                        "elapsed_seconds": 12,
                        "estimated_remaining_seconds": 30,
                        "planning_stages": [
                            {"index": 1, "name": "Inspect target", "status": "complete", "estimated_seconds": 5},
                            {"index": 2, "name": "Codex drafts plan", "status": "running", "estimated_seconds": 10},
                            {"index": 3, "name": "Qwen narrows scope", "status": "pending", "estimated_seconds": 10},
                            {"index": 4, "name": "Codex confirms contract", "status": "pending", "estimated_seconds": 10},
                        ],
                    }
                }
            raise AssertionError(payload)

        output = io.StringIO()
        with patch("ctx_agent.cli.call", side_effect=fake_call), patch(
            "ctx_agent.cli.time.sleep", return_value=None
        ), redirect_stderr(output), patch("sys.stdout", output):
            result = _wait_for_recovery_successor(
                "/tmp/fake.sock",
                remote=True,
                scope="pi",
                failed_plan_id="oldplan12345",
            )

        text = output.getvalue()
        self.assertEqual(result["id"], "newplan12345")
        self.assertIn("[Qwen manager] A new plan is required", text)
        self.assertIn("failed stage: Repair nginx", text)
        self.assertIn("reason: nginx verification repeatedly failed", text)
        self.assertIn("[Qwen manager] replacement plan progress", text)
        self.assertIn("[done] 1. Inspect target", text)
        self.assertIn("[active] 2. Codex drafts plan", text)
        self.assertIn("Recovery plan ready for your decision", text)



if __name__ == "__main__":
    unittest.main()
