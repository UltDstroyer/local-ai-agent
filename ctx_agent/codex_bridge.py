"""User-owned bridge to Codex CLI.

The system ctx-agent never receives ChatGPT/Codex credentials. This bridge runs
as the interactive user, owns Codex authentication, and exposes only a narrow
Unix-socket RPC for read-only consultation and workspace-scoped coding turns.
"""

from __future__ import annotations

import argparse
import grp
import json
import os
import shutil
import socket
import socketserver
import subprocess
import tempfile
import threading
from pathlib import Path

ALLOWED_ROOTS = (
    Path("/var/lib/ctx-agent/projects"),
    Path("/var/lib/ctx-agent/workspaces"),
)
MAX_PROMPT = 24_000
MAX_RESPONSE = 60_000


class BridgeError(RuntimeError):
    pass


def _within_allowed(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_dir() or resolved.is_symlink():
        raise BridgeError("Codex workspace must be a real directory")
    for root in ALLOWED_ROOTS:
        try:
            rr = root.resolve(strict=True)
        except OSError:
            continue
        if resolved == rr or rr in resolved.parents:
            return resolved
    raise BridgeError("Codex workspace is outside managed ctx-agent roots")


def _extract_thread_id(output: str) -> str:
    for raw in output.splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started":
            value = event.get("thread_id")
            if isinstance(value, str) and value:
                return value
            thread = event.get("thread")
            if isinstance(thread, dict) and isinstance(thread.get("id"), str):
                return thread["id"]
    return ""


def _run_codex(
    cwd: Path,
    prompt: str,
    *,
    writable: bool,
    thread_id: str = "",
    network: bool = False,
    timeout: int = 1200,
) -> dict:
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
        raise BridgeError("invalid Codex prompt")
    if thread_id and (len(thread_id) > 128 or any(c.isspace() for c in thread_id)):
        raise BridgeError("invalid Codex thread id")
    codex = shutil.which("codex")
    if not codex:
        raise BridgeError("Codex CLI is not installed")

    cwd = _within_allowed(cwd)
    sandbox = "workspace-write" if writable else "read-only"
    with tempfile.NamedTemporaryFile(prefix="ctx-codex-", suffix=".txt", delete=False) as tmp:
        last_message = Path(tmp.name)

    command = [
        codex, "exec",
        "--json",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--sandbox", sandbox,
        "-C", str(cwd),
        "-c", 'approval_policy="never"',
        "-o", str(last_message),
    ]
    if writable and network:
        command.extend(["-c", "sandbox_workspace_write.network_access=true"])
    if thread_id:
        command.extend(["resume", thread_id, prompt])
    else:
        command.append(prompt)

    try:
        proc = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=max(30, min(timeout, 7200)),
            env=os.environ.copy(),
        )
        output = proc.stdout[-MAX_RESPONSE:]
        try:
            message = last_message.read_text(encoding="utf-8")[:MAX_RESPONSE]
        except OSError:
            message = ""
        discovered = thread_id or _extract_thread_id(output)
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "thread_id": discovered,
            "message": message.strip(),
            "events_tail": output,
            "sandbox": sandbox,
        }
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(f"Codex turn exceeded {timeout} seconds") from exc
    finally:
        last_message.unlink(missing_ok=True)


class CodexBridge:
    def dispatch(self, request: dict) -> dict:
        op = request.get("op")
        if op == "health":
            codex = shutil.which("codex")
            if not codex:
                return {"available": False, "authenticated": False, "detail": "codex not found"}
            proc = subprocess.run(
                [codex, "login", "status"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=20,
                env=os.environ.copy(),
            )
            return {
                "available": True,
                "authenticated": proc.returncode == 0,
                "detail": proc.stdout.strip()[:4000],
            }
        if op not in ("consult", "work"):
            raise BridgeError("unsupported bridge operation")
        cwd = Path(request.get("cwd", ""))
        prompt = request.get("prompt", "")
        thread_id = request.get("thread_id", "")
        timeout = request.get("timeout", 1200)
        network = request.get("network", False)
        if type(timeout) is not int:
            raise BridgeError("timeout must be an integer")
        if type(network) is not bool:
            raise BridgeError("network must be boolean")
        if op != "work" and network:
            raise BridgeError("network access is only valid for approved work")
        return _run_codex(
            cwd,
            prompt,
            writable=(op == "work"),
            thread_id=thread_id,
            network=network,
            timeout=timeout,
        )


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        raw = self.rfile.readline(262145)
        if len(raw) > 262144:
            return
        try:
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise BridgeError("request must be an object")
            with self.server.bridge_lock:
                result = self.server.bridge.dispatch(request)
            response = {"ok": True, "result": result}
        except (BridgeError, OSError, ValueError, json.JSONDecodeError) as exc:
            response = {"ok": False, "error": str(exc)}
        except Exception:
            response = {"ok": False, "error": "internal Codex bridge error"}
        self.wfile.write((json.dumps(response) + "\n").encode())


class Server(socketserver.UnixStreamServer):
    allow_reuse_address = False


def bridge_call(path: str | Path, payload: dict, timeout: int = 1300) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall((json.dumps(payload) + "\n").encode())
        data = bytearray()
        while True:
            part = sock.recv(65536)
            if not part:
                break
            data.extend(part)
            if len(data) > 1_000_000:
                raise BridgeError("bridge response too large")
            if b"\n" in part:
                break
    response = json.loads(data)
    if not response.get("ok"):
        raise BridgeError(response.get("error", "Codex bridge failed"))
    return response["result"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ctx-agent-codex-bridge")
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--group", default="ctx-agent-users")
    args = parser.parse_args(argv)

    try:
        gid = grp.getgrnam(args.group).gr_gid
    except KeyError:
        return 0
    if gid not in os.getgroups() and os.getegid() != gid:
        return 0

    args.socket.parent.mkdir(parents=True, exist_ok=True)
    if args.socket.exists():
        if not args.socket.is_socket():
            raise BridgeError("refusing to replace non-socket bridge path")
        args.socket.unlink()

    bridge = CodexBridge()
    with Server(str(args.socket), Handler) as server:
        server.bridge = bridge
        server.bridge_lock = threading.RLock()
        os.chmod(args.socket, 0o660)
        os.chown(args.socket, -1, gid)
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            args.socket.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
