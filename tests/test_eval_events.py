import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ctx_agent.eval_events import (
    build_eval_event,
    emit_eval_event,
    iter_eval_events,
)
from ctx_agent.store import Store


class EvalEventContractTests(unittest.TestCase):
    def test_decision_request_drops_sensitive_explanation(self):
        event = build_eval_event(
            "local",
            "plan-1",
            "deviation_requested",
            "dev-123: password=hunter2 and a long internal explanation",
            at=123.0,
        )
        self.assertEqual(event["schema_version"], "1")
        self.assertEqual(event["kind"], "decision.requested")
        self.assertEqual(event["scope_id"], "plan-1")
        self.assertEqual(event["payload"]["decision_id"], "dev-123")
        encoded = json.dumps(event)
        self.assertNotIn("hunter2", encoded)
        self.assertNotIn("internal explanation", encoded)

    def test_owner_resolution_is_normalized(self):
        event = build_eval_event(
            "remote",
            "plan-2",
            "remote_deviation_rejected_by_owner",
            "dev-456",
            at=456.0,
        )
        self.assertEqual(event["kind"], "decision.resolved")
        self.assertEqual(event["actor"], "owner")
        self.assertEqual(event["payload"]["decision"], "rejected")
        self.assertEqual(event["payload"]["decision_id"], "dev-456")

    def test_unconfigured_export_is_noop(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(emit_eval_event(
                "local", "plan-3", "plan_created", "ignored"
            ))

    def test_jsonl_round_trip_skips_foreign_records(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.jsonl"
            self.assertTrue(emit_eval_event(
                "local", "plan-4", "plan_created", "demo", at=10.0, path=path
            ))
            with path.open("a", encoding="utf-8") as handle:
                handle.write("{not json}\n")
                handle.write('{"schema_version":"999","kind":"foreign","payload":{}}\n')

            events = list(iter_eval_events(path))
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["kind"], "plan.created")
            self.assertEqual(events[0]["at"], 10.0)

    def test_store_exports_linked_decision_request_and_resolution(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            export = root / "eval.jsonl"
            with patch.dict(
                os.environ, {"CTX_EVAL_EVENTS_PATH": str(export)}, clear=False
            ):
                store = Store(root / "state.db")
                store.create_plan(
                    "demo",
                    "test evaluator decisions",
                    {"id": "plan-eval-1"},
                )
                self.assertTrue(store.approve_plan("plan-eval-1"))
                self.assertIsNotNone(store.next_plan())
                did = store.create_deviation(
                    "plan-eval-1",
                    "demo",
                    "Need one extra file.",
                    {
                        "type": "mutation_amendment",
                        "mutation_budget": {"allow_new_files": True},
                        "add_write_scope": ["src/helper.py"],
                    },
                )
                self.assertTrue(store.resolve_deviation(did, True))

            events = list(iter_eval_events(export))
            decisions = [
                event for event in events
                if event["kind"] in {"decision.requested", "decision.resolved"}
            ]
            self.assertEqual(len(decisions), 2)
            self.assertEqual(
                decisions[0]["payload"]["decision_id"],
                decisions[1]["payload"]["decision_id"],
            )
            self.assertEqual(decisions[1]["payload"]["decision"], "approved")


if __name__ == "__main__":
    unittest.main()
