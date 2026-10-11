import unittest

from ctx_agent.progress import plan_snapshot, planning_snapshot


class PlanningProgressTests(unittest.TestCase):
    def test_planning_snapshot_reports_stage_and_eta(self):
        state = {
            "kind": "remote_plan",
            "scope": "pi",
            "stages": [
                {"name": "Inspect target", "estimated_seconds": 30},
                {"name": "Codex drafts plan", "estimated_seconds": 180},
            ],
            "stage_index": 1,
            "started_at": 100.0,
            "stage_started_at": 120.0,
            "planned_stages": ["Inspect storage", "Configure services"],
            "active": True,
            "status": "running",
        }
        progress = planning_snapshot(state, now=150.0)
        self.assertEqual(progress["current_stage_name"], "Codex drafts plan")
        self.assertEqual(progress["current_stage_index"], 2)
        self.assertEqual(progress["planned_execution_stages"], [
            "Inspect storage", "Configure services"
        ])
        self.assertGreater(progress["estimated_remaining_seconds"], 0)
        self.assertEqual(progress["elapsed_seconds"], 50)


class PlanProgressTests(unittest.TestCase):
    def row(self, status="running", current=1):
        return {
            "id": "abc123def456",
            "status": status,
            "current_milestone": current,
            "created": 100.0,
            "plan": {
                "milestones": ["Inspect storage", "Configure Caddy", "Verify Jellyfin"]
            },
        }

    def test_running_remote_plan_shows_current_stage_and_time_left(self):
        events = [
            {"at": 110.0, "event": "remote_plan_approved_by_owner", "details": ""},
            {"at": 170.0, "event": "remote_milestone_completed", "details": "1"},
        ]
        progress = plan_snapshot(
            self.row(),
            events,
            remote=True,
            mode="maximum",
            paused=False,
            now=200.0,
        )
        self.assertEqual(progress["current_stage_name"], "Configure Caddy")
        self.assertEqual(progress["current_stage_index"], 2)
        self.assertEqual(progress["stages"][0]["status"], "complete")
        self.assertEqual(progress["stages"][1]["status"], "running")
        self.assertIsNotNone(progress["estimated_remaining_seconds"])
        self.assertGreater(progress["estimated_work_remaining_seconds"], 0)

    def test_balanced_mode_reports_waiting_and_work_remaining(self):
        progress = plan_snapshot(
            self.row(status="approved", current=0),
            [{"at": 110.0, "event": "remote_plan_approved_by_owner", "details": ""}],
            remote=True,
            mode="balanced",
            paused=False,
            now=130.0,
        )
        self.assertEqual(progress["phase"], "waiting_for_maximum_mode")
        self.assertEqual(progress["waiting_on"], "maximum_mode")
        self.assertIsNone(progress["estimated_remaining_seconds"])
        self.assertGreater(progress["estimated_work_remaining_seconds"], 0)

    def test_completed_plan_reports_all_done(self):
        events = [
            {"at": 110.0, "event": "remote_plan_approved_by_owner", "details": ""},
            {"at": 150.0, "event": "remote_milestone_completed", "details": "1"},
            {"at": 190.0, "event": "remote_milestone_completed", "details": "2"},
            {"at": 230.0, "event": "remote_milestone_completed", "details": "3"},
            {"at": 250.0, "event": "remote_plan_completed", "details": ""},
        ]
        progress = plan_snapshot(
            self.row(status="completed", current=3),
            events,
            remote=True,
            mode="maximum",
            paused=False,
            now=260.0,
        )
        self.assertEqual(progress["percent_complete"], 100)
        self.assertEqual(progress["estimated_work_remaining_seconds"], 0)
        self.assertTrue(all(
            stage["status"] == "complete" for stage in progress["stages"]
        ))


if __name__ == "__main__":
    unittest.main()
