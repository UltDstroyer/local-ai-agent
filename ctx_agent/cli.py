"""User-facing local CLI. No privileged commands or network APIs."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path


def call(path, payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(20)
        sock.connect(path)
        sock.sendall((json.dumps(payload) + "\n").encode())
        data = bytearray()
        while True:
            part = sock.recv(65536)
            if not part:
                break
            data.extend(part)
            if len(data) > 2_000_000:
                raise RuntimeError("Response too large")
            if b"\n" in part:
                break
    message = json.loads(data)
    if not message["ok"]:
        raise RuntimeError(message["error"])
    return message["result"]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ctx-agent")
    parser.add_argument("--socket", default=os.environ.get(
        "CTX_AGENT_SOCKET", "/run/ctx-agent/agent.sock"))
    sub = parser.add_subparsers(dest="action", required=True)
    for cmd in ("health", "projects", "list", "pause", "resume"):
        sub.add_parser(cmd)
    for cmd in ("init", "notes"):
        x = sub.add_parser(cmd)
        x.add_argument("project")
    x = sub.add_parser("submit")
    x.add_argument("project")
    x.add_argument("prompt", help="Quoted natural-language task for Ollama")
    for cmd in ("show", "events", "cancel", "diff", "apply"):
        x = sub.add_parser(cmd)
        x.add_argument("id")
    x = sub.add_parser("note")
    x.add_argument("project")
    x.add_argument("key")
    x.add_argument("text")
    args = parser.parse_args(argv)
    data = {"op": args.action}
    for field in ("project", "prompt", "id", "key", "text"):
        if hasattr(args, field):
            data[field] = getattr(args, field)
    try:
        result = call(args.socket, data)
        if args.action == "diff":
            print(result["diff"])
            print("Changed:", ", ".join(result["changed"]))
        else:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print("ctx-agent:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
