"""Interactive Codex/ChatGPT authentication helpers.

These commands intentionally run in the owner's desktop session. Authentication
material remains in Codex's normal user-owned storage and is never forwarded to
the privileged/system ctx-agent daemon.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys


def _codex() -> str:
    binary = shutil.which("codex")
    if not binary:
        raise RuntimeError("Codex CLI is not installed")
    return binary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ctx-agent-codex-auth")
    parser.add_argument("action", choices=("login", "status", "logout", "doctor"))
    args = parser.parse_args(argv)
    try:
        codex = _codex()
        if args.action == "login":
            print("Opening Codex sign-in. Choose Sign in with ChatGPT when prompted.")
            command = [codex, "login"]
        elif args.action == "status":
            command = [codex, "login", "status"]
        elif args.action == "logout":
            command = [codex, "logout"]
        else:
            command = [codex, "doctor"]
        return subprocess.call(command)
    except (OSError, RuntimeError) as exc:
        print(f"ctx-agent codex: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
