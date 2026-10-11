"""Project plans, mutation budgets, and post-edit scope enforcement."""

from __future__ import annotations

import difflib
import fnmatch
import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from .snapshot import inventory

DEPENDENCY_FILES = {
    "pyproject.toml", "requirements.txt", "requirements-dev.txt",
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum", "Gemfile", "Gemfile.lock",
}
CONFIG_PATTERNS = (
    "flake.nix", "flake.lock", "nix/*", "nix/**", ".github/*", ".github/**",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
)


class ContractError(ValueError):
    pass


def _safe_pattern(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ContractError("invalid scope entry")
    p = Path(value)
    if p.is_absolute() or ".." in p.parts:
        raise ContractError("scope entries must stay inside the project")
    return value.replace("\\", "/")


def path_matches(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    path = path.replace("\\", "/")
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def is_dependency_path(path: str) -> bool:
    return Path(path).name in DEPENDENCY_FILES


def is_config_path(path: str) -> bool:
    return path_matches(path, CONFIG_PATTERNS)


@dataclass(frozen=True)
class MutationBudget:
    max_files_changed: int = 4
    max_lines_changed: int = 300
    allow_new_files: bool = True
    allow_file_deletion: bool = False
    allow_dependency_changes: bool = False
    allow_config_changes: bool = False
    allow_network: bool = False
    max_attempts_per_milestone: int = 3

    @classmethod
    def from_dict(cls, raw: object) -> "MutationBudget":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ContractError("mutation_budget must be an object")
        data = {
            "max_files_changed": raw.get("max_files_changed", 4),
            "max_lines_changed": raw.get("max_lines_changed", 300),
            "allow_new_files": raw.get("allow_new_files", True),
            "allow_file_deletion": raw.get("allow_file_deletion", False),
            "allow_dependency_changes": raw.get("allow_dependency_changes", False),
            "allow_config_changes": raw.get("allow_config_changes", False),
            "allow_network": raw.get("allow_network", False),
            "max_attempts_per_milestone": raw.get("max_attempts_per_milestone", 3),
        }
        for name in ("max_files_changed", "max_lines_changed", "max_attempts_per_milestone"):
            if type(data[name]) is not int:
                raise ContractError(f"{name} must be an integer")
        if not 1 <= data["max_files_changed"] <= 500:
            raise ContractError("max_files_changed out of bounds")
        if not 1 <= data["max_lines_changed"] <= 100_000:
            raise ContractError("max_lines_changed out of bounds")
        if not 1 <= data["max_attempts_per_milestone"] <= 12:
            raise ContractError("max_attempts_per_milestone out of bounds")
        for name in (
            "allow_new_files", "allow_file_deletion", "allow_dependency_changes",
            "allow_config_changes", "allow_network",
        ):
            if type(data[name]) is not bool:
                raise ContractError(f"{name} must be boolean")
        return cls(**data)

    def to_dict(self) -> dict:
        return {
            "max_files_changed": self.max_files_changed,
            "max_lines_changed": self.max_lines_changed,
            "allow_new_files": self.allow_new_files,
            "allow_file_deletion": self.allow_file_deletion,
            "allow_dependency_changes": self.allow_dependency_changes,
            "allow_config_changes": self.allow_config_changes,
            "allow_network": self.allow_network,
            "max_attempts_per_milestone": self.max_attempts_per_milestone,
        }


@dataclass(frozen=True)
class Plan:
    id: str
    project: str
    goal: str
    milestones: tuple[str, ...]
    read_scope: tuple[str, ...]
    write_scope: tuple[str, ...]
    verification: tuple[str, ...]
    risks: tuple[str, ...]
    assumptions: tuple[str, ...]
    budget: MutationBudget

    @classmethod
    def from_dict(cls, raw: object, project: str, plan_id: str | None = None) -> "Plan":
        if not isinstance(raw, dict):
            raise ContractError("plan must be an object")
        goal = raw.get("goal", "")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 6000:
            raise ContractError("plan goal is invalid")

        def string_list(name: str, minimum: int = 0, maximum: int = 100) -> tuple[str, ...]:
            value = raw.get(name, [])
            if not isinstance(value, list) or not minimum <= len(value) <= maximum:
                raise ContractError(f"{name} must contain {minimum}-{maximum} entries")
            out = []
            for item in value:
                if not isinstance(item, str) or not item.strip() or len(item) > 1200:
                    raise ContractError(f"invalid entry in {name}")
                out.append(item.strip())
            return tuple(out)

        milestones = string_list("milestones", 1, 50)
        verification = string_list("verification", 1, 50)
        risks = string_list("risks", 0, 50)
        assumptions = string_list("assumptions", 0, 50)

        read_raw = raw.get("read_scope", ["**"])
        write_raw = raw.get("write_scope", [])
        if not isinstance(read_raw, list) or not 1 <= len(read_raw) <= 200:
            raise ContractError("read_scope must contain 1-200 entries")
        if not isinstance(write_raw, list) or not 1 <= len(write_raw) <= 200:
            raise ContractError("write_scope must contain 1-200 entries")
        read_scope = tuple(_safe_pattern(x) for x in read_raw)
        write_scope = tuple(_safe_pattern(x) for x in write_raw)
        budget = MutationBudget.from_dict(raw.get("mutation_budget"))
        ident = plan_id or uuid.uuid4().hex[:12]
        return cls(
            id=ident,
            project=project,
            goal=goal.strip(),
            milestones=milestones,
            read_scope=read_scope,
            write_scope=write_scope,
            verification=verification,
            risks=risks,
            assumptions=assumptions,
            budget=budget,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project": self.project,
            "goal": self.goal,
            "milestones": list(self.milestones),
            "read_scope": list(self.read_scope),
            "write_scope": list(self.write_scope),
            "verification": list(self.verification),
            "risks": list(self.risks),
            "assumptions": list(self.assumptions),
            "mutation_budget": self.budget.to_dict(),
        }


@dataclass(frozen=True)
class ChangeReport:
    changed: tuple[str, ...]
    added: tuple[str, ...]
    deleted: tuple[str, ...]
    lines_changed: int
    config_paths: tuple[str, ...]
    dependency_paths: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "changed": list(self.changed),
            "added": list(self.added),
            "deleted": list(self.deleted),
            "lines_changed": self.lines_changed,
            "config_paths": list(self.config_paths),
            "dependency_paths": list(self.dependency_paths),
        }


def analyze_changes(source: Path, workspace: Path) -> ChangeReport:
    before = inventory(source)
    after = inventory(workspace)
    changed: list[str] = []
    added: list[str] = []
    deleted: list[str] = []
    lines = 0

    for rel in sorted(set(before) | set(after)):
        if rel not in after:
            changed.append(rel)
            deleted.append(rel)
            try:
                lines += len(before[rel].read_text(encoding="utf-8").splitlines())
            except UnicodeError:
                lines += 1
            continue
        if rel not in before:
            changed.append(rel)
            added.append(rel)
            try:
                lines += len(after[rel].read_text(encoding="utf-8").splitlines())
            except UnicodeError:
                lines += 1
            continue
        old = before[rel].read_bytes()
        new = after[rel].read_bytes()
        if old == new:
            continue
        changed.append(rel)
        try:
            old_lines = old.decode("utf-8").splitlines()
            new_lines = new.decode("utf-8").splitlines()
            for item in difflib.ndiff(old_lines, new_lines):
                if item.startswith("+ ") or item.startswith("- "):
                    lines += 1
        except UnicodeError:
            lines += 1

    config_paths = tuple(x for x in changed if is_config_path(x))
    dependency_paths = tuple(x for x in changed if is_dependency_path(x))
    return ChangeReport(
        changed=tuple(changed),
        added=tuple(added),
        deleted=tuple(deleted),
        lines_changed=lines,
        config_paths=config_paths,
        dependency_paths=dependency_paths,
    )


def contract_violations(plan: Plan, report: ChangeReport) -> list[str]:
    problems: list[str] = []
    if len(report.changed) > plan.budget.max_files_changed:
        problems.append(
            f"changed {len(report.changed)} files; budget allows {plan.budget.max_files_changed}"
        )
    if report.lines_changed > plan.budget.max_lines_changed:
        problems.append(
            f"changed {report.lines_changed} lines; budget allows {plan.budget.max_lines_changed}"
        )
    outside = [p for p in report.changed if not path_matches(p, plan.write_scope)]
    if outside:
        problems.append("outside write scope: " + ", ".join(outside))
    if report.added and not plan.budget.allow_new_files:
        problems.append("new files are not approved: " + ", ".join(report.added))
    if report.deleted and not plan.budget.allow_file_deletion:
        problems.append("file deletion is not approved: " + ", ".join(report.deleted))
    if report.config_paths and not plan.budget.allow_config_changes:
        problems.append("configuration changes need approval: " + ", ".join(report.config_paths))
    if report.dependency_paths and not plan.budget.allow_dependency_changes:
        problems.append(
            "dependency changes need approval: " + ", ".join(report.dependency_paths)
        )
    return problems


def deviation_amendment(plan: Plan, report: ChangeReport, reason: str) -> dict:
    extra = [p for p in report.changed if not path_matches(p, plan.write_scope)]
    return {
        "reason": reason[:4000],
        "add_write_scope": extra,
        "mutation_budget": {
            "max_files_changed": max(plan.budget.max_files_changed, len(report.changed)),
            "max_lines_changed": max(plan.budget.max_lines_changed, report.lines_changed),
            "allow_new_files": plan.budget.allow_new_files or bool(report.added),
            "allow_file_deletion": plan.budget.allow_file_deletion or bool(report.deleted),
            "allow_dependency_changes": (
                plan.budget.allow_dependency_changes or bool(report.dependency_paths)
            ),
            "allow_config_changes": plan.budget.allow_config_changes or bool(report.config_paths),
            "allow_network": plan.budget.allow_network,
            "max_attempts_per_milestone": plan.budget.max_attempts_per_milestone,
        },
        "observed_changes": report.to_dict(),
    }


def apply_amendment(plan: Plan, amendment: dict) -> Plan:
    raw = plan.to_dict()
    for item in amendment.get("add_write_scope", []):
        item = _safe_pattern(item)
        if item not in raw["write_scope"]:
            raw["write_scope"].append(item)
    if "mutation_budget" in amendment:
        raw["mutation_budget"] = MutationBudget.from_dict(
            amendment["mutation_budget"]
        ).to_dict()
    return Plan.from_dict(raw, plan.project, plan.id)


def pretty_plan(plan: Plan) -> str:
    return json.dumps(plan.to_dict(), indent=2, ensure_ascii=False)
