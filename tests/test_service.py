import json
import tempfile
import unittest
from pathlib import Path

from ctx_agent.runtime import Settings, sensitive, valid_name
from ctx_agent.snapshot import SnapshotError, apply, create, diff, project_path, import_files
from ctx_agent.sandbox import command, file_tool, SandboxError
from ctx_agent.server import Coordinator
from ctx_agent.store import Store


class TestRuntime(unittest.TestCase):
    def test_names(self):
        self.assertTrue(valid_name("ctx-test_1"))
        for bad in ("", "../private", "a/b", "hello world", "-leading"):
            self.assertFalse(valid_name(bad))

    def test_secret_names(self):
        for name in (".env", ".env.local", "id_ed25519", "a.pem", "token.json"):
            self.assertTrue(sensitive(name))

    def test_config_requires_loopback(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "settings.json"
            p.write_text(json.dumps({
                "state_dir": td, "socket_path": td + "/socket",
                "projects_dir": td + "/projects",
                "ollama_url": "http://evil.example/api/chat",
            }))
            with self.assertRaises(ValueError):
                Settings.load(p)


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.source = self.root / "project"
        self.source.mkdir()
        self.scratch = self.root / "work"

    def test_ignores_secrets_symlinks(self):
        (self.source / "main.py").write_text("print('hi')\n")
        (self.source / ".env").write_text("SECRET=123")
        (self.source / ".git").mkdir()
        (self.source / ".git" / "config").write_text("credentials")
        (self.source / "link.py").symlink_to(self.source / ".env")
        create(self.source, self.scratch)
        self.assertTrue((self.scratch / "main.py").exists())
        self.assertFalse((self.scratch / ".env").exists())
        self.assertFalse((self.scratch / "link.py").exists())
        self.assertFalse((self.scratch / ".git").exists())

    def test_diff_and_apply_only_non_deletion(self):
        (self.source / "main.py").write_text("original\n")
        (self.source / "untouched.py").write_text("keep\n")
        create(self.source, self.scratch)
        (self.scratch / "main.py").write_text("updated\n")
        (self.scratch / "untouched.py").unlink()
        (self.scratch / "new.py").write_text("new\n")
        changed, patch = diff(self.source, self.scratch)
        self.assertEqual(changed, ["main.py", "new.py"])
        self.assertIn("+updated", patch)
        self.assertEqual(apply(self.source, self.scratch), changed)
        self.assertEqual((self.source / "main.py").read_text(), "updated\n")
        self.assertTrue((self.source / "untouched.py").exists())

    def test_import_filters_private_files(self):
        dest = self.root / "imported"
        dest.mkdir()
        (self.source / "public.py").write_text("print(42)")
        (self.source / ".env").write_text("PASSWORD=secret")
        paths = import_files(self.source, dest)
        self.assertEqual(paths, ["public.py"])
        self.assertFalse((dest / ".env").exists())

    def test_project_path_rejects_traversal(self):
        with self.assertRaises(SnapshotError):
            project_path(self.root, "../outside")

    def test_snapshot_rejects_symlink_root(self):
        (self.source / "main.py").write_text("x")
        (self.root / "linked").symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(SnapshotError):
            create(self.root / "linked", self.scratch)


class TestStore(unittest.TestCase):
    def test_queue_and_restart(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "db.sqlite3"
            store = Store(path)
            a = store.submit("p", "task")
            self.assertEqual(store.next()["id"], a)
            self.assertEqual(store.get(a)["status"], "running")
            store.db.close()
            other = Store(path)
            self.assertEqual(other.get(a)["status"], "interrupted")

    def test_cancel(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "db")
            a = store.submit("p", "task")
            self.assertTrue(store.cancel(a))
            self.assertIsNone(store.next())


class TestCoordinator(unittest.TestCase):
    def test_init_submit_and_pause(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            c = Coordinator(Settings(root, root / "agent.sock", root / "projects"))
            c.dispatch({"op": "init", "project": "demo"})
            result = c.dispatch({"op": "submit", "project": "demo", "prompt": "Help"})
            self.assertEqual(c.dispatch({"op": "show", "id": result["id"]})["job"]["status"],
                             "queued")
            self.assertTrue(c.dispatch({"op": "pause"})["paused"])
            self.assertFalse(c.dispatch({"op": "resume"})["paused"])
            self.assertTrue(c.dispatch({"op": "cancel", "id": result["id"]})["cancelled"])

    def test_only_one_plan_can_be_active_globally(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            c = Coordinator(Settings(root, root / "agent.sock", root / "projects"))
            c.store.create_plan(
                "demo",
                "First task",
                {"id": "abc123def456"},
            )
            with self.assertRaisesRegex(ValueError, "another plan is already active"):
                c._require_idle_for_new_plan()

    def test_apply_requires_completed_job(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            c = Coordinator(Settings(root, root / "agent.sock", root / "projects"))
            c.dispatch({"op": "init", "project": "demo"})
            job = c.dispatch({"op": "submit", "project": "demo", "prompt": "Task"})
            with self.assertRaises(ValueError):
                c.dispatch({"op": "apply", "id": job["id"]})


class TestSandbox(unittest.TestCase):
    def test_fixed_namespace_prefix_and_no_network(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = Settings(root, root / "socket", root / "projects")
            argv = command(config, root, ["python", "-V"])
            self.assertIn("--unshare-all", argv)
            self.assertIn("--ro-bind", argv)
            self.assertNotIn("--share-net", argv)
            self.assertEqual(argv[-2:], ["python", "-V"])

    def test_tools_are_known_and_fixed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = Settings(root, root / "socket", root / "projects")
            with self.assertRaises(SandboxError):
                file_tool(config, root, "shell", {"command": "rm -rf /"})


if __name__ == "__main__":
    unittest.main()
