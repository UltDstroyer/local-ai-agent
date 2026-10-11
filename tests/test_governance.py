import unittest

from ctx_agent.governance import (
    owner_request_is_internal,
    valid_local_owner_request,
    valid_remote_owner_request,
)
from ctx_agent.server import Coordinator


class BossGovernanceTests(unittest.TestCase):
    def test_format_retry_can_never_be_boss_decision(self):
        self.assertTrue(owner_request_is_internal(
            "Approve retrying with corrected dependency format specification"
        ))
        self.assertFalse(valid_remote_owner_request({
            "type": "manager_escalation",
            "boss_request": "Approve retrying with corrected dependency format specification",
            "risk": "medium",
        }))

    def test_execution_budget_can_never_be_boss_decision(self):
        self.assertFalse(valid_remote_owner_request({
            "type": "manager_escalation",
            "boss_request": "Approve increasing the remote action budget from 30 to 31.",
            "risk": "low",
        }))

    def test_real_dependency_install_is_boss_eligible(self):
        self.assertTrue(valid_remote_owner_request({
            "type": "dependency_install",
            "boss_request": "Allow me to download and install cloudflared.",
            "risk": "medium",
            "proposed_action": {
                "op": "package_install",
                "packages": ["cloudflared"],
            },
        }))

    def test_real_service_activation_is_boss_eligible(self):
        self.assertTrue(valid_remote_owner_request({
            "type": "service_activation",
            "boss_request": "Allow me to enable the cloudflared service.",
            "risk": "medium",
            "proposed_action": {
                "op": "service",
                "service": "cloudflared.service",
                "action": "enable_now",
            },
        }))

    def test_human_input_requires_structured_one_item_request(self):
        self.assertTrue(valid_remote_owner_request({
            "type": "human_input",
            "boss_request": "Allow me to ask for one Cloudflare credential.",
            "input_request": {
                "key": "cloudflare_token",
                "prompt": "Cloudflare token",
                "reason": "Needed to authenticate the approved tunnel.",
                "sensitive": True,
            },
        }))
        self.assertFalse(valid_remote_owner_request({
            "type": "human_input",
            "boss_request": "Allow me to ask for a credential.",
            "input_request": {
                "prompt": "Token",
                "reason": "Needed.",
                "sensitive": True,
            },
        }))

    def test_raw_object_cannot_be_boss_request(self):
        self.assertFalse(valid_remote_owner_request({
            "type": "manager_escalation",
            "boss_request": {
                "key": "cloudflared_status",
                "prompt": "Is cloudflared running?",
            },
        }))

    def test_local_mutation_is_typed_owner_decision(self):
        self.assertTrue(valid_local_owner_request({
            "type": "mutation_amendment",
            "mutation_budget": {
                "allow_dependency_changes": True,
            },
        }))
        self.assertFalse(valid_local_owner_request({
            "type": "stalled",
            "boss_request": "Allow another retry cycle.",
        }))


class BossViewTests(unittest.TestCase):
    def test_public_plan_contract_drops_negotiation_transcript(self):
        public = Coordinator._public_plan_contract({
            "goal": "demo",
            "planning": {
                "codex_proposal": {"secret_internal": True},
            },
        })
        self.assertEqual(public, {"goal": "demo"})

    def test_public_deviation_list_drops_request_payload(self):
        public = Coordinator._public_deviations([{
            "id": "abc123def456",
            "status": "pending",
            "created": 1.0,
            "updated": 2.0,
            "reason": "internal detail",
            "request": {"error": "raw validator output"},
        }])
        self.assertEqual(public, [{
            "id": "abc123def456",
            "status": "pending",
            "created": 1.0,
            "updated": 2.0,
        }])


if __name__ == "__main__":
    unittest.main()
