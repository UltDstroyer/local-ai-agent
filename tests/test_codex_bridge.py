import unittest
from unittest.mock import patch

from ctx_agent.codex_bridge import BridgeError, CodexBridge, _extract_thread_id
from ctx_agent import codex_auth


class CodexBridgeTests(unittest.TestCase):
    def test_extracts_thread_id_from_json_events(self):
        output = (
            '{"type":"thread.started","thread_id":"thread-123"}\n'
            '{"type":"turn.started"}\n'
        )
        self.assertEqual(_extract_thread_id(output), "thread-123")

    def test_consult_cannot_request_shell_network(self):
        with self.assertRaises(BridgeError):
            CodexBridge().dispatch({
                "op": "consult",
                "cwd": "/var/lib/ctx-agent/projects/demo",
                "prompt": "plan only",
                "network": True,
            })

    def test_health_does_not_expose_credentials(self):
        class Result:
            returncode = 0
            stdout = "Logged in using ChatGPT\n"

        with patch("ctx_agent.codex_bridge.shutil.which", return_value="/bin/codex"):
            with patch("ctx_agent.codex_bridge.subprocess.run", return_value=Result()):
                result = CodexBridge().dispatch({"op": "health"})
        self.assertTrue(result["authenticated"])
        self.assertNotIn("token", result)


class CodexAuthTests(unittest.TestCase):
    def test_login_delegates_to_official_codex_cli(self):
        with patch("ctx_agent.codex_auth.shutil.which", return_value="/bin/codex"):
            with patch("ctx_agent.codex_auth.subprocess.call", return_value=0) as call:
                self.assertEqual(codex_auth.main(["login"]), 0)
        call.assert_called_once_with(["/bin/codex", "login"])


if __name__ == "__main__":
    unittest.main()
