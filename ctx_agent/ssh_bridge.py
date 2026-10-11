"""User-owned SSH broker for typed remote administration.

The system ctx-agent never receives SSH private keys or agent sockets. This
bridge runs in the desktop user's session and translates a small typed action
vocabulary into fixed SSH commands.
"""

from __future__ import annotations

import argparse
import base64
import grp
import json
import os
import shlex
import shutil
import socket
import socketserver
import subprocess
import threading
from pathlib import PurePosixPath, Path

from .remote import (
    RemoteBudget,
    RemoteContractError,
    RemoteTarget,
    validate_remote_action,
)

MAX_OUTPUT = 80_000
MAX_REQUEST = 1_000_000


class SSHBridgeError(RuntimeError):
    pass


def _ssh_binary() -> str:
    binary = shutil.which("ssh")
    if not binary:
        raise SSHBridgeError("OpenSSH client is not installed")
    return binary


def _ssh_base(target: RemoteTarget) -> list[str]:
    return [
        _ssh_binary(),
        "-o", "BatchMode=yes",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=2",
        "-p", str(target.port),
        "--",
        f"{target.user}@{target.host}",
    ]


def _resolve_inputs(value, inputs: dict[str, str]):
    if not isinstance(value, str):
        return value
    result = value
    for key, supplied in inputs.items():
        result = result.replace("{{input:" + key + "}}", supplied)
    return result


def _resolve_action_inputs(action: dict, inputs: dict[str, str]) -> dict:
    resolved = dict(action)
    for field in ("command", "content", "url", "path"):
        if field in resolved and isinstance(resolved[field], str):
            resolved[field] = _resolve_inputs(resolved[field], inputs)
            if "{{input:" in resolved[field]:
                raise SSHBridgeError(
                    "required human input is unavailable; request it from the owner again"
                )
    return resolved


def _run_script(
    target: RemoteTarget,
    script: str,
    *,
    timeout: int = 120,
) -> dict:
    if not isinstance(script, str) or not script or len(script) > 100_000:
        raise SSHBridgeError("invalid remote script")
    command = _ssh_base(target) + ["sh", "-c", shlex.quote(script)]
    try:
        proc = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=max(5, min(timeout, 3600)),
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired as exc:
        raise SSHBridgeError(f"SSH operation exceeded {timeout} seconds") from exc
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "output": proc.stdout[-MAX_OUTPUT:],
    }


def _sudo(budget: RemoteBudget) -> str:
    if not budget.allow_sudo:
        raise RemoteContractError("sudo capability is not approved")
    return "sudo -n "


def _quote(value: str) -> str:
    return shlex.quote(value)


def _backup_key(path: str) -> str:
    return path.encode("utf-8").hex()


def _backup_prefix(plan: str, milestone: int) -> str:
    if (
        not isinstance(plan, str)
        or len(plan) != 12
        or not plan.isalnum()
        or type(milestone) is not int
        or not 0 <= milestone <= 999
    ):
        raise SSHBridgeError("invalid remote backup identity")
    return f"/var/lib/ctx-agent-remote-backups/{plan}/m{milestone:03d}"


def _inspection_script() -> str:
    return r"""set +e
printf '%s\n' '=== identity ==='
id
printf '%s\n' '=== uname ==='
uname -a
printf '%s\n' '=== os-release ==='
cat /etc/os-release 2>/dev/null
printf '%s\n' '=== uptime ==='
uptime 2>/dev/null
printf '%s\n' '=== filesystems ==='
df -hT 2>/dev/null
printf '%s\n' '=== block-devices ==='
lsblk -o NAME,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS 2>/dev/null
printf '%s\n' '=== listeners ==='
ss -lntup 2>/dev/null || ss -lnt 2>/dev/null
printf '%s\n' '=== common-services ==='
for s in jellyfin nginx cloudflared caddy docker; do
  printf '%s: ' "$s"
  systemctl is-active "$s" 2>/dev/null || true
done
printf '%s\n' '=== discovered-services ==='
systemctl list-unit-files --type=service --no-legend --no-pager 2>/dev/null \
  | awk '{print $1}' | sed -n '1,300p'
printf '%s\n' '=== discovered-running-services ==='
systemctl list-units --type=service --state=running --no-legend --no-pager 2>/dev/null \
  | awk '{print $1}' | sed -n '1,200p'
printf '%s\n' '=== common-commands ==='
for c in jellyfin nginx cloudflared curl apt-get dnf pacman podman docker; do
  if command -v "$c" >/dev/null 2>&1; then
    printf '%s=' "$c"
    command -v "$c"
  fi
done
printf '%s\n' '=== selected-configs ==='
for f in /etc/nginx/nginx.conf /etc/cloudflared/config.yml /etc/fstab; do
  if [ -e "$f" ]; then
    ls -ld "$f" 2>/dev/null
  fi
done
"""


def _read_file(
    target: RemoteTarget, budget: RemoteBudget, action: dict
) -> dict:
    path = action["path"]
    prefix = "sudo -n " if budget.allow_sudo else ""
    script = (
        "set -eu; "
        + prefix
        + "test -f "
        + _quote(path)
        + "; "
        + prefix
        + "cat -- "
        + _quote(path)
    )
    return _run_script(target, script, timeout=60)


def _write_file(
    target: RemoteTarget,
    budget: RemoteBudget,
    action: dict,
    plan: str,
    milestone: int,
) -> dict:
    sudo = _sudo(budget)
    path = action["path"]
    payload = base64.b64encode(action["content"].encode()).decode()
    mode = action["mode"]
    backup_root = _backup_prefix(plan, milestone)
    key = _backup_key(path)
    backup = f"{backup_root}/{key}.bak"
    marker = f"{backup_root}/{key}.new"
    script = f"""set -eu
{sudo}mkdir -p -- {_quote(backup_root)}
if ! {sudo}test -e {_quote(backup)} && ! {sudo}test -e {_quote(marker)}; then
  if {sudo}test -e {_quote(path)}; then
    {sudo}cp -a -- {_quote(path)} {_quote(backup)}
  else
    {sudo}touch -- {_quote(marker)}
  fi
fi
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
printf %s {_quote(payload)} | base64 -d > "$tmp"
{sudo}install -m {_quote(mode)} -- "$tmp" {_quote(path)}
"""
    result = _run_script(target, script, timeout=120)
    if result["ok"]:
        result["mutation"] = {
            "op": "write_file",
            "path": path,
            "backup": backup,
            "new_marker": marker,
        }
    return result


def _ensure_dir(
    target: RemoteTarget,
    budget: RemoteBudget,
    action: dict,
    plan: str,
    milestone: int,
) -> dict:
    sudo = _sudo(budget)
    path = action["path"]
    mode = action["mode"]
    backup_root = _backup_prefix(plan, milestone)
    key = _backup_key(path)
    marker = f"{backup_root}/{key}.dir-new"
    script = f"""set -eu
{sudo}mkdir -p -- {_quote(backup_root)}
if ! {sudo}test -d {_quote(path)}; then
  {sudo}touch -- {_quote(marker)}
  {sudo}install -d -m {_quote(mode)} -- {_quote(path)}
fi
"""
    result = _run_script(target, script, timeout=60)
    if result["ok"]:
        result["mutation"] = {
            "op": "ensure_dir",
            "path": path,
            "new_marker": marker,
        }
    return result


def _package_install(
    target: RemoteTarget, budget: RemoteBudget, action: dict
) -> dict:
    sudo = _sudo(budget)
    if not budget.allow_network:
        raise RemoteContractError("package installation requires network approval")
    packages = " ".join(_quote(x) for x in action["packages"])
    script = f"""set -eu
command -v apt-get >/dev/null 2>&1 || {{
  echo 'Only apt-get package installation is currently supported' >&2
  exit 64
}}
export DEBIAN_FRONTEND=noninteractive
{sudo}apt-get update
{sudo}apt-get install -y -- {packages}
"""
    return _run_script(target, script, timeout=1800)


def _service(
    target: RemoteTarget, budget: RemoteBudget, action: dict
) -> dict:
    name = action["service"]
    service_action = action["action"]
    if service_action == "status":
        return _run_script(
            target,
            "systemctl status --no-pager -- "
            + _quote(name)
            + " || true",
            timeout=60,
        )
    sudo = _sudo(budget)
    mapping = {
        "start": f"{sudo}systemctl start -- {_quote(name)}",
        "restart": f"{sudo}systemctl restart -- {_quote(name)}",
        "enable": f"{sudo}systemctl enable -- {_quote(name)}",
        "enable_now": f"{sudo}systemctl enable --now -- {_quote(name)}",
    }
    return _run_script(target, "set -eu; " + mapping[service_action], timeout=120)


def _remote_exec(
    target: RemoteTarget, budget: RemoteBudget, action: dict
) -> dict:
    """Execute an explicitly approved command on the SSH target only."""
    command = action["command"]
    timeout = action["timeout"]
    if action["as_root"]:
        command = _sudo(budget) + "sh -c " + _quote(command)
    return _run_script(target, command, timeout=timeout)


def _check(
    target: RemoteTarget, budget: RemoteBudget, action: dict
) -> dict:
    check = action["check"]
    if check == "systemctl_is_active":
        script = "systemctl is-active -- " + _quote(action["service"])
    elif check == "journal_tail":
        prefix = "sudo -n " if budget.allow_sudo else ""
        script = (
            prefix
            + "journalctl -u "
            + _quote(action["service"])
            + " -n 80 --no-pager"
        )
    elif check == "nginx_test":
        prefix = "sudo -n " if budget.allow_sudo else ""
        script = prefix + "nginx -t"
    elif check == "listen_sockets":
        script = "ss -lntup 2>/dev/null || ss -lnt"
    elif check == "command_exists":
        script = "command -v -- " + _quote(action["command"])
    elif check == "path_exists":
        script = "test -e " + _quote(action["path"]) + " && echo exists"
    elif check in ("http_get", "http_head"):
        flag = "-fsS" if check == "http_get" else "-fsSI"
        script = (
            "command -v curl >/dev/null 2>&1 && curl "
            + flag
            + " --max-time 15 -- "
            + _quote(action["url"])
        )
    else:
        raise RemoteContractError("unsupported remote check")
    return _run_script(target, script, timeout=90)


def _rollback_records(
    target: RemoteTarget,
    budget: RemoteBudget,
    records: list[dict],
) -> dict:
    sudo = _sudo(budget)
    if not isinstance(records, list) or len(records) > 100:
        raise SSHBridgeError("invalid rollback record set")
    scripts = ["set -eu"]
    restored = []
    for record in reversed(records):
        if not isinstance(record, dict):
            raise SSHBridgeError("invalid rollback record")
        op = record.get("op")
        path = record.get("path", "")
        if op not in ("write_file", "ensure_dir"):
            continue
        validate_remote_action(
            {
                "op": "ensure_dir" if op == "ensure_dir" else "write_file",
                "path": path,
                "mode": "0755" if op == "ensure_dir" else "0644",
                **({"content": "rollback-placeholder"} if op == "write_file" else {}),
            },
            budget,
        )
        if op == "write_file":
            backup = record.get("backup", "")
            marker = record.get("new_marker", "")
            if not isinstance(backup, str) or not isinstance(marker, str):
                raise SSHBridgeError("invalid rollback metadata")
            scripts.append(
                f"if {sudo}test -e {_quote(backup)}; then "
                f"{sudo}cp -a -- {_quote(backup)} {_quote(path)}; "
                f"elif {sudo}test -e {_quote(marker)}; then "
                f"{sudo}rm -f -- {_quote(path)}; fi"
            )
        else:
            marker = record.get("new_marker", "")
            if not isinstance(marker, str):
                raise SSHBridgeError("invalid rollback metadata")
            scripts.append(
                f"if {sudo}test -e {_quote(marker)}; then "
                f"{sudo}rmdir --ignore-fail-on-non-empty -- {_quote(path)} || true; fi"
            )
        restored.append(path)
    result = _run_script(target, "\n".join(scripts), timeout=180)
    result["restored"] = restored
    return result


def _redact_input_values(text: str, inputs: dict[str, str]) -> str:
    value = str(text)
    for supplied in inputs.values():
        if isinstance(supplied, str) and supplied:
            value = value.replace(supplied, "[REDACTED]")
    return value


def _redact_result_outputs(result: dict, inputs: dict[str, str]) -> dict:
    if not isinstance(result, dict) or not inputs:
        return result
    redacted = dict(result)
    secrets = [value for value in inputs.values() if isinstance(value, str) and value]
    for key in ("output", "stdout", "stderr", "error", "message"):
        value = redacted.get(key)
        if not isinstance(value, str):
            continue
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        redacted[key] = value
    return redacted


class SSHBridge:
    def dispatch(self, request: dict) -> dict:
        op = request.get("op")
        if op == "health":
            return {"available": bool(shutil.which("ssh"))}
        target = RemoteTarget.from_dict(request.get("target"))

        if op == "inspect":
            return _run_script(target, _inspection_script(), timeout=120)

        budget = RemoteBudget.from_dict(request.get("budget"))
        if op == "rollback":
            return _rollback_records(
                target, budget, request.get("records", [])
            )
        if op != "action":
            raise SSHBridgeError("unsupported SSH bridge operation")

        raw_action = request.get("action")
        inputs = request.get("inputs") or {}
        if not isinstance(inputs, dict) or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in inputs.items()
        ):
            raise SSHBridgeError("invalid input values")
        action = validate_remote_action(
            _resolve_action_inputs(raw_action, inputs),
            budget,
            allow_critical_remote_exec=(
                request.get("critical_owner_approved") is True
            ),
        )
        if action["op"] == "read_file":
            result = _read_file(target, budget, action)
        elif action["op"] == "write_file":
            result = _write_file(
                target,
                budget,
                action,
                request.get("plan", ""),
                request.get("milestone", -1),
            )
        elif action["op"] == "ensure_dir":
            result = _ensure_dir(
                target,
                budget,
                action,
                request.get("plan", ""),
                request.get("milestone", -1),
            )
        elif action["op"] == "package_install":
            result = _package_install(target, budget, action)
        elif action["op"] == "service":
            result = _service(target, budget, action)
        elif action["op"] == "check":
            result = _check(target, budget, action)
        elif action["op"] == "remote_exec":
            result = _remote_exec(target, budget, action)
        else:
            raise SSHBridgeError("unreachable remote action")
        return _redact_result_outputs(result, inputs)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        raw = self.rfile.readline(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            return
        request = {}
        try:
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise SSHBridgeError("request must be an object")
            with self.server.bridge_lock:
                result = self.server.bridge.dispatch(request)
            response = {"ok": True, "result": result}
        except (
            SSHBridgeError,
            RemoteContractError,
            OSError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            inputs = {}
            if isinstance(request, dict):
                raw_inputs = request.get("inputs") or {}
                if isinstance(raw_inputs, dict):
                    inputs = {
                        str(key): value
                        for key, value in raw_inputs.items()
                        if isinstance(value, str)
                    }
            response = {
                "ok": False,
                "error": _redact_input_values(str(exc), inputs),
            }
        except Exception:
            response = {"ok": False, "error": "internal SSH bridge error"}
        self.wfile.write((json.dumps(response) + "\n").encode())


class Server(socketserver.UnixStreamServer):
    allow_reuse_address = False


def ssh_bridge_call(
    path: str | Path, payload: dict, timeout: int = 1900
) -> dict:
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
            if len(data) > 2_000_000:
                raise SSHBridgeError("SSH bridge response too large")
            if b"\n" in part:
                break
    response = json.loads(data)
    if not response.get("ok"):
        raise SSHBridgeError(response.get("error", "SSH bridge failed"))
    return response["result"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ctx-agent-ssh-bridge")
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
            raise SSHBridgeError("refusing to replace non-socket SSH bridge path")
        args.socket.unlink()

    bridge = SSHBridge()
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
