"""Untrusted code runs only in a Bubblewrap mount/PID/user/network namespace."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from .runtime import Settings, MAX_TOOL_OUTPUT

# Only stdlib and Nix store paths are visible. No /home, host /run or sockets.
# Fixed script is used for structured file operations instead of shell commands.
FILE_TOOL = r'''
import json, pathlib, sys
op = sys.argv[1]
args = json.loads(sys.argv[2])
root = pathlib.Path("/work")
def target(value):
    if not isinstance(value,str) or not value or value.startswith("/"):
        raise ValueError("Expected project-relative path")
    p = (root / value).resolve(strict=False)
    if p != root and root not in p.parents:
        raise ValueError("Path leaves workspace")
    if any(n.lower() in {".env",".git",".ssh",".gnupg","secrets.json",
                         "credentials.json","id_rsa","id_ed25519","logins.json",
                         "key4.db"} or n.lower().startswith(".env.")
           or n.lower().endswith((".pem",".key",".kdbx",".p12"))
           for n in p.relative_to(root).parts):
        raise ValueError("Protected filename")
    return p
if op == "list_files":
    p = target(args.get("path","."))
    if not p.is_dir(): raise ValueError("Not a directory")
    print(json.dumps(sorted(str(x.relative_to(root))
           for x in p.iterdir() if not x.is_symlink())[:150]))
elif op == "read_file":
    p = target(args["path"])
    if not p.is_file() or p.stat().st_size > 150000:
        raise ValueError("Missing, non-text or oversized file")
    print(p.read_text(encoding="utf-8")[:12000])
elif op == "write_file":
    p = target(args["path"])
    data = args["content"]
    if not isinstance(data,str) or len(data.encode("utf-8")) > 150000:
        raise ValueError("Invalid content or oversized file")
    if p.exists():
        raise ValueError("write_file only creates new files; use apply_patch for edits")
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(data,encoding="utf-8")
    print("created", str(p.relative_to(root)))
elif op == "apply_patch":
    p = target(args["path"])
    if not p.is_file() or p.stat().st_size > 150000:
        raise ValueError("Missing, non-text or oversized file")
    expected = args["expected"]
    replacement = args["replacement"]
    if not isinstance(expected,str) or not expected:
        raise ValueError("Patch expected text must be non-empty")
    if not isinstance(replacement,str):
        raise ValueError("Patch replacement must be text")
    data = p.read_text(encoding="utf-8")
    if data.count(expected) != 1:
        raise ValueError("Patch expected text must match exactly once")
    updated = data.replace(expected,replacement,1)
    if len(updated.encode("utf-8")) > 150000:
        raise ValueError("Patched file is oversized")
    p.write_text(updated,encoding="utf-8")
    print("patched", str(p.relative_to(root)))
else:
    raise ValueError("Unknown tool")
'''


class SandboxError(RuntimeError):
    pass


def command(settings: Settings, directory: Path, arguments: list[str]):
    if not directory.is_dir() or directory.is_symlink():
        raise SandboxError("Sandbox directory missing or linked")
    # The bubblewrap binary is provided by immutable Nix store path.
    # --unshare-all includes mount, user, pid, IPC and network namespaces.
    return [
        settings.bubblewrap,
        "--die-with-parent", "--new-session", "--unshare-all",
        "--ro-bind", "/nix/store", "/nix/store",
        "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
        "--dir", "/run", "--dir", "/home", "--dir", "/work",
        "--bind", str(directory.resolve()), "/work",
        "--setenv", "HOME", "/tmp",
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        "--setenv", "PATH", "/nix/store",
        "--chdir", "/work", "--",
        *arguments,
    ]


def run(settings: Settings, directory: Path, args: list[str],
        timeout: int | None = None, cancel=None):
    timeout = min(timeout or settings.worker_timeout, settings.worker_timeout)
    argv = command(settings, directory, args)
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True, text=True,
            env={"PATH": "/usr/bin:/bin"},  # bwrap absolute path from Nix
        )
    except OSError as exc:
        raise SandboxError(f"Cannot start bubblewrap: {exc}") from exc
    try:
        # Communicate with a hard deadline; running subprocess is killed on timeout.
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise SandboxError(f"Sandbox exceeded {timeout} seconds") from exc
    if cancel is not None and cancel():
        raise SandboxError("Task cancelled")
    return {"exit_code": proc.returncode, "output": output[:MAX_TOOL_OUTPUT]}


def file_tool(settings: Settings, directory: Path, name: str, args: dict, cancel=None):
    if name not in ("list_files", "read_file", "write_file", "apply_patch"):
        raise SandboxError("Unknown file tool")
    return run(settings, directory, [settings.python, "-c",
                                     FILE_TOOL, name, json.dumps(args)],
               cancel=cancel)


def run_tests(settings: Settings, directory: Path, suite: str, cancel=None):
    if suite not in ("unittest", "compile"):
        raise SandboxError("Only predefined offline test commands are permitted")
    args = ([settings.python, "-m", "unittest", "discover", "-s", "tests", "-v"]
            if suite == "unittest" else
            [settings.python, "-m", "compileall", "-q", "."])
    return run(settings, directory, args, cancel=cancel)
