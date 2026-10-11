"""Agent-side extension adapter tests (no Context Switcher installation required)."""
import os
import unittest
from unittest.mock import patch

from ctx_agent.context_extension import main


def empty_raw(payload, timeout=30):
    op = payload.get("op")
    if op == "plan_list":
        return {"plans": []}
    if op == "remote_plan_list":
        return {"plans": []}
    if op == "target_list":
        return {"targets": [{"name": "pi"}]}
    return {}


class AdapterTests(unittest.TestCase):
    def invoke(self, argv, context="research", version="1", raw=empty_raw):
        with patch.dict(os.environ, {
            "CTX_CONTEXT": context,
            "CTX_EXTENSION_API_VERSION": version,
        }):
            with patch(
                "ctx_agent.context_extension._raw",
                side_effect=raw,
            ) as raw_mock:
                with patch(
                    "ctx_agent.context_extension.agent_cli.main",
                    return_value=0,
                ) as agent:
                    with patch(
                        "ctx_agent.context_extension.media.main",
                        return_value=0,
                    ) as media:
                        result = main(argv)
                        return result, agent, media, raw_mock

    def test_context_defaults_to_active_project(self):
        result, cli, _, _ = self.invoke(["submit", "Fix the tests"])
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["submit", "research", "Fix the tests"])

    def test_explicit_project_works_without_context(self):
        result, cli, _, _ = self.invoke(
            ["submit", "--project", "gaming", "Check logs"], context=""
        )
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["submit", "gaming", "Check logs"])

    def test_import_is_scoped(self):
        result, cli, _, _ = self.invoke(["import", "/tmp/My Project"])
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["import", "research", "/tmp/My Project"])

    def test_note_scoping(self):
        result, cli, _, _ = self.invoke(["note", "priority", "testing"])
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["note", "research", "priority", "testing"])

    def test_quiet_mode_is_global(self):
        result, cli, _, _ = self.invoke(["mode", "quiet"], context="")
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["mode", "quiet"])

    def test_logs_alias(self):
        result, cli, _, _ = self.invoke(["logs", "abc123"])
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["events", "abc123"])

    def test_status_routes_to_current_remote_task_without_target_argument(self):
        def raw(payload, timeout=30):
            if payload["op"] == "plan_list":
                return {"plans": []}
            if payload["op"] == "remote_plan_list":
                return {"plans": [{
                    "id": "abc123def456",
                    "target": "pi",
                    "status": "running",
                    "created": 1,
                    "updated": 2,
                }]}
            return empty_raw(payload, timeout)

        result, cli, _, _ = self.invoke(["status"], context="", raw=raw)
        self.assertEqual(result, 0)
        cli.assert_called_once_with([
            "remote-plan-progress", "pi", "abc123def456"
        ])

    def test_status_falls_back_to_health_without_any_plan(self):
        result, cli, _, _ = self.invoke(["status"], context="", raw=empty_raw)
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["health"])

    def test_media_kept_separate(self):
        result, cli, media, _ = self.invoke(["media", "record", "start"])
        self.assertEqual(result, 0)
        cli.assert_not_called()
        media.assert_called_once_with(["record", "start"])

    def test_missing_context_requires_explicit_project_for_scoped_job(self):
        result, cli, _, _ = self.invoke(["submit", "Task"], context="")
        self.assertEqual(result, 2)
        cli.assert_not_called()

    def test_invalid_project_denied(self):
        result, cli, _, _ = self.invoke(
            ["submit", "--project", "../secret", "Task"]
        )
        self.assertEqual(result, 2)
        cli.assert_not_called()

    def test_old_api_denied(self):
        result, cli, _, _ = self.invoke(["health"], version="2")
        self.assertEqual(result, 2)
        cli.assert_not_called()

    def test_apply_is_explicit_direct_command(self):
        result, cli, _, _ = self.invoke(["apply", "job123"])
        self.assertEqual(result, 0)
        cli.assert_called_once_with(["apply", "job123"])

    def test_plan_uses_active_context_for_new_project_task(self):
        result, cli, _, _ = self.invoke(["plan", "Build the feature"])
        self.assertEqual(result, 0)
        cli.assert_called_once_with([
            "plan-create", "research", "Build the feature"
        ])

    def test_plan_without_context_uses_only_ssh_target(self):
        result, cli, _, _ = self.invoke(
            ["plan", "Set up media server"],
            context="",
        )
        self.assertEqual(result, 0)
        cli.assert_called_once_with([
            "remote-plan-create", "pi", "Set up media server"
        ])

    def test_new_plan_is_rejected_while_another_task_is_active(self):
        def raw(payload, timeout=30):
            if payload["op"] == "plan_list":
                return {"plans": [{
                    "id": "local1234567",
                    "project": "research",
                    "status": "running",
                    "created": 1,
                    "updated": 2,
                }]}
            if payload["op"] == "remote_plan_list":
                return {"plans": []}
            return empty_raw(payload, timeout)

        result, cli, _, _ = self.invoke(
            ["plan", "Another task"], raw=raw
        )
        self.assertEqual(result, 2)
        cli.assert_not_called()

    def test_approve_routes_to_current_remote_plan(self):
        def raw(payload, timeout=30):
            if payload["op"] == "plan_list":
                return {"plans": []}
            if payload["op"] == "remote_plan_list":
                return {"plans": [{
                    "id": "abc123def456",
                    "target": "pi",
                    "status": "awaiting_approval",
                    "created": 1,
                    "updated": 2,
                }]}
            return empty_raw(payload, timeout)

        result, cli, _, _ = self.invoke(["approve"], context="", raw=raw)
        self.assertEqual(result, 0)
        cli.assert_called_once_with([
            "remote-plan-approve", "pi", "abc123def456"
        ])

    def test_approve_routes_to_current_remote_deviation(self):
        def raw(payload, timeout=30):
            if payload["op"] == "plan_list":
                return {"plans": []}
            if payload["op"] == "remote_plan_list":
                return {"plans": [{
                    "id": "abc123def456",
                    "target": "pi",
                    "status": "awaiting_deviation",
                    "created": 1,
                    "updated": 2,
                }]}
            if payload["op"] == "remote_plan_show":
                return {"plan": {
                    "deviations": [{
                        "id": "52eb43d5925f",
                        "status": "pending",
                    }]
                }}
            return empty_raw(payload, timeout)

        result, cli, _, _ = self.invoke(["approve"], context="", raw=raw)
        self.assertEqual(result, 0)
        cli.assert_called_once_with([
            "remote-deviation-approve", "52eb43d5925f"
        ])

    def test_progress_routes_to_current_project_without_project_argument(self):
        def raw(payload, timeout=30):
            if payload["op"] == "plan_list":
                return {"plans": [{
                    "id": "abc123def456",
                    "project": "research",
                    "status": "running",
                    "created": 1,
                    "updated": 2,
                }]}
            if payload["op"] == "remote_plan_list":
                return {"plans": []}
            return empty_raw(payload, timeout)

        result, cli, _, _ = self.invoke(["progress"], raw=raw)
        self.assertEqual(result, 0)
        cli.assert_called_once_with([
            "plan-progress", "research", "abc123def456"
        ])

    def test_plan_show_routes_to_current_remote_task(self):
        def raw(payload, timeout=30):
            if payload["op"] == "plan_list":
                return {"plans": []}
            if payload["op"] == "remote_plan_list":
                return {"plans": [{
                    "id": "abc123def456",
                    "target": "pi",
                    "status": "running",
                    "created": 1,
                    "updated": 2,
                }]}
            return empty_raw(payload, timeout)

        result, cli, _, _ = self.invoke(["plan", "show"], context="", raw=raw)
        self.assertEqual(result, 0)
        cli.assert_called_once_with([
            "remote-plan-show", "pi", "abc123def456"
        ])

    def test_target_add_remains_available_for_one_time_setup(self):
        result, cli, _, _ = self.invoke(
            ["target", "add", "pi", "--ssh", "pi@raspberrypi"],
            context="",
        )
        self.assertEqual(result, 0)
        cli.assert_called_once_with([
            "target-add", "pi", "pi@raspberrypi"
        ])

    def test_unknown_command_rejected(self):
        result, cli, _, _ = self.invoke(["arbitrary-host-shell"])
        self.assertEqual(result, 2)
        cli.assert_not_called()


if __name__ == "__main__":
    unittest.main()
