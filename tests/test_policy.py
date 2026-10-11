import json
import tempfile
import unittest
from pathlib import Path

from ctx_agent.policy import Policy, PolicyError


class TestPolicy(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.secret = self.base / "private"
        self.secret.mkdir()
        self.public = self.base / "workspace"
        self.public.mkdir()
        self.policy = Policy.from_dict({
            "version": 1,
            "filesystem": {
                "default": "allow",
                "protected_roots": [str(self.secret)],
                "protected_names": [".env", ".env.*", "id_ed25519", "*.pem"],
                "critical_write_roots": [str(self.base / "system")],
            },
        })

    def check(self, op, path=None, session="background"):
        return self.policy.evaluate(op, [str(path)] if path is not None else [], session)

    def test_ordinary_read_allowed(self):
        self.assertEqual(self.check("filesystem.read", self.public / "a.py").effect, "allow")

    def test_ordinary_write_allowed(self):
        self.assertEqual(self.check("filesystem.write", self.public / "a.py").effect, "allow")

    def test_protected_root_denies_read_and_write(self):
        for op in ("filesystem.read", "filesystem.write", "filesystem.delete"):
            with self.subTest(op=op):
                self.assertEqual(self.check(op, self.secret / "keys").effect, "deny")

    def test_sibling_with_similar_prefix_not_denied(self):
        sibling = self.base / "private-ordinary" / "file.txt"
        self.assertEqual(self.check("filesystem.write", sibling).effect, "allow")

    def test_secret_filenames_denied(self):
        for name in (".env", ".env.production", "id_ed25519", "key.pem"):
            with self.subTest(name=name):
                self.assertEqual(self.check("filesystem.read", self.public / name).effect, "deny")

    def test_symlink_into_protected_root_denied(self):
        (self.public / "shortcut").symlink_to(self.secret, target_is_directory=True)
        self.assertEqual(self.check("filesystem.read", self.public / "shortcut" / "key").effect, "deny")

    def test_delete_requires_approval(self):
        self.assertEqual(self.check("filesystem.delete", self.public / "note").effect, "approval_required")

    def test_critical_write_requires_approval(self):
        self.assertEqual(self.check("filesystem.write", self.base / "system" / "config").effect,
                         "approval_required")
        self.assertEqual(self.check("filesystem.read", self.base / "system" / "config").effect,
                         "allow")

    def test_desktop_always_requires_authorization(self):
        for mode in ("interactive", "background"):
            for op in ("desktop.control", "desktop.capture", "desktop.launch"):
                with self.subTest(mode=mode, op=op):
                    self.assertEqual(self.policy.evaluate(op, session=mode).effect, "approval_required")

    def test_permanent_denials(self):
        for op in ("privilege.escalate", "policy.modify", "process.memory_read",
                   "desktop.capture_background", "disk.format"):
            with self.subTest(op=op):
                self.assertEqual(self.policy.evaluate(op).effect, "deny")

    def test_commands_not_enabled_by_the_policy_checker(self):
        self.assertEqual(self.policy.evaluate("command.execute").effect, "deny")

    def test_unknown_operations_fail_closed(self):
        self.assertEqual(self.policy.evaluate("run_whatever").effect, "deny")

    def test_missing_path_fails_closed(self):
        self.assertEqual(self.policy.evaluate("filesystem.write").effect, "deny")

    def test_relative_path_fails_closed(self):
        self.assertEqual(self.check("filesystem.read", "relative/file").effect, "deny")

    def test_invalid_session_fails_closed(self):
        self.assertEqual(self.check("filesystem.read", self.public, "admin").effect, "deny")

    def test_move_checks_both_paths(self):
        decision = self.policy.evaluate("filesystem.move",
                                        [str(self.public / "file"), str(self.secret / "file")])
        self.assertEqual(decision.effect, "deny")

    def test_bad_policy_fails_closed(self):
        with self.assertRaises(PolicyError):
            Policy.from_dict({"version": 1, "filesystem": {"default": "allow"}})
        with self.assertRaises(PolicyError):
            Policy.from_dict({"version": 2})

    def test_unreadable_policy_fails_closed(self):
        with self.assertRaises(PolicyError):
            Policy.from_file(self.base / "missing.json")


if __name__ == "__main__":
    unittest.main()
