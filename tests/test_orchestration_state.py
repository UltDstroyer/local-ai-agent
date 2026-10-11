import tempfile
import unittest
from pathlib import Path

from ctx_agent.contracts import Plan
from ctx_agent.runtime import Settings
from ctx_agent.server import Coordinator
from ctx_agent.store import Store


def raw_plan():
    return {
        "goal": "Implement the requested feature",
        "milestones": ["Implement", "Verify"],
        "read_scope": ["**"],
        "write_scope": ["main.py"],
        "verification": ["Behavior is correct"],
        "risks": [],
        "assumptions": [],
        "mutation_budget": {
            "max_files_changed": 1,
            "max_lines_changed": 40,
            "allow_new_files": False,
            "allow_file_deletion": False,
            "allow_dependency_changes": False,
            "allow_config_changes": False,
            "allow_network": False,
            "max_attempts_per_milestone": 3,
        },
    }


class PlanStoreTests(unittest.TestCase):
    def test_plan_requires_owner_approval_before_worker_can_lease(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = Store(root / "state.sqlite3")
            plan = Plan.from_dict(raw_plan(), "demo")
            store.create_plan("demo", plan.goal, plan.to_dict())
            self.assertIsNone(store.next_plan())
            self.assertTrue(store.approve_plan(plan.id))
            leased = store.next_plan()
            self.assertEqual(leased["id"], plan.id)
            self.assertEqual(leased["status"], "running")

    def test_deviation_pauses_and_resolves(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = Store(root / "state.sqlite3")
            plan = Plan.from_dict(raw_plan(), "demo")
            store.create_plan("demo", plan.goal, plan.to_dict())
            did = store.create_deviation(
                plan.id, "demo", "needs another file",
                {"add_write_scope": ["helper.py"]},
            )
            self.assertEqual(store.get_plan(plan.id)["status"], "awaiting_deviation")
            self.assertTrue(store.resolve_deviation(did, True))
            self.assertEqual(store.get_deviation(did)["status"], "approved")




    def test_duplicate_pending_project_deviations_collapse(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = Store(root / "state.sqlite3")
            plan = Plan.from_dict(raw_plan(), "demo")
            store.create_plan("demo", plan.goal, plan.to_dict())
            first = store.create_deviation(
                plan.id, "demo", "first", {"add_write_scope": ["helper.py"]}
            )
            second = store.create_deviation(
                plan.id, "demo", "second", {"add_write_scope": ["other.py"]}
            )
            self.assertEqual(first, second)
            pending = [
                item for item in store.list_deviations(plan=plan.id)
                if item["status"] == "pending"
            ]
            self.assertEqual(len(pending), 1)


class CoordinatorApprovalTests(unittest.TestCase):
    def test_owner_approval_records_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            projects = root / "projects"
            projects.mkdir()
            settings = Settings(root, root / "agent.sock", projects)
            coordinator = Coordinator(settings)
            project = projects / "demo"
            project.mkdir()
            (project / "main.py").write_text("x = 1\n")
            plan = Plan.from_dict(raw_plan(), "demo")
            coordinator.store.create_plan("demo", plan.goal, plan.to_dict())
            result = coordinator.dispatch({
                "op": "plan_approve", "project": "demo", "id": plan.id
            })
            self.assertEqual(result["plan"]["status"], "approved")
            self.assertTrue((coordinator.plan_baselines / f"{plan.id}.json").exists())


if __name__ == "__main__":
    unittest.main()
