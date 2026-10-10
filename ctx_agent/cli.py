"""User-facing local CLI. No privileged shell or remote network endpoint."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path


def call(path, payload, timeout=900):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall((json.dumps(payload) + "\n").encode())
        data = bytearray()
        while True:
            part = sock.recv(65536)
            if not part:
                break
            data.extend(part)
            if len(data) > 4_000_000:
                raise RuntimeError("Response too large")
            if b"\n" in part:
                break
    message = json.loads(data)
    if not message["ok"]:
        raise RuntimeError(message["error"])
    return message["result"]


def _add_project_and_optional_id(parser):
    parser.add_argument("project")
    parser.add_argument("id", nargs="?")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ctx-agent")
    parser.add_argument(
        "--socket",
        default=os.environ.get("CTX_AGENT_SOCKET", "/run/ctx-agent/agent.sock"),
    )
    sub = parser.add_subparsers(dest="action", required=True)
    for cmd in ("health", "projects", "list", "pause", "resume"):
        sub.add_parser(cmd)
    for cmd in ("init", "notes"):
        item = sub.add_parser(cmd)
        item.add_argument("project")

    item = sub.add_parser("mode")
    item.add_argument("mode", choices=("quiet", "balanced", "maximum"))

    item = sub.add_parser("import")
    item.add_argument("project")
    item.add_argument("source", type=Path)

    item = sub.add_parser("submit")
    item.add_argument("project")
    item.add_argument("prompt", help="Quoted natural-language task for Ollama")

    for cmd in ("show", "events", "cancel", "diff", "apply"):
        item = sub.add_parser(cmd)
        item.add_argument("id")

    item = sub.add_parser("note")
    item.add_argument("project")
    item.add_argument("key")
    item.add_argument("text")

    item = sub.add_parser("plan-create")
    item.add_argument("project")
    item.add_argument("goal")

    item = sub.add_parser("plan-list")
    item.add_argument("project", nargs="?")

    for cmd in (
        "plan-show", "plan-approve", "plan-reject", "plan-cancel",
        "plan-diff", "plan-apply",
    ):
        _add_project_and_optional_id(sub.add_parser(cmd))

    item = sub.add_parser("deviation-list")
    item.add_argument("project", nargs="?")

    for cmd in ("deviation-show", "deviation-approve", "deviation-reject"):
        item = sub.add_parser(cmd)
        item.add_argument("id")

    args = parser.parse_args(argv)
    data = {"op": args.action.replace("-", "_")}
    for field in ("project", "prompt", "goal", "id", "key", "text", "mode"):
        if hasattr(args, field):
            value = getattr(args, field)
            if value is not None:
                data[field] = value

    try:
        if args.action == "import":
            from .snapshot import SnapshotError, import_files

            source = args.source.expanduser().resolve(strict=True)
            dest = call(args.socket, {"op": "init", "project": args.project})
            result = {
                "project": args.project,
                "copied": import_files(source, Path(dest["path"])),
            }
        else:
            timeout = 1200 if args.action == "plan-create" else 900
            result = call(args.socket, data, timeout=timeout)

        if args.action in ("diff", "plan-diff"):
            print(result["diff"])
            print("Changed:", ", ".join(result["changed"]))
            if "mutation_usage" in result:
                print(json.dumps(
                    {"mutation_usage": result["mutation_usage"]},
                    indent=2,
                    ensure_ascii=False,
                ))
        else:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print("ctx-agent:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
