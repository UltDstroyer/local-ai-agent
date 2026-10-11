"""Agent-side extension adapter tests (no Context Switcher installation required)."""
import os
import unittest
from unittest.mock import patch

from ctx_agent.context_extension import main


class AdapterTests(unittest.TestCase):
    def invoke(self, argv, context="research", version="1", state=None):
        if state is None:
            state = (
                ([{
                    "id": "plan123",
                    "project": context,
                    "status": "awaiting_approval",
                }] if context else []),
                [],
                [],
                [],
            )
        with patch.dict(os.environ, {"CTX_CONTEXT": context,
                                     "CTX_EXTENSION_API_VERSION": version}):
            with patch("ctx_agent.context_extension.agent_cli.main",
                       return_value=0) as agent:
                with patch(
                    "ctx_agent.context_extension._global_state",
                    return_value=state,
                ):
                    with patch("ctx_agent.context_extension.media.main",
                               return_value=0) as media:
                        result = main(argv)
                        return result, agent, media

    def test_context_defaults_to_active_project(self):
        result, cli, _ = self.invoke(["submit", "Fix the tests"])
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["submit", "research", "Fix the tests"])

    def test_explicit_project_works_without_context(self):
        result, cli, _ = self.invoke(
            ["submit", "--project", "gaming", "Check logs"], context=""
        )
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["submit", "gaming", "Check logs"])

    def test_import_is_scoped(self):
        result, cli, _ = self.invoke(["import", "/tmp/My Project"])
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["import", "research", "/tmp/My Project"])

    def test_note_scoping(self):
        result, cli, _ = self.invoke(["note", "priority", "testing"])
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["note", "research", "priority", "testing"])

    def test_quiet_mode_is_global(self):
        result, cli, _ = self.invoke(["mode", "quiet"], context="")
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["mode", "quiet"])

    def test_logs_alias(self):
        result, cli, _ = self.invoke(["logs", "abc123"])
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["events", "abc123"])

    def test_status_alias(self):
        result, cli, _ = self.invoke(["status"])
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["health"])
        result, cli, _ = self.invoke(["status", "abc123"])
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["show", "abc123"])

    def test_media_kept_separate(self):
        result, cli, media = self.invoke(["media", "record", "start"])
        self.assertEqual(result, 0)
        cli.assert_not_called()
        media.assert_called_once_with(["record", "start"])

    def test_missing_context_requires_explicit_project(self):
        result, cli, _ = self.invoke(["submit", "Task"], context="")
        self.assertEqual(result, 2)
        cli.assert_not_called()

    def test_invalid_project_denied(self):
        result, cli, _ = self.invoke(
            ["submit", "--project", "../secret", "Task"]
        )
        self.assertEqual(result, 2)
        cli.assert_not_called()

    def test_old_api_denied(self):
        result, cli, _ = self.invoke(["health"], version="2")
        self.assertEqual(result, 2)
        cli.assert_not_called()

    def test_apply_is_explicit_direct_command(self):
        result, cli, _ = self.invoke(["apply", "job123"])
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["apply", "job123"])

    def test_plan_uses_active_context(self):
        result, cli, _ = self.invoke(["plan", "Build the feature"])
        self.assertEqual(result, 0)
        cli.assert_called_once_with([
            "plan-create", "research", "Build the feature"
        ])

    def test_plan_approval_uses_active_context(self):
        result, cli, _ = self.invoke(["approve"])
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["plan-approve", "research", "plan123"])

    def test_deviation_approval_routes_directly(self):
        result, cli, _ = self.invoke(["deviation", "approve", "abc123def456"])
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["deviation-approve", "abc123def456"])

    def test_codex_login_stays_in_user_session(self):
        with patch.dict(os.environ, {
            "CTX_CONTEXT": "",
            "CTX_EXTENSION_API_VERSION": "1",
        }):
            with patch("ctx_agent.context_extension.agent_cli.main") as agent:
                with patch(
                    "ctx_agent.context_extension.codex_auth.main", return_value=0
                ) as auth:
                    result = main(["codex", "login"])
        self.assertEqual(result, 0)
        agent.assert_not_called()
        auth.assert_called_once_with(["login"])

    def test_chatgpt_is_alias_for_codex_auth(self):
        with patch.dict(os.environ, {
            "CTX_CONTEXT": "",
            "CTX_EXTENSION_API_VERSION": "1",
        }):
            with patch(
                "ctx_agent.context_extension.codex_auth.main", return_value=0
            ) as auth:
                result = main(["chatgpt", "status"])
        self.assertEqual(result, 0)
        auth.assert_called_once_with(["status"])

    def test_remote_plan_routes_to_target(self):
        result, cli, _ = self.invoke(
            ["plan", "--target", "pi", "Set up media server"],
            context="",
        )
        self.assertEqual(result, 0)
        cli.assert_called_once_with([
            "remote-plan-create", "pi", "Set up media server"
        ])

    def test_remote_approval_routes_to_target(self):
        result, cli, _ = self.invoke(
            ["approve", "--target", "pi"], context=""
        )
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["remote-plan-approve", "pi"])

    def test_target_add_routes_to_cli(self):
        result, cli, _ = self.invoke(
            ["target", "add", "pi", "--ssh", "pi@raspberrypi"],
            context="",
        )
        self.assertEqual(result, 0)
        cli.assert_called_once_with([
            "target-add", "pi", "pi@raspberrypi"
        ])

    def test_progress_uses_active_project(self):
        result, cli, _ = self.invoke(["progress"])
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["plan-progress", "research"])

    def test_remote_progress_routes_to_target(self):
        result, cli, _ = self.invoke(
            ["progress", "--target", "pi"], context=""
        )
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["remote-plan-progress", "pi"])

    def test_remote_deviation_is_separate_from_local(self):
        result, cli, _ = self.invoke(
            ["remote-deviation", "approve", "abc123def456"],
            context="",
        )
        self.assertEqual(result, 0)
        cli.assert_called_once_with([
            "remote-deviation-approve", "abc123def456"
        ])

    def test_progress_without_context_uses_single_remote_plan(self):
        state = (
            [],
            [{"id": "plan123", "target": "pi", "status": "running"}],
            [],
            [],
        )
        result, cli, _ = self.invoke(["progress"], context="", state=state)
        self.assertEqual(result, 0)
        cli.assert_called_once_with([
            "remote-plan-progress", "pi", "plan123"
        ])

    def test_deviation_without_args_shows_single_remote_pending(self):
        state = (
            [],
            [],
            [],
            [{"id": "abc123def456", "target": "pi", "status": "pending"}],
        )
        result, cli, _ = self.invoke(["deviation"], context="", state=state)
        self.assertEqual(result, 0)
        cli.assert_called_once_with([
            "remote-deviation-show", "abc123def456"
        ])

    def test_approve_without_context_prefers_pending_deviation(self):
        state = (
            [],
            [{"id": "plan123", "target": "pi", "status": "awaiting_deviation"}],
            [],
            [{"id": "abc123def456", "target": "pi", "status": "pending"}],
        )
        result, cli, _ = self.invoke(["approve"], context="", state=state)
        self.assertEqual(result, 0)
        cli.assert_called_once_with([
            "remote-deviation-approve", "abc123def456"
        ])

    def test_approve_without_context_uses_single_waiting_plan(self):
        state = (
            [],
            [{"id": "plan123", "target": "pi", "status": "awaiting_approval"}],
            [],
            [],
        )
        result, cli, _ = self.invoke(["approve"], context="", state=state)
        self.assertEqual(result, 0)
        cli.assert_called_once_with([
            "remote-plan-approve", "pi", "plan123"
        ])

    def test_unknown_command_rejected(self):
        result, cli, _ = self.invoke(["arbitrary-host-shell"])
        self.assertEqual(result, 2)
        cli.assert_not_called()


    def test_active_context_approval_prefers_pending_deviation(self):
        state = (
            [{"id": "plan123", "project": "research", "status": "approved"}],
            [],
            [{"id": "abc123def456", "project": "research", "status": "pending"}],
            [],
        )
        result, agent, _ = self.invoke(["approve"], state=state)
        self.assertEqual(result, 0)
        agent.assert_called_once_with(["deviation-approve", "abc123def456"])

    def test_active_context_still_prefers_single_remote_pending_deviation(self):
        state = (
            [{"id": "local123", "project": "research", "status": "approved"}],
            [{"id": "remote123", "target": "pi", "status": "awaiting_deviation"}],
            [],
            [{"id": "abc123def456", "target": "pi", "status": "pending"}],
        )
        result, agent, _ = self.invoke(["approve"], state=state)
        self.assertEqual(result, 0)
        agent.assert_called_once_with([
            "remote-deviation-approve", "abc123def456"
        ])


if __name__ == "__main__":
    unittest.main()
