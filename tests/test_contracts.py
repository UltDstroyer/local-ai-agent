import tempfile
import unittest
from pathlib import Path

from ctx_agent.contracts import (
    ContractError,
    Plan,
    analyze_changes,
    apply_amendment,
    contract_violations,
    deviation_amendment,
)


def plan_dict(**budget_overrides):
    budget = {
        "max_files_changed": 2,
        "max_lines_changed": 20,
        "allow_new_files": False,
        "allow_file_deletion": False,
        "allow_dependency_changes": False,
        "allow_config_changes": False,
        "allow_network": False,
        "max_attempts_per_milestone": 3,
    }
    budget.update(budget_overrides)
    return {
        "goal": "Change one function",
        "milestones": ["Make the narrow implementation", "Verify behavior"],
        "read_scope": ["**"],
        "write_scope": ["src/main.py", "tests/test_main.py"],
        "verification": ["Tests pass"],
        "risks": [],
        "assumptions": [],
        "mutation_budget": budget,
    }


class ContractTests(unittest.TestCase):
    def test_rejects_traversal_scope(self):
        raw = plan_dict()
        raw["write_scope"] = ["../outside"]
        with self.assertRaises(ContractError):
            Plan.from_dict(raw, "demo")

    def test_detects_out_of_scope_and_new_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            work = root / "work"
            (source / "src").mkdir(parents=True)
            (work / "src").mkdir(parents=True)
            (source / "src/main.py").write_text("x = 1\n")
            (work / "src/main.py").write_text("x = 2\n")
            (work / "surprise.py").write_text("oops = True\n")
            plan = Plan.from_dict(plan_dict(), "demo")
            report = analyze_changes(source, work)
            violations = contract_violations(plan, report)
            self.assertTrue(any("outside write scope" in x for x in violations))
            self.assertTrue(any("new files are not approved" in x for x in violations))

    def test_config_change_needs_explicit_permission(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            work = root / "work"
            source.mkdir()
            work.mkdir()
            (source / "flake.nix").write_text("{}\n")
            (work / "flake.nix").write_text("{ outputs = _: {}; }\n")
            raw = plan_dict(allow_new_files=True)
            raw["write_scope"] = ["flake.nix"]
            plan = Plan.from_dict(raw, "demo")
            report = analyze_changes(source, work)
            self.assertTrue(any(
                "configuration changes need approval" in x
                for x in contract_violations(plan, report)
            ))

    def test_approved_amendment_expands_only_requested_scope(self):
        plan = Plan.from_dict(plan_dict(), "demo", "abc123def456")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            work = root / "work"
            source.mkdir()
            work.mkdir()
            (source / "src").mkdir()
            (work / "src").mkdir()
            (source / "src/main.py").write_text("x = 1\n")
            (work / "src/main.py").write_text("x = 2\n")
            (work / "src/helper.py").write_text("y = 3\n")
            report = analyze_changes(source, work)
            amendment = deviation_amendment(plan, report, "helper needed")
            updated = apply_amendment(plan, amendment)
            self.assertIn("src/helper.py", updated.write_scope)
            self.assertEqual(updated.id, plan.id)


if __name__ == "__main__":
    unittest.main()
