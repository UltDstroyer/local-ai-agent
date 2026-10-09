"""Context Switcher Extension API v1 entrypoint, owned by local-ai-agent.

Core ctx only discovers ctx-ext-agent and sets CTX_CONTEXT. All AI routing
lives here. It never changes context lifecycle or grants desktop privileges.
"""

from __future__ import annotations

import os
import re
import sys

from . import cli as agent_cli
from . import media
from .runtime import valid_name

SCOPED = frozenset({"init", "import", "submit", "note", "notes"})
DIRECT = frozenset({
    "health", "projects", "list", "pause", "resume", "mode",
    "events", "show", "diff", "apply", "cancel",
})

HELP = """ctx-agent extension (Extension API v1)

Commands:
  ctx agent health | status [JOB_ID]
  ctx agent projects | list
  ctx agent init [--project NAME]
  ctx agent import [--project NAME] SOURCE_DIRECTORY
  ctx agent submit [--project NAME] "TASK"
  ctx agent note [--project NAME] KEY "TEXT"
  ctx agent notes [--project NAME]
  ctx agent mode quiet|balanced|maximum
  ctx agent events|logs|show|diff|apply|cancel JOB_ID
  ctx agent pause | resume
  ctx agent media record|enroll|narrate|mux ...

Project-scoped commands default to the active ctx context, if available.
Use --project NAME to override. Queued tasks continue across ctx switches.
Apply modifies an agent-managed project copy, not the original Git checkout.
Desktop recording is initiated explicitly, never through background capture.
"""


def fail(message: str) -> int:
    print(f"ctx-ext-agent: {message}", file=sys.stderr)
    return 2


def select_project(rest: list[str]) -> tuple[str, list[str]]:
    if rest[:1] == ["--project"]:
        if len(rest) < 2:
            raise ValueError("--project requires a name")
        name, tail = rest[1], rest[2:]
    else:
        name = os.environ.get("CTX_CONTEXT", "")
        tail = rest
    if not name:
        raise ValueError("no active ctx context; provide --project NAME")
    if not valid_name(name):
        raise ValueError("invalid agent project name (1-48 letters, digits, _ or -)")
    return name, tail


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("help", "--help", "-h"):
        print(HELP)
        return 0

    # Only versioned ctx invocation supplies this env variable. The variable
    # does not confer permissions; it merely announces the interface.
    version = os.environ.get("CTX_EXTENSION_API_VERSION")
    if version is not None and version != "1":
        return fail("unsupported Context Switcher extension API version")

    action, rest = argv[0], argv[1:]
    try:
        if action in SCOPED:
            project, args = select_project(rest)
            required = {"init": 0, "notes": 0, "import": 1,
                        "submit": 1, "note": 2}[action]
            if len(args) != required or (action == "submit" and not args[0]):
                return fail(f"invalid arguments for {action}; use ctx agent help")
            return agent_cli.main([action, project, *args])

        if action == "status":
            if len(rest) > 1:
                return fail("usage: ctx agent status [JOB_ID]")
            return agent_cli.main(["show", rest[0]] if rest else ["health"])

        if action == "logs":
            if len(rest) != 1:
                return fail("usage: ctx agent logs JOB_ID")
            return agent_cli.main(["events", rest[0]])

        if action == "media":
            if not rest:
                return fail("usage: ctx agent media record|enroll|narrate|mux ...")
            return media.main(rest)

        if action in DIRECT:
            return agent_cli.main([action, *rest])

        return fail(f"unknown command {action!r}; use ctx agent help")
    except ValueError as exc:
        return fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
