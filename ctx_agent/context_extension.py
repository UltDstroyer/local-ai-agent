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

Current task:
  ctx agent status
  ctx agent progress
  ctx agent approve
  ctx agent reject
  ctx agent deviation
  ctx agent deviations
  ctx agent plan show|cancel
  ctx agent plan "GOAL"

The agent operates one active plan at a time. Current-task commands automatically
resolve whether that plan is a local project or an SSH target, so normal use
does not require --target, target names, plan IDs, or deviation IDs.

Projects:
  ctx agent import [--project NAME] SOURCE_DIRECTORY
  ctx agent submit [--project NAME] "TASK"
  ctx agent note [--project NAME] KEY "TEXT"
  ctx agent notes [--project NAME]

Agent:
  ctx agent mode quiet|balanced|maximum
  ctx agent health
  ctx agent codex login|status|logout|doctor

One-time SSH setup remains available through ctx agent target add/list/show/
inspect/remove, but target selection is not part of the normal task workflow.

Plan creation is read-only. ctx-agent and Codex negotiate a mutation budget,
then wait for owner approval. Approved plans execute autonomously only in
maximum mode. When owner input is required, status/progress shows the reason
and the single approve/reject command to use.
"""


def fail(message: str) -> int:
    print(f"ctx-ext-agent: {message}", file=sys.stderr)
    return 2


ACTIVE_PLAN_STATUSES = frozenset({
    "awaiting_approval",
    "approved",
    "running",
    "awaiting_deviation",
    "ready_for_review",
})


def _raw(payload: dict, timeout: int = 30) -> dict:
    socket_path = os.environ.get("CTX_AGENT_SOCKET", "/run/ctx-agent/agent.sock")
    return agent_cli.call(socket_path, payload, timeout=timeout)


def _all_plan_rows() -> list[tuple[str, dict]]:
    rows = []
    for row in _raw({"op": "plan_list"}).get("plans", []):
        if isinstance(row, dict):
            rows.append(("project", row))
    for row in _raw({"op": "remote_plan_list"}).get("plans", []):
        if isinstance(row, dict):
            rows.append(("remote", row))
    return rows


def _current_task(include_latest: bool = False) -> tuple[str, dict] | None:
    rows = _all_plan_rows()
    active = [
        item for item in rows
        if item[1].get("status") in ACTIVE_PLAN_STATUSES
    ]
    if len(active) > 1:
        raise ValueError(
            "more than one active plan exists; cancel the extra plan before continuing"
        )
    if active:
        return active[0]
    if not include_latest or not rows:
        return None
    rows.sort(
        key=lambda item: float(item[1].get("updated") or item[1].get("created") or 0),
        reverse=True,
    )
    return rows[0]


def _known_project(name: str) -> bool:
    if not name or not valid_name(name):
        return False
    projects = _raw({"op": "projects"}).get("projects", [])
    return name in projects


def _sole_target() -> str:
    targets = _raw({"op": "target_list"}).get("targets", [])
    if len(targets) != 1:
        if not targets:
            raise ValueError(
                "no active project and no SSH target is configured"
            )
        raise ValueError(
            "more than one SSH target is configured; automatic target selection "
            "requires exactly one target"
        )
    name = targets[0].get("name")
    if not isinstance(name, str) or not valid_name(name):
        raise ValueError("configured SSH target has an invalid name")
    return name


def _plan_command(kind: str, row: dict, action: str) -> list[str]:
    if kind == "remote":
        return ["remote-plan-" + action, row["target"], row["id"]]
    return ["plan-" + action, row["project"], row["id"]]


def _pending_deviation(kind: str, row: dict) -> tuple[str, dict]:
    if kind == "remote":
        view = _raw({
            "op": "remote_plan_show",
            "target": row["target"],
            "id": row["id"],
        })
    else:
        view = _raw({
            "op": "plan_show",
            "project": row["project"],
            "id": row["id"],
        })
    plan = view.get("plan") if isinstance(view, dict) else None
    deviations = plan.get("deviations", []) if isinstance(plan, dict) else []
    pending = [
        item for item in deviations
        if isinstance(item, dict) and item.get("status") == "pending"
    ]
    if len(pending) != 1:
        raise ValueError(
            "current task does not have exactly one pending deviation"
        )
    return kind, pending[0]


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
    if rest[:1] == ["list"]:
        if len(rest) != 1:
            return fail("usage: ctx agent plan list")
        rows = [row for _, row in _all_plan_rows()]
        rows.sort(
            key=lambda row: float(row.get("updated") or row.get("created") or 0),
            reverse=True,
        )
        agent_cli._print_plan_list({"plans": rows})
        return 0

    if rest and rest[0] in ("show", "cancel", "diff", "apply"):
        if len(rest) != 1:
            return fail(f"usage: ctx agent plan {rest[0]}")
        current = _current_task(include_latest=True)
        if current is None:
            return fail("there is no current plan")
        kind, row = current
        action = rest[0]
        if action in ("diff", "apply") and kind == "remote":
            return fail(f"ctx agent plan {action} applies only to local project plans")
        return agent_cli.main(_plan_command(kind, row, action))

    if rest[:1] == ["--project"]:
        if len(rest) != 3 or not rest[2]:
            return fail('usage: ctx agent plan --project NAME "GOAL"')
        if _current_task() is not None:
            return fail("finish or cancel the current task before creating another")
        project = rest[1]
        if not valid_name(project):
            return fail("invalid project name")
        return agent_cli.main(["plan-create", project, rest[2]])

    if len(rest) != 1 or not rest[0]:
        return fail('usage: ctx agent plan "GOAL"')
    if _current_task() is not None:
        return fail("finish or cancel the current task before creating another")

    context = os.environ.get("CTX_CONTEXT", "")
    if context and _known_project(context):
        return agent_cli.main(["plan-create", context, rest[0]])

    target = _sole_target()
    return agent_cli.main(["remote-plan-create", target, rest[0]])


def _progress(rest: list[str]) -> int:
    if rest:
        return fail("usage: ctx agent progress")
    current = _current_task(include_latest=True)
    if current is None:
        return fail("there is no current plan")
    kind, row = current
    return agent_cli.main(_plan_command(kind, row, "progress"))


def _approval(action: str, rest: list[str]) -> int:
    if rest:
        return fail(f"usage: ctx agent {action}")
    current = _current_task()
    if current is None:
        return fail("there is no active task waiting for a decision")
    kind, row = current

    if row.get("status") == "awaiting_deviation":
        _, deviation = _pending_deviation(kind, row)
        prefix = "remote-" if kind == "remote" else ""
        return agent_cli.main([
            prefix + "deviation-" + action,
            deviation["id"],
        ])

    if row.get("status") != "awaiting_approval":
        return fail(
            f"current task is {row.get('status', 'unknown')}; "
            f"nothing is waiting for {action}"
        )
    return agent_cli.main(_plan_command(kind, row, action))


def _deviation(rest: list[str]) -> int:
    if not rest:
        action = "show"
        explicit_id = None
    else:
        if rest[0] not in ("show", "approve", "reject"):
            return fail("usage: ctx agent deviation [show|approve|reject]")
        if len(rest) > 2:
            return fail(
                "usage: ctx agent deviation [show|approve|reject] [DEVIATION_ID]"
            )
        action = rest[0]
        explicit_id = rest[1] if len(rest) == 2 else None

    current = _current_task()
    if current is None:
        return fail("there is no active task")
    kind, row = current

    if explicit_id:
        did = explicit_id
    else:
        _, deviation = _pending_deviation(kind, row)
        did = deviation["id"]

    prefix = "remote-" if kind == "remote" else ""
    return agent_cli.main([prefix + "deviation-" + action, did])


def _remote_deviation(rest: list[str]) -> int:
    # Backward-compatible alias. Normal use should call ctx agent deviation ...
    return _deviation(rest)


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
            if rest:
                return fail("usage: ctx agent deviations")
            return _deviation([])
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
            if rest:
                return agent_cli.main(["show", rest[0]])
            try:
                current = _current_task(include_latest=True)
            except (OSError, RuntimeError, ValueError, KeyError):
                current = None
            if current is None:
                return agent_cli.main(["health"])
            kind, row = current
            return agent_cli.main(_plan_command(kind, row, "progress"))

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
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        return fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
