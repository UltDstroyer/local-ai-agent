"""Context Switcher Extension API v1 entrypoint, owned by local-ai-agent.

Core ctx only discovers ctx-ext-agent and sets CTX_CONTEXT. AI routing and
approval semantics live here; Context Switcher itself grants no extra privilege.
"""

from __future__ import annotations

import os
import sys

from . import cli as agent_cli
from . import codex_auth
from . import media
from .runtime import valid_name

SCOPED = frozenset({"init", "import", "submit", "note", "notes"})
DIRECT = frozenset({
    "health", "projects", "list", "pause", "resume", "mode",
    "events", "show", "diff", "apply", "cancel",
})

HELP = """ctx-agent extension (Extension API v1)

Quick jobs:
  ctx agent health | status [JOB_ID]
  ctx agent projects | list
  ctx agent init [--project NAME]
  ctx agent import [--project NAME] SOURCE_DIRECTORY
  ctx agent submit [--project NAME] "TASK"
  ctx agent note [--project NAME] KEY "TEXT"
  ctx agent notes [--project NAME]
  ctx agent mode quiet|balanced|maximum
  ctx agent events|logs|show|diff|apply|cancel JOB_ID

Codex project workflow:
  ctx agent codex login|status|logout|doctor
  ctx agent chatgpt login|status|logout
  ctx agent plan [--project NAME] "GOAL"
  ctx agent plan [--project NAME] show|diff|apply|cancel [PLAN_ID]
  ctx agent plan list [--project NAME]
  ctx agent progress [--project NAME | --target NAME] [PLAN_ID]
  ctx agent approve [--project NAME] [PLAN_ID]
  ctx agent reject [--project NAME] [PLAN_ID]
  ctx agent deviations [--project NAME]
  ctx agent deviation show|approve|reject DEVIATION_ID

SSH targets and remote projects:
  ctx agent target add NAME --ssh USER@HOST [--port PORT]
  ctx agent target list|show|inspect|remove [NAME]
  ctx agent plan --target NAME "GOAL"
  ctx agent plan --target NAME show|cancel|events [PLAN_ID]
  ctx agent approve --target NAME [PLAN_ID]
  ctx agent reject --target NAME [PLAN_ID]
  ctx agent deviations --target NAME
  ctx agent remote-deviation show|approve|reject DEVIATION_ID

Media:
  ctx agent media record|enroll|narrate|mux ...

Plan creation is read-only. ctx-agent and Codex negotiate a mutation budget,
then wait for owner approval. Approved plans execute autonomously only in
maximum mode. Codex may ask Qwen for narrow scope expansion; Qwen can approve
reasonable typed expansions and escalates high-risk or ambiguous requests to
the owner. Approval terminals stream progress and print owner deviations as
soon as they occur. Codex/ChatGPT authentication stays in the desktop user's
own Codex storage.
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


def _plan(rest: list[str]) -> int:
    # Global/local/remote plan listing does not require an active context.
    if rest[:1] == ["list"]:
        if len(rest) == 1:
            context = os.environ.get("CTX_CONTEXT", "")
            return agent_cli.main(["plan-list", context] if context else ["plan-list"])
        if len(rest) == 3 and rest[1] == "--project":
            return agent_cli.main(["plan-list", rest[2]])
        if len(rest) == 3 and rest[1] == "--target":
            return agent_cli.main(["remote-plan-list", rest[2]])
        return fail(
            "usage: ctx agent plan list [--project NAME | --target NAME]"
        )

    if rest[:1] == ["--target"]:
        if len(rest) < 3:
            return fail('usage: ctx agent plan --target NAME "GOAL"')
        target, args = rest[1], rest[2:]
        if not valid_name(target):
            return fail("invalid SSH target name")
        if args[0] in ("show", "cancel", "events"):
            action = "remote-plan-" + args[0]
            if len(args) > 2:
                return fail(
                    f"usage: ctx agent plan --target NAME {args[0]} [PLAN_ID]"
                )
            return agent_cli.main([action, target, *args[1:]])
        if len(args) != 1 or not args[0]:
            return fail('usage: ctx agent plan --target NAME "GOAL"')
        return agent_cli.main(["remote-plan-create", target, args[0]])

    project, args = select_project(rest)
    if not args:
        return fail('usage: ctx agent plan [--project NAME] "GOAL"')
    if args[0] in ("show", "diff", "apply", "cancel"):
        action = "plan-" + args[0]
        if len(args) > 2:
            return fail(f"usage: ctx agent plan {args[0]} [PLAN_ID]")
        return agent_cli.main([action, project, *args[1:]])
    if len(args) != 1 or not args[0]:
        return fail('usage: ctx agent plan [--project NAME] "GOAL"')
    return agent_cli.main(["plan-create", project, args[0]])


def _progress(rest: list[str]) -> int:
    if rest[:1] == ["--target"]:
        if len(rest) not in (2, 3):
            return fail(
                "usage: ctx agent progress --target NAME [PLAN_ID]"
            )
        target = rest[1]
        if not valid_name(target):
            return fail("invalid SSH target name")
        return agent_cli.main([
            "remote-plan-progress", target, *rest[2:]
        ])

    if rest[:1] == ["--project"]:
        if len(rest) not in (2, 3):
            return fail(
                "usage: ctx agent progress --project NAME [PLAN_ID]"
            )
        project = rest[1]
        if not valid_name(project):
            return fail("invalid project name")
        return agent_cli.main([
            "plan-progress", project, *rest[2:]
        ])

    if len(rest) > 1:
        return fail(
            "usage: ctx agent progress [--project NAME | --target NAME] [PLAN_ID]"
        )
    project = os.environ.get("CTX_CONTEXT", "")
    if not project:
        return fail(
            "no active ctx context; provide --project NAME or --target NAME"
        )
    if not valid_name(project):
        return fail("invalid agent project name")
    return agent_cli.main(["plan-progress", project, *rest])


def _approval(action: str, rest: list[str]) -> int:
    if rest[:1] == ["--target"]:
        if len(rest) < 2 or len(rest) > 3:
            return fail(
                f"usage: ctx agent {action} --target NAME [PLAN_ID]"
            )
        target = rest[1]
        if not valid_name(target):
            return fail("invalid SSH target name")
        return agent_cli.main(
            ["remote-plan-" + action, target, *rest[2:]]
        )
    project, args = select_project(rest)
    if len(args) > 1:
        return fail(f"usage: ctx agent {action} [--project NAME] [PLAN_ID]")
    return agent_cli.main(["plan-" + action, project, *args])


def _deviation(rest: list[str]) -> int:
    if len(rest) != 2 or rest[0] not in ("show", "approve", "reject"):
        return fail("usage: ctx agent deviation show|approve|reject DEVIATION_ID")
    return agent_cli.main(["deviation-" + rest[0], rest[1]])


def _remote_deviation(rest: list[str]) -> int:
    if len(rest) != 2 or rest[0] not in ("show", "approve", "reject"):
        return fail(
            "usage: ctx agent remote-deviation show|approve|reject DEVIATION_ID"
        )
    return agent_cli.main(["remote-deviation-" + rest[0], rest[1]])


def _target(rest: list[str]) -> int:
    if not rest:
        return fail("usage: ctx agent target add|list|show|inspect|remove ...")
    action = rest[0]
    args = rest[1:]
    if action == "list":
        if args:
            return fail("usage: ctx agent target list")
        return agent_cli.main(["target-list"])
    if action == "add":
        if len(args) not in (3, 5) or args[1] != "--ssh":
            return fail(
                "usage: ctx agent target add NAME --ssh USER@HOST [--port PORT]"
            )
        command = ["target-add", args[0], args[2]]
        if len(args) == 5:
            if args[3] != "--port":
                return fail(
                    "usage: ctx agent target add NAME --ssh USER@HOST [--port PORT]"
                )
            command.extend(["--port", args[4]])
        return agent_cli.main(command)
    if action in ("show", "inspect", "remove"):
        if len(args) != 1:
            return fail(f"usage: ctx agent target {action} NAME")
        return agent_cli.main(["target-" + action, args[0]])
    return fail("usage: ctx agent target add|list|show|inspect|remove ...")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("help", "--help", "-h"):
        print(HELP)
        return 0

    version = os.environ.get("CTX_EXTENSION_API_VERSION")
    if version is not None and version != "1":
        return fail("unsupported Context Switcher extension API version")

    action, rest = argv[0], argv[1:]
    try:
        if action in SCOPED:
            project, args = select_project(rest)
            required = {
                "init": 0,
                "notes": 0,
                "import": 1,
                "submit": 1,
                "note": 2,
            }[action]
            if len(args) != required or (action == "submit" and not args[0]):
                return fail(f"invalid arguments for {action}; use ctx agent help")
            return agent_cli.main([action, project, *args])

        if action == "plan":
            return _plan(rest)
        if action == "target":
            return _target(rest)
        if action == "progress":
            return _progress(rest)
        if action in ("approve", "reject"):
            return _approval(action, rest)
        if action == "deviations":
            if rest[:1] == ["--target"]:
                if len(rest) != 2 or not valid_name(rest[1]):
                    return fail("usage: ctx agent deviations --target NAME")
                return agent_cli.main(["remote-deviation-list", rest[1]])
            if not rest:
                context = os.environ.get("CTX_CONTEXT", "")
                return agent_cli.main(
                    ["deviation-list", context] if context else ["deviation-list"]
                )
            project, args = select_project(rest)
            if args:
                return fail(
                    "usage: ctx agent deviations [--project NAME | --target NAME]"
                )
            return agent_cli.main(["deviation-list", project])
        if action == "deviation":
            return _deviation(rest)
        if action == "remote-deviation":
            return _remote_deviation(rest)

        if action in ("codex", "chatgpt"):
            if len(rest) != 1:
                return fail(f"usage: ctx agent {action} login|status|logout|doctor")
            command = rest[0]
            if command not in ("login", "status", "logout", "doctor"):
                return fail(f"usage: ctx agent {action} login|status|logout|doctor")
            return codex_auth.main([command])

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
