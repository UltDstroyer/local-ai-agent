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
            self.assertTrue(store.approve_plan(plan.id))
            self.assertEqual(store.next_plan()["status"], "running")
            did = store.create_deviation(
                plan.id, "demo", "needs another file",
                {
                    "type": "mutation_amendment",
                    "add_write_scope": ["helper.py"],
                    "mutation_budget": raw_plan()["mutation_budget"],
                },
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
            self.assertTrue(store.approve_plan(plan.id))
            self.assertEqual(store.next_plan()["status"], "running")
            first = store.create_deviation(
                plan.id, "demo", "first", {
                    "type": "mutation_amendment",
                    "add_write_scope": ["helper.py"],
                    "mutation_budget": raw_plan()["mutation_budget"],
                }
            )
            second = store.create_deviation(
                plan.id, "demo", "second", {
                    "type": "mutation_amendment",
                    "add_write_scope": ["other.py"],
                    "mutation_budget": raw_plan()["mutation_budget"],
                }
            )
            self.assertEqual(first, second)
            pending = [
                item for item in store.list_deviations(plan=plan.id)
                if item["status"] == "pending"
            ]
            self.assertEqual(len(pending), 1)


    def test_deviation_cannot_be_created_before_plan_is_running(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = Store(root / "state.sqlite3")
            plan = Plan.from_dict(raw_plan(), "demo")
            store.create_plan("demo", plan.goal, plan.to_dict())
            with self.assertRaisesRegex(ValueError, "no longer running"):
                store.create_deviation(
                    plan.id,
                    "demo",
                    "should not be possible",
                    {
                        "type": "manager_escalation",
                        "boss_request": "Choose whether this optional behavior is wanted.",
                    },
                )
            self.assertEqual(
                store.get_plan(plan.id)["status"],
                "awaiting_approval",
            )

    def test_cancelled_plan_cannot_be_resurrected_by_stale_worker_update(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = Store(root / "state.sqlite3")
            plan = Plan.from_dict(raw_plan(), "demo")
            store.create_plan("demo", plan.goal, plan.to_dict())
            self.assertTrue(store.approve_plan(plan.id))
            self.assertIsNotNone(store.next_plan())
            self.assertTrue(store.cancel_plan(plan.id))
            with self.assertRaisesRegex(ValueError, "cannot be resurrected"):
                store.update_plan(plan.id, status="approved")
            self.assertEqual(store.get_plan(plan.id)["status"], "cancelled")

    def test_generic_update_cannot_bypass_owner_plan_approval(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = Store(root / "state.sqlite3")
            plan = Plan.from_dict(raw_plan(), "demo")
            store.create_plan("demo", plan.goal, plan.to_dict())
            with self.assertRaisesRegex(ValueError, "dedicated transition"):
                store.update_plan(plan.id, status="approved")
            self.assertEqual(
                store.get_plan(plan.id)["status"],
                "awaiting_approval",
            )


    def test_internal_cancel_request_becomes_failed_internal_not_cancelled(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = Store(root / "state.sqlite3")
            plan = Plan.from_dict(raw_plan(), "demo")
            store.create_plan("demo", plan.goal, plan.to_dict())
            self.assertTrue(store.approve_plan(plan.id))
            self.assertEqual(store.next_plan()["status"], "running")
            store.update_plan(
                plan.id,
                status="cancelled",
                result="internal recovery exhausted",
                context={},
            )
            row = store.get_plan(plan.id)
            self.assertEqual(row["status"], "failed_internal")
            self.assertTrue(row["context"]["recovery_plan_required"])
            self.assertEqual(
                row["context"]["last_internal_failure"],
                "internal recovery exhausted",
            )

    def test_owner_cancel_cannot_be_retried_as_internal_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = Store(root / "state.sqlite3")
            plan = Plan.from_dict(raw_plan(), "demo")
            store.create_plan("demo", plan.goal, plan.to_dict())
            self.assertTrue(store.approve_plan(plan.id))
            self.assertEqual(store.next_plan()["status"], "running")
            self.assertTrue(store.cancel_plan(plan.id))
            self.assertFalse(store.retry_internal_plan(plan.id))
            self.assertEqual(store.get_plan(plan.id)["status"], "cancelled")


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
