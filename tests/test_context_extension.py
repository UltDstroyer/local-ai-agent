"""Agent-side extension adapter tests (no Context Switcher installation required)."""
import os
import unittest
from unittest.mock import patch

from ctx_agent.context_extension import main


class AdapterTests(unittest.TestCase):
    def invoke(self, argv, context="research", version="1"):
        with patch.dict(os.environ, {"CTX_CONTEXT": context,
                                     "CTX_EXTENSION_API_VERSION": version}):
            with patch("ctx_agent.context_extension.agent_cli.main",
                       return_value=0) as agent:
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

    def test_unknown_command_rejected(self):
        result, cli, _ = self.invoke(["arbitrary-host-shell"])
        self.assertEqual(result, 2)
        cli.assert_not_called()


if __name__ == "__main__":
    unittest.main()
