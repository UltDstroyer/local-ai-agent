"""Fail-closed request classification; NOT an operating-system sandbox.

A future privileged launcher must enforce filesystem, process and desktop
boundaries independently of these advisory checks. Never execute arbitrary
commands relying on Policy.evaluate() as the only protection.
"""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Effect = Literal["allow", "deny", "approval_required"]

# Kept in code, not editable policy: the model cannot promote these actions.
PERMANENTLY_DENIED = frozenset({
    "policy.modify", "audit.disable", "privilege.escalate",
    "process.memory_read", "credentials.access", "disk.format",
    "disk.partition", "desktop.capture_background",
})

FILE_OPERATIONS = frozenset({
    "filesystem.read", "filesystem.write", "filesystem.delete",
    "filesystem.move", "filesystem.chmod",
})

CRITICAL_OPERATIONS = frozenset({
    "system.change", "service.deploy", "package.install_global",
    "network.upload", "desktop.control", "desktop.capture",
    "desktop.launch", "filesystem.delete", "filesystem.move",
    "filesystem.chmod",
})

# There is intentionally no command execution until sandboxed workers exist.
UNIMPLEMENTED_OPERATIONS = frozenset({
    "command.execute", "network.download", "memory.store",
})

KNOWN_OPERATIONS = (
    PERMANENTLY_DENIED | FILE_OPERATIONS | CRITICAL_OPERATIONS
    | UNIMPLEMENTED_OPERATIONS
)


class PolicyError(ValueError):
    """Missing, malformed or unsafe policy configuration."""


@dataclass(frozen=True)
class Decision:
    effect: Effect
    reason: str

    @property
    def permitted(self) -> bool:
        return self.effect == "allow"


@dataclass(frozen=True)
class Policy:
    protected_roots: tuple[Path, ...]
    protected_names: tuple[str, ...]
    critical_write_roots: tuple[Path, ...]

    @staticmethod
    def _absolute(value: str) -> Path:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise PolicyError("invalid filesystem path")
        expanded = os.path.expanduser(value)
        if not os.path.isabs(expanded):
            raise PolicyError("policy paths must be absolute")
        return Path(os.path.abspath(expanded))

    @classmethod
    def from_dict(cls, data: object) -> "Policy":
        if not isinstance(data, dict) or data.get("version") != 1:
            raise PolicyError("unsupported or missing policy version")
        fs = data.get("filesystem")
        if not isinstance(fs, dict) or fs.get("default") != "allow":
            raise PolicyError("filesystem policy must declare default=allow")
        required = ("protected_roots", "protected_names", "critical_write_roots")
        for name in required:
            values = fs.get(name)
            if not isinstance(values, list) or not values or any(
                not isinstance(v, str) or not v for v in values
            ):
                raise PolicyError(f"invalid or absent {name}")
        if any("/" in n or "\x00" in n for n in fs["protected_names"]):
            raise PolicyError("protected_names must be filename patterns")
        return cls(
            protected_roots=tuple(map(cls._absolute, fs["protected_roots"])),
            protected_names=tuple(fs["protected_names"]),
            critical_write_roots=tuple(
                map(cls._absolute, fs["critical_write_roots"])
            ),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "Policy":
        try:
            return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise PolicyError(f"cannot load policy: {exc}") from exc

    @staticmethod
    def _within(path: Path, root: Path) -> bool:
        return path == root or root in path.parents

    @staticmethod
    def _variants(value: str) -> tuple[Path, Path]:
        # Checking both forms blocks obvious lexical tricks and symlink targets.
        # This is NOT race-safe; the kernel sandbox must be the security boundary.
        source = Policy._absolute(value)
        return source, source.resolve(strict=False)

    def evaluate(
        self,
        operation: str,
        paths: tuple[str, ...] | list[str] = (),
        session: str = "background",
    ) -> Decision:
        if session not in ("background", "interactive"):
            return Decision("deny", "invalid_session")
        if operation in PERMANENTLY_DENIED:
            return Decision("deny", "permanently_forbidden")
        if operation not in KNOWN_OPERATIONS:
            return Decision("deny", "unknown_operation")
        if operation in UNIMPLEMENTED_OPERATIONS:
            return Decision("deny", "executor_not_implemented")
        if operation in FILE_OPERATIONS:
            expected = 2 if operation == "filesystem.move" else 1
            if not isinstance(paths, (tuple, list)) or len(paths) != expected:
                return Decision("deny", "incorrect_path_count")
            critical_target = False
            for raw in paths:
                try:
                    candidates = self._variants(raw)
                except (PolicyError, OSError, RuntimeError):
                    return Decision("deny", "invalid_path")
                for candidate in candidates:
                    if any(
                        self._within(candidate, root.resolve(strict=False))
                        for root in self.protected_roots
                    ):
                        return Decision("deny", "protected_root")
                    if any(
                        fnmatch.fnmatchcase(part, pattern)
                        for part in candidate.parts
                        for pattern in self.protected_names
                    ):
                        return Decision("deny", "protected_filename")
                    if operation != "filesystem.read" and any(
                        self._within(candidate, root.resolve(strict=False))
                        for root in self.critical_write_roots
                    ):
                        critical_target = True
            if critical_target:
                return Decision("approval_required", "critical_system_path")
        elif paths:
            return Decision("deny", "unexpected_paths")
        if operation in CRITICAL_OPERATIONS:
            return Decision("approval_required", "critical_operation")
        return Decision("allow", "ordinary_filesystem_operation")
