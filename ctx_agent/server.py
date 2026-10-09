"""Unix-socket background task coordinator. No network listener or shell endpoint."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import socketserver
import threading
import time
from pathlib import Path

from .agent import run_agent
from .runtime import Settings, valid_name
from .snapshot import SnapshotError, apply, create, diff, inventory, project_path
from .store import Store


def digest_inventory(root: Path):
    return {name: hashlib.sha256(p.read_bytes()).hexdigest()
            for name, p in inventory(root).items()}


class Coordinator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.settings.projects_dir.mkdir(parents=True, exist_ok=True)
        self.jobs = self.settings.state_dir / "jobs"
        self.jobs.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.settings.state_dir / "state.sqlite3")
        self.paused = False
        self.mode = 'quiet'
        try:
            saved = json.loads((self.settings.state_dir / "resource_mode.json").read_text())
            if saved in ("quiet", "balanced", "maximum"):
                self.mode = saved
        except (OSError, ValueError):
            pass
        self.shutdown = threading.Event()
        self.lock = threading.RLock()

    def project(self, name):
        return project_path(self.settings.projects_dir, name)

    def dispatch(self, data: dict):
        op = data.get("op")
        if op == "health":
            return {"status": "running", "paused": self.paused,
                    "model": self.settings.model, "resource_mode": self.mode}
        if op == "mode":
            mode = data.get("mode")
            if mode not in ("quiet", "balanced", "maximum"):
                raise ValueError("Resource mode must be quiet, balanced or maximum")
            with self.lock:
                self.mode = mode
                (self.settings.state_dir / "resource_mode.json").write_text(json.dumps(mode))
            self.store.event(None, "resource_mode_changed", mode)
            return {"mode": mode}
        if op == "projects":
            return {"projects": sorted(x.name for x in self.settings.projects_dir.iterdir()
                                       if x.is_dir() and not x.is_symlink()
                                       and valid_name(x.name))}
        if op == "init":
            project = data.get("project", "")
            path = self.project(project)
            path.mkdir(mode=0o770, exist_ok=False)
            return {"project": project, "path": str(path)}
        if op == "submit":
            project = data.get("project", "")
            prompt = data.get("prompt", "")
            if not self.project(project).is_dir():
                raise ValueError("Unknown project; run ctx-agent init first")
            if not isinstance(prompt, str) or not 1 <= len(prompt) <= 6000:
                raise ValueError("Prompt must be 1 to 6000 characters")
            return {"id": self.store.submit(project, prompt)}
        if op == "list":
            return {"jobs": self.store.list()}
        if op in ("show", "events", "cancel", "diff", "apply"):
            jid = data.get("id", "")
            if not isinstance(jid, str) or len(jid) != 12 or not jid.isalnum():
                raise ValueError("Invalid job identifier")
            job = self.store.get(jid)
            if not job:
                raise ValueError("No such job")
            if op == "show":
                return {"job": job}
            if op == "events":
                return {"events": self.store.events(jid)}
            if op == "cancel":
                return {"cancelled": self.store.cancel(jid)}
            if job["status"] != "completed":
                raise ValueError("Job is not completed")
            source = self.project(job["project"])
            scratch = self.jobs / jid / "work"
            changed, patch = diff(source, scratch)
            if op == "diff":
                return {"changed": changed, "diff": patch}
            # Applying changes is only possible from the locally authenticated
            # Unix-socket owner CLI. It is NEVER an AI tool.
            baseline = json.loads((self.jobs / jid / "baseline.json").read_text())
            current = digest_inventory(source)
            for rel in changed:
                if current.get(rel) != baseline.get(rel):
                    raise ValueError(f"Project changed since snapshot: {rel}")
            applied = apply(source, scratch)
            self.store.event(jid, "applied_by_owner", ",".join(applied)[:1000])
            return {"applied": applied}
        if op == "pause":
            self.paused = True
            return {"paused": True, "note": "Stops picking up new tasks; active task continues."}
        if op == "resume":
            self.paused = False
            return {"paused": False}
        if op == "note":
            project, key, value = (data.get("project"), data.get("key"), data.get("text"))
            if not self.project(project).is_dir() or not valid_name(key):
                raise ValueError("Invalid project or key")
            if not isinstance(value, str) or len(value) > 2000:
                raise ValueError("Note must be at most 2000 characters")
            self.store.note(project, key, value)
            return {"saved": key}
        if op == "notes":
            project = data.get("project")
            if not self.project(project).is_dir():
                raise ValueError("Unknown project")
            return {"notes": self.store.notes(project)}
        raise ValueError("Unknown request")

    def worker(self):
        while not self.shutdown.is_set():
            try:
                if self.paused:
                    self.shutdown.wait(0.5)
                    continue
                job = self.store.next()
                if not job:
                    self.shutdown.wait(0.5)
                    continue
                jid = job["id"]
                base = self.jobs / jid
                try:
                    source = self.project(job["project"])
                    base.mkdir(mode=0o700, exist_ok=False)
                    scratch = base / "work"
                    create(source, scratch)
                    (base / "baseline.json").write_text(json.dumps(digest_inventory(source)))
                    self.store.event(jid, "snapshot_ready")
                    answer = run_agent(
                        self.settings, scratch, job["prompt"],
                        self.store.notes(job["project"]),
                        lambda kind, detail: self.store.event(jid, kind, detail),
                        lambda: self.store.cancelled(jid), self.mode,
                    )
                    if not self.store.cancelled(jid):
                        changes, _ = diff(source, scratch)
                        self.store.finish(
                            jid, "completed",
                            answer[:20000] + "\n\nChanged files: " + ", ".join(changes),
                        )
                except Exception as exc:
                    if not self.store.cancelled(jid):
                        self.store.finish(jid, "failed", error=str(exc))
            except Exception:
                # A transient DB or filesystem failure should not kill the daemon.
                self.shutdown.wait(1)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        # Socket permissions and local peer identity are the authorization
        # boundary. Never expose the Unix socket through an HTTP reverse proxy.
        raw = self.rfile.readline(65537)
        if len(raw) > 65536:
            return
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError("Request must be a JSON object")
            output = {"ok": True, "result": self.server.coordinator.dispatch(obj)}
        except (ValueError, OSError, SnapshotError, json.JSONDecodeError) as exc:
            output = {"ok": False, "error": str(exc)}
        except Exception:
            output = {"ok": False, "error": "Internal request error"}
        self.wfile.write((json.dumps(output) + "\n").encode())


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    args = p.parse_args()
    settings = Settings.load(args.config)
    coordinator = Coordinator(settings)
    settings.socket_path.parent.mkdir(parents=True, exist_ok=True)
    if settings.socket_path.exists():
        # Remove only our Unix socket, not unexpected files.
        if not settings.socket_path.is_socket():
            raise RuntimeError("Refusing to replace a non-socket path")
        settings.socket_path.unlink()
    with UnixServer(str(settings.socket_path), Handler) as server:
        server.coordinator = coordinator
        os.chmod(settings.socket_path, 0o660)
        worker = threading.Thread(target=coordinator.worker, daemon=True)
        worker.start()
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            coordinator.shutdown.set()
            worker.join(timeout=2)
            settings.socket_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
