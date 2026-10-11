"""User-facing local CLI. No privileged shell or remote network endpoint."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import sys
import threading
import time
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


def _duration(seconds):
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _progress_line(progress):
    stage = progress.get("current_stage_name") or progress.get("phase", "working")
    index = progress.get("current_stage_index")
    count = progress.get("stage_count")
    prefix = f"{index}/{count} " if index and count else ""
    elapsed = _duration(progress.get("elapsed_seconds"))
    remaining = progress.get("estimated_remaining_seconds")
    if remaining is not None:
        eta = "~" + _duration(remaining) + " left"
    else:
        work = progress.get("estimated_work_remaining_seconds")
        waiting = progress.get("waiting_on")
        if work is not None:
            eta = "~" + _duration(work) + " work left"
            if waiting:
                eta += f"; waiting on {waiting.replace('_', ' ')}"
        else:
            eta = "time left unknown"
    percent = progress.get("percent_complete")
    pct = f" · {percent}%" if isinstance(percent, int) else ""
    return f"[ctx-agent] {prefix}{stage}{pct} · elapsed {elapsed} · {eta}"


def _print_progress_report(progress):
    print(f"Phase: {progress.get('phase', 'unknown')}")
    if progress.get("current_stage_name"):
        index = progress.get("current_stage_index")
        count = progress.get("stage_count")
        position = f"{index}/{count} " if index and count else ""
        print(f"Current stage: {position}{progress['current_stage_name']}")
    if isinstance(progress.get("percent_complete"), int):
        print(f"Progress: {progress['percent_complete']}%")
    print(f"Elapsed: {_duration(progress.get('elapsed_seconds'))}")
    remaining = progress.get("estimated_remaining_seconds")
    work = progress.get("estimated_work_remaining_seconds")
    if remaining is not None:
        print(f"Estimated time left: ~{_duration(remaining)}")
    elif work is not None:
        print(f"Estimated work left: ~{_duration(work)}")
    if progress.get("waiting_on"):
        print("Waiting on:", progress["waiting_on"].replace("_", " "))
    stages = progress.get("stages") or progress.get("planning_stages") or []
    if stages:
        print("Stages:")
        labels = {"complete": "done", "running": "active", "queued": "queued", "pending": "pending"}
        for stage in stages:
            status = labels.get(stage.get("status"), stage.get("status", "pending"))
            estimate = stage.get("estimated_seconds")
            suffix = f" (~{_duration(estimate)})" if estimate is not None else ""
            print(f"  [{status}] {stage.get('index')}. {stage.get('name')}{suffix}")
    planned = progress.get("planned_execution_stages") or []
    if planned and not progress.get("stages"):
        print("Planned execution stages:")
        for index, name in enumerate(planned, 1):
            print(f"  {index}. {name}")
    if progress.get("estimate_note"):
        print("Estimate:", progress["estimate_note"])


def _call_with_progress(path, payload, progress_payload, timeout):
    result = {}
    failure = {}
    done = threading.Event()

    def run():
        try:
            result["value"] = call(path, payload, timeout=timeout)
        except Exception as exc:
            failure["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    last = None
    last_planned = ()
    while not done.wait(1.5):
        try:
            snapshot = call(path, progress_payload, timeout=10)["progress"]
            key = (
                snapshot.get("phase"),
                snapshot.get("current_stage_index"),
                snapshot.get("current_stage_name"),
                (snapshot.get("estimated_remaining_seconds") or 0) // 30,
                snapshot.get("percent_complete"),
            )
            if key != last:
                print(_progress_line(snapshot), file=sys.stderr, flush=True)
                last = key
            planned = tuple(snapshot.get("planned_execution_stages") or [])
            if planned and planned != last_planned:
                print("[ctx-agent] Planned execution stages:", file=sys.stderr)
                for index, name in enumerate(planned, 1):
                    print(f"  {index}. {name}", file=sys.stderr)
                last_planned = planned
        except (OSError, RuntimeError, ValueError, KeyError):
            pass
    thread.join()
    if "error" in failure:
        raise failure["error"]
    return result["value"]


def _prompt_human_input(request):
    prompt = str(request.get("prompt", "Enter value")).strip() or "Enter value"
    reason = str(request.get("reason", "")).strip()
    print("", file=sys.stderr)
    print("[Qwen manager] I need one input", file=sys.stderr)
    if reason:
        print("  reason:", reason, file=sys.stderr)
    if request.get("sensitive") is True:
        return getpass.getpass(prompt + ": ")
    return input(prompt + ": ")


def _print_live_deviation(deviation, brief):
    print("", file=sys.stderr)
    print("[Qwen manager] I need your decision", file=sys.stderr, flush=True)
    print("  summary:", brief.get("summary", ""), file=sys.stderr)
    if brief.get("request"):
        print("  request:", brief.get("request", ""), file=sys.stderr)
    print("  risk:", brief.get("risk", "medium"), file=sys.stderr)
    print("  recommendation:", brief.get("recommendation", ""), file=sys.stderr)
    print("  decision:", brief.get("decision", ""), file=sys.stderr)
    print(
        "  reply: ctx a a  (approve)  |  ctx a x  (reject)",
        file=sys.stderr,
        flush=True,
    )


def _show_pending_deviation(path, *, remote, scope, plan_id=""):
    deviations = call(
        path,
        {
            "op": "remote_deviation_list" if remote else "deviation_list",
            "target" if remote else "project": scope,
        },
        timeout=15,
    )["deviations"]
    pending = next(
        (
            item for item in deviations
            if item.get("status") == "pending"
            and (not plan_id or item.get("plan") == plan_id)
        ),
        None,
    )
    if not pending:
        return False
    detail = call(
        path,
        {
            "op": "remote_deviation_show" if remote else "deviation_show",
            "id": pending["id"],
        },
        timeout=180,
    )
    _print_live_deviation(
        detail.get("deviation", pending),
        detail.get("brief", {}),
    )
    return True


def _follow_approved_plan(path, plan, *, remote):
    pid = plan.get("id", "")
    scope = plan.get("target" if remote else "project", "")
    if not scope:
        return
    last = None
    while True:
        progress_payload = {
            "op": "remote_plan_progress" if remote else "plan_progress",
            "target" if remote else "project": scope,
            **({"id": pid} if pid else {}),
        }
        snapshot = call(path, progress_payload, timeout=15)["progress"]
        key = (
            snapshot.get("phase"),
            snapshot.get("current_stage_index"),
            snapshot.get("percent_complete"),
            snapshot.get("waiting_on"),
        )
        if key != last:
            print(_progress_line(snapshot), file=sys.stderr, flush=True)
            last = key
        phase = snapshot.get("phase")
        if phase == "awaiting_deviation":
            _show_pending_deviation(
                path, remote=remote, scope=scope, plan_id=pid
            )
            return
        if phase in (
            "completed",
            "cancelled",
            "rejected",
            "ready_for_owner_review",
            "waiting_for_maximum_mode",
            "paused",
        ):
            return
        time.sleep(1.0)


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
        "plan-show", "plan-progress", "plan-approve", "plan-reject", "plan-cancel",
        "plan-diff", "plan-apply",
    ):
        _add_project_and_optional_id(sub.add_parser(cmd))

    item = sub.add_parser("deviation-list")
    item.add_argument("project", nargs="?")

    for cmd in ("deviation-show", "deviation-approve", "deviation-reject"):
        item = sub.add_parser(cmd)
        item.add_argument("id")

    item = sub.add_parser("target-add")
    item.add_argument("name")
    item.add_argument("ssh")
    item.add_argument("--port", type=int, default=22)

    sub.add_parser("target-list")
    for cmd in ("target-show", "target-remove", "target-inspect"):
        item = sub.add_parser(cmd)
        item.add_argument("name")

    item = sub.add_parser("remote-plan-create")
    item.add_argument("target")
    item.add_argument("goal")

    item = sub.add_parser("remote-plan-list")
    item.add_argument("target", nargs="?")

    for cmd in (
        "remote-plan-show", "remote-plan-progress", "remote-plan-approve",
        "remote-plan-reject", "remote-plan-cancel", "remote-plan-events",
    ):
        item = sub.add_parser(cmd)
        item.add_argument("target")
        item.add_argument("id", nargs="?")

    item = sub.add_parser("remote-deviation-list")
    item.add_argument("target", nargs="?")

    for cmd in (
        "remote-deviation-show",
        "remote-deviation-approve",
        "remote-deviation-reject",
    ):
        item = sub.add_parser(cmd)
        item.add_argument("id")

    args = parser.parse_args(argv)
    data = {"op": args.action.replace("-", "_")}
    for field in (
        "project", "prompt", "goal", "id", "key", "text", "mode",
        "name", "ssh", "port", "target",
    ):
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
            timeout = 1200 if args.action in (
                "plan-create", "remote-plan-create", "target-inspect"
            ) else 900
            if args.action == "plan-create":
                result = _call_with_progress(
                    args.socket,
                    data,
                    {
                        "op": "plan_progress",
                        "project": args.project,
                    },
                    timeout,
                )
            elif args.action == "remote-plan-create":
                result = _call_with_progress(
                    args.socket,
                    data,
                    {
                        "op": "remote_plan_progress",
                        "target": args.target,
                    },
                    timeout,
                )
            else:
                result = call(args.socket, data, timeout=timeout)

        if args.action in (
            "plan-approve",
            "remote-plan-approve",
            "deviation-approve",
            "remote-deviation-approve",
        ) and isinstance(result.get("plan"), dict):
            plan = result["plan"]
            remote = args.action.startswith("remote-")
            print("[Qwen manager] approved", file=sys.stderr, flush=True)
            _follow_approved_plan(args.socket, plan, remote=remote)

        if args.action in ("plan-progress", "remote-plan-progress"):
            _print_progress_report(result["progress"])
            phase = result["progress"].get("phase")
            if phase not in (
                "completed",
                "cancelled",
                "rejected",
                "ready_for_owner_review",
                "waiting_for_maximum_mode",
                "paused",
                "awaiting_deviation",
            ):
                remote = args.action == "remote-plan-progress"
                scope = args.target if remote else args.project
                pid = getattr(args, "id", None) or ""
                if scope:
                    _follow_approved_plan(
                        args.socket,
                        {
                            "id": pid or result["progress"].get("plan_id", ""),
                            "target" if remote else "project": scope,
                        },
                        remote=remote,
                    )
            elif phase == "awaiting_deviation":
                remote = args.action == "remote-plan-progress"
                scope = args.target if remote else args.project
                pid = getattr(args, "id", None) or ""
                if scope:
                    _show_pending_deviation(
                        args.socket,
                        remote=remote,
                        scope=scope,
                        plan_id=pid,
                    )
        elif args.action in ("deviation-show", "remote-deviation-show"):
            deviation = result["deviation"]
            brief = result.get("brief", {})
            _print_live_deviation(deviation, brief)
        elif args.action in (
            "plan-approve",
            "remote-plan-approve",
            "deviation-approve",
            "remote-deviation-approve",
        ):
            pass
        elif args.action in (
            "plan-reject",
            "remote-plan-reject",
            "deviation-reject",
            "remote-deviation-reject",
        ):
            print("[Qwen manager] rejected", file=sys.stderr)
        elif args.action in ("diff", "plan-diff"):
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
