"""Remote target and SSH mutation contracts.

Remote administration prefers typed actions. Plans may explicitly opt into the
high-trust remote_exec capability for arbitrary commands on an SSH target.
That capability exists only in the SSH bridge and never grants local desktop
command execution.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import re
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlparse

from .runtime import valid_name

USER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
HOST_RE = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")
SERVICE_RE = re.compile(r"^[A-Za-z0-9@_.:-]{1,128}$")
PACKAGE_RE = re.compile(r"^[A-Za-z0-9.+:-]{1,128}$")

REMOTE_ACTIONS = frozenset({
    "read_file",
    "write_file",
    "ensure_dir",
    "package_install",
    "service",
    "check",
    "remote_exec",
})
SERVICE_ACTIONS = frozenset({"status", "start", "restart", "enable", "enable_now"})
CHECK_TYPES = frozenset({
    "systemctl_is_active",
    "http_get",
    "http_head",
    "nginx_test",
    "journal_tail",
    "listen_sockets",
    "command_exists",
    "path_exists",
})


class RemoteContractError(ValueError):
    pass


@dataclass(frozen=True)
class RemoteTarget:
    name: str
    host: str
    user: str
    port: int = 22

    @classmethod
    def from_dict(cls, raw: object) -> "RemoteTarget":
        if not isinstance(raw, dict):
            raise RemoteContractError("target must be an object")
        name = raw.get("name", "")
        host = raw.get("host", "")
        user = raw.get("user", "")
        port = raw.get("port", 22)
        if not valid_name(name):
            raise RemoteContractError("invalid target name")
        if (
            not isinstance(host, str)
            or not HOST_RE.fullmatch(host)
            or host.startswith("-")
        ):
            raise RemoteContractError("invalid SSH host")
        if (
            not isinstance(user, str)
            or not USER_RE.fullmatch(user)
            or user.startswith("-")
        ):
            raise RemoteContractError("invalid SSH user")
        if type(port) is not int or not 1 <= port <= 65535:
            raise RemoteContractError("invalid SSH port")
        return cls(name=name, host=host, user=user, port=port)

    @classmethod
    def parse_ssh(cls, name: str, endpoint: str, port: int = 22) -> "RemoteTarget":
        if not isinstance(endpoint, str) or endpoint.count("@") != 1:
            raise RemoteContractError("SSH endpoint must be USER@HOST")
        user, host = endpoint.split("@", 1)
        return cls.from_dict({
            "name": name,
            "host": host,
            "user": user,
            "port": port,
        })

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "host": self.host,
            "user": self.user,
            "port": self.port,
        }


def _absolute_remote_pattern(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RemoteContractError("invalid remote path")
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise RemoteContractError("remote paths must be absolute and traversal-free")
    return str(path)


def remote_path_matches(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    normalized = _absolute_remote_pattern(path)
    return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in patterns)


def _safe_url(url: str, allowed_domains: tuple[str, ...]) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise RemoteContractError("invalid URL")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise RemoteContractError("remote checks only allow http/https URLs")
    host = parsed.hostname.lower()
    try:
        ip = ipaddress.ip_address(host)
        if not (ip.is_loopback or ip.is_private):
            raise RemoteContractError("public IP URL is not in allowed domains")
    except ValueError:
        if host not in allowed_domains and host not in ("localhost",):
            raise RemoteContractError("URL domain is outside remote contract")
    return url


@dataclass(frozen=True)
class RemoteBudget:
    allowed_read_paths: tuple[str, ...]
    allowed_write_paths: tuple[str, ...]
    allowed_services: tuple[str, ...]
    allowed_packages: tuple[str, ...]
    allowed_domains: tuple[str, ...]
    allow_sudo: bool = False
    allow_package_install: bool = False
    allow_network: bool = False
    allow_remote_admin: bool = False
    max_actions: int = 40
    max_file_writes: int = 12
    max_attempts_per_milestone: int = 3
    backup_required: bool = True

    @classmethod
    def from_dict(cls, raw: object) -> "RemoteBudget":
        if not isinstance(raw, dict):
            raise RemoteContractError("remote_budget must be an object")

        def strings(name: str, maximum: int = 100) -> tuple[str, ...]:
            values = raw.get(name, [])
            if not isinstance(values, list) or len(values) > maximum:
                raise RemoteContractError(f"{name} must be a list")
            if any(not isinstance(x, str) or not x for x in values):
                raise RemoteContractError(f"invalid value in {name}")
            return tuple(values)

        read_paths = tuple(_absolute_remote_pattern(x) for x in strings(
            "allowed_read_paths", 200
        ))
        write_paths = tuple(_absolute_remote_pattern(x) for x in strings(
            "allowed_write_paths", 200
        ))
        services = strings("allowed_services", 100)
        packages = strings("allowed_packages", 100)
        domains = tuple(x.lower() for x in strings("allowed_domains", 100))
        if any(not SERVICE_RE.fullmatch(x) for x in services):
            raise RemoteContractError("invalid service name")
        if any(not PACKAGE_RE.fullmatch(x) for x in packages):
            raise RemoteContractError("invalid package name")
        if any(
            not HOST_RE.fullmatch(x) or ":" in x
            for x in domains
        ):
            raise RemoteContractError("invalid allowed domain")

        bools = {
            "allow_sudo": raw.get("allow_sudo", False),
            "allow_package_install": raw.get("allow_package_install", False),
            "allow_network": raw.get("allow_network", False),
            "allow_remote_admin": raw.get("allow_remote_admin", False),
            "backup_required": raw.get("backup_required", True),
        }
        if any(type(value) is not bool for value in bools.values()):
            raise RemoteContractError("remote capability flags must be boolean")

        ints = {
            "max_actions": raw.get("max_actions", 40),
            "max_file_writes": raw.get("max_file_writes", 12),
            "max_attempts_per_milestone": raw.get(
                "max_attempts_per_milestone", 3
            ),
        }
        if any(type(value) is not int for value in ints.values()):
            raise RemoteContractError("remote limits must be integers")
        if not 1 <= ints["max_actions"] <= 500:
            raise RemoteContractError("max_actions out of bounds")
        if not 0 <= ints["max_file_writes"] <= 100:
            raise RemoteContractError("max_file_writes out of bounds")
        if not 1 <= ints["max_attempts_per_milestone"] <= 12:
            raise RemoteContractError("max_attempts_per_milestone out of bounds")

        return cls(
            allowed_read_paths=read_paths,
            allowed_write_paths=write_paths,
            allowed_services=services,
            allowed_packages=packages,
            allowed_domains=domains,
            **bools,
            **ints,
        )

    def to_dict(self) -> dict:
        return {
            "allowed_read_paths": list(self.allowed_read_paths),
            "allowed_write_paths": list(self.allowed_write_paths),
            "allowed_services": list(self.allowed_services),
            "allowed_packages": list(self.allowed_packages),
            "allowed_domains": list(self.allowed_domains),
            "allow_sudo": self.allow_sudo,
            "allow_package_install": self.allow_package_install,
            "allow_network": self.allow_network,
            "allow_remote_admin": self.allow_remote_admin,
            "max_actions": self.max_actions,
            "max_file_writes": self.max_file_writes,
            "max_attempts_per_milestone": self.max_attempts_per_milestone,
            "backup_required": self.backup_required,
        }


@dataclass(frozen=True)
class RemotePlan:
    id: str
    target: str
    goal: str
    milestones: tuple[str, ...]
    verification: tuple[str, ...]
    risks: tuple[str, ...]
    assumptions: tuple[str, ...]
    budget: RemoteBudget

    @classmethod
    def from_dict(
        cls, raw: object, target: str, plan_id: str | None = None
    ) -> "RemotePlan":
        if not isinstance(raw, dict):
            raise RemoteContractError("remote plan must be an object")
        if not valid_name(target):
            raise RemoteContractError("invalid target")
        goal = raw.get("goal", "")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 6000:
            raise RemoteContractError("invalid remote plan goal")

        def lines(name: str, minimum: int, maximum: int) -> tuple[str, ...]:
            values = raw.get(name, [])
            if not isinstance(values, list) or not minimum <= len(values) <= maximum:
                raise RemoteContractError(f"{name} must contain {minimum}-{maximum} items")
            if any(
                not isinstance(x, str) or not x.strip() or len(x) > 1200
                for x in values
            ):
                raise RemoteContractError(f"invalid {name}")
            return tuple(x.strip() for x in values)

        return cls(
            id=plan_id or uuid.uuid4().hex[:12],
            target=target,
            goal=goal.strip(),
            milestones=lines("milestones", 1, 50),
            verification=lines("verification", 1, 50),
            risks=lines("risks", 0, 50),
            assumptions=lines("assumptions", 0, 50),
            budget=RemoteBudget.from_dict(raw.get("remote_budget")),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": "remote",
            "target": self.target,
            "goal": self.goal,
            "milestones": list(self.milestones),
            "verification": list(self.verification),
            "risks": list(self.risks),
            "assumptions": list(self.assumptions),
            "remote_budget": self.budget.to_dict(),
        }


def validate_remote_action(action: object, budget: RemoteBudget) -> dict:
    if not isinstance(action, dict):
        raise RemoteContractError("remote action must be an object")
    op = action.get("op")
    if op not in REMOTE_ACTIONS:
        raise RemoteContractError(f"unsupported remote action: {op!r}")
    result = dict(action)

    if op == "read_file":
        path = _absolute_remote_pattern(action.get("path", ""))
        if not remote_path_matches(path, budget.allowed_read_paths):
            raise RemoteContractError(f"read outside remote scope: {path}")
        result["path"] = path

    elif op in ("write_file", "ensure_dir"):
        path = _absolute_remote_pattern(action.get("path", ""))
        if not remote_path_matches(path, budget.allowed_write_paths):
            raise RemoteContractError(f"write outside remote scope: {path}")
        if not budget.allow_sudo:
            raise RemoteContractError("remote writes require approved sudo capability")
        result["path"] = path
        if op == "write_file":
            content = action.get("content")
            if not isinstance(content, str) or len(content.encode()) > 500_000:
                raise RemoteContractError("remote file content is invalid or too large")
            mode = action.get("mode", "0644")
            if mode not in ("0600", "0640", "0644", "0750", "0755"):
                raise RemoteContractError("unsupported remote file mode")
            result["mode"] = mode
        else:
            mode = action.get("mode", "0755")
            if mode not in ("0700", "0750", "0755", "0770"):
                raise RemoteContractError("unsupported remote directory mode")
            result["mode"] = mode

    elif op == "package_install":
        packages = action.get("packages")
        if (
            not budget.allow_package_install
            or not budget.allow_sudo
            or not isinstance(packages, list)
            or not packages
            or len(packages) > 20
        ):
            raise RemoteContractError("package installation is not approved")
        for package in packages:
            if (
                not isinstance(package, str)
                or package not in budget.allowed_packages
                or not PACKAGE_RE.fullmatch(package)
            ):
                raise RemoteContractError(f"package outside remote contract: {package!r}")

    elif op == "service":
        service = action.get("service", "")
        service_action = action.get("action", "")
        if service not in budget.allowed_services:
            raise RemoteContractError(f"service outside remote contract: {service}")
        if service_action not in SERVICE_ACTIONS:
            raise RemoteContractError("unsupported service action")
        if service_action != "status" and not budget.allow_sudo:
            raise RemoteContractError("service mutation requires approved sudo capability")

    elif op == "remote_exec":
        if not budget.allow_remote_admin:
            raise RemoteContractError(
                "arbitrary remote administration is not approved"
            )
        command = action.get("command")
        as_root = action.get("as_root", False)
        timeout = action.get("timeout", 900)
        if (
            not isinstance(command, str)
            or not command.strip()
            or "\x00" in command
            or len(command.encode()) > 50_000
        ):
            raise RemoteContractError("invalid remote command")
        if type(as_root) is not bool:
            raise RemoteContractError("remote_exec as_root must be boolean")
        if as_root and not budget.allow_sudo:
            raise RemoteContractError(
                "root remote execution requires approved sudo capability"
            )
        if type(timeout) is not int or not 5 <= timeout <= 3600:
            raise RemoteContractError("remote_exec timeout out of bounds")
        result["command"] = command
        result["as_root"] = as_root
        result["timeout"] = timeout

    elif op == "check":
        check = action.get("check", "")
        if check not in CHECK_TYPES:
            raise RemoteContractError("unsupported remote check")
        if check in ("http_get", "http_head"):
            url = _safe_url(action.get("url", ""), budget.allowed_domains)
            if not budget.allow_network:
                parsed = urlparse(url)
                host = parsed.hostname or ""
                try:
                    ip = ipaddress.ip_address(host)
                    local = ip.is_loopback or ip.is_private
                except ValueError:
                    local = host == "localhost"
                if not local:
                    raise RemoteContractError("external HTTP checks need network approval")
            result["url"] = url
        if check in ("systemctl_is_active", "journal_tail"):
            service = action.get("service", "")
            if service not in budget.allowed_services:
                raise RemoteContractError("check references unapproved service")
        if check == "path_exists":
            path = _absolute_remote_pattern(action.get("path", ""))
            if not (
                remote_path_matches(path, budget.allowed_read_paths)
                or remote_path_matches(path, budget.allowed_write_paths)
            ):
                raise RemoteContractError("path check outside approved scope")
            result["path"] = path
        if check == "command_exists":
            command = action.get("command", "")
            if not isinstance(command, str) or not re.fullmatch(
                r"[A-Za-z0-9._+-]{1,64}", command
            ):
                raise RemoteContractError("invalid command existence check")

    return result


def action_deviation(plan: RemotePlan, action: object, reason: str) -> dict:
    return {
        "type": "remote_scope",
        "reason": reason[:4000],
        "proposed_action": action if isinstance(action, dict) else {},
        "target": plan.target,
        "current_budget": plan.budget.to_dict(),
    }
