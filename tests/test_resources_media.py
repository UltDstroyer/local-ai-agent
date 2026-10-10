import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ctx_agent.agent import ask
from ctx_agent.resources import ollama_options
from ctx_agent.media import enroll, mux, obs_record, synthesize
from ctx_agent.runtime import Settings


class TestResources(unittest.TestCase):
    def test_quiet_uses_some_gpu(self):
        quiet = ollama_options("quiet")
        balanced = ollama_options("balanced")
        maximum = ollama_options("maximum")
        self.assertGreater(quiet["num_gpu"], 0)
        self.assertEqual(balanced["num_gpu"], -1)
        self.assertEqual(maximum["num_gpu"], -1)
        self.assertLess(quiet["num_thread"], balanced["num_thread"])
        self.assertLess(quiet["num_ctx"], balanced["num_ctx"])
        self.assertGreater(balanced["num_batch"], quiet["num_batch"])

    def test_balanced_request_is_fast_and_kept_warm(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, _limit):
                return b'{"message":{"content":"ok"}}'

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            settings = Settings(root, root / "socket", root / "projects")
            with patch("ctx_agent.agent.urllib.request.urlopen",
                       return_value=Response()) as opened:
                ask(settings, [{"role": "user", "content": "hi"}], "balanced")
            request = opened.call_args.args[0]
            body = json.loads(request.data)
            self.assertFalse(body["think"])
            self.assertEqual(body["keep_alive"], "5m")
            self.assertEqual(body["options"]["num_gpu"], -1)

    def test_unknown_mode_denied(self):
        with self.assertRaises(ValueError):
            ollama_options("turbo-random")


class TestMedia(unittest.TestCase):
    def test_enroll_protects_reference(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)
            source = p / "sample.wav"
            source.write_bytes(b"RIFF" + b"x" * 80)
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(p / "local")}):
                target = enroll(source, "These are my spoken words.")
                self.assertEqual(target.read_bytes(), source.read_bytes())
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                self.assertEqual((target.parent / "transcript.txt").read_text(),
                                 "These are my spoken words.")

    def test_symlink_not_accepted_for_voice_enroll(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)
            (p / "source.wav").write_bytes(b"RIFF")
            (p / "link.wav").symlink_to(p / "source.wav")
            with self.assertRaises(ValueError):
                enroll(p / "link.wav", "transcript")

    def test_no_narration_before_enrollment(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(p)}):
                with self.assertRaises(ValueError):
                    synthesize("Hello there", p / "result.wav")

    def test_mux_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)
            (p / "in.mp4").write_bytes(b"x")
            (p / "voice.wav").write_bytes(b"x")
            (p / "out.mp4").write_bytes(b"x")
            with self.assertRaises(ValueError):
                mux(p / "in.mp4", p / "voice.wav", p / "out.mp4")

    def test_record_requires_owner_supplied_obs_password(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                obs_record("start")


if __name__ == "__main__":
    unittest.main()
