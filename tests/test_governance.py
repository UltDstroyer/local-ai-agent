import unittest

from ctx_agent.governance import (
    owner_decision_key,
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


    def test_rephrased_same_remote_action_has_same_decision_identity(self):
        first = owner_decision_key({
            "type": "dependency_install",
            "boss_request": "Allow me to install cloudflared.",
            "proposed_action": {
                "op": "package_install",
                "packages": ["cloudflared"],
            },
        })
        second = owner_decision_key({
            "type": "dependency_install",
            "boss_request": "May I download and install cloudflared now?",
            "proposed_action": {
                "packages": ["cloudflared"],
                "op": "package_install",
            },
        })
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("action:"))

    def test_local_generic_escalation_cannot_request_typed_secret_input(self):
        self.assertFalse(valid_local_owner_request({
            "type": "manager_escalation",
            "boss_request": "Please paste your API token so Codex can continue.",
        }))
        self.assertFalse(valid_local_owner_request({
            "type": "manager_escalation",
            "boss_request": "Provide the password for this service.",
        }))

    def test_local_non_secret_preference_is_still_boss_eligible(self):
        self.assertTrue(valid_local_owner_request({
            "type": "manager_escalation",
            "boss_request": "Choose whether the generated report should use PDF or HTML.",
        }))

    def test_same_local_mutation_has_stable_identity(self):
        first = owner_decision_key({
            "type": "mutation_amendment",
            "mutation_budget": {
                "allow_dependency_changes": True,
                "max_files_changed": 4,
            },
            "add_write_scope": ["requirements.txt", "src/new.py"],
        })
        second = owner_decision_key({
            "type": "mutation_amendment",
            "boss_request": "Different wording does not matter.",
            "add_write_scope": ["src/new.py", "requirements.txt"],
            "mutation_budget": {
                "max_files_changed": 4,
                "allow_dependency_changes": True,
            },
        })
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("mutation:"))


    def test_generic_retry_escalations_are_never_boss_decisions(self):
        for request in (
            "Allow another retry for this milestone.",
            "Approve one more recovery cycle.",
            "May I debug the service and try again?",
        ):
            self.assertFalse(valid_remote_owner_request({
                "type": "manager_escalation",
                "boss_request": request,
            }))

    def test_generic_technical_authority_requires_typed_request(self):
        self.assertFalse(valid_remote_owner_request({
            "type": "manager_escalation",
            "boss_request": "Allow me to restart the nginx service.",
        }))
        self.assertFalse(valid_remote_owner_request({
            "type": "manager_escalation",
            "boss_request": "Allow me to edit the nginx config file.",
        }))
        self.assertFalse(valid_local_owner_request({
            "type": "manager_escalation",
            "boss_request": "Allow me to install a new dependency.",
        }))


    def test_observable_credential_path_input_is_not_boss_eligible(self):
        self.assertFalse(valid_remote_owner_request({
            "type": "human_input",
            "boss_request": "Allow me to ask for one Cloudflare input.",
            "input_request": {
                "key": "cloudflare_credentials_path",
                "prompt": "Cloudflare Tunnel credential path",
                "reason": "Needed to locate the credential file on the Pi.",
                "sensitive": False,
            },
        }))


if __name__ == "__main__":
    unittest.main()


class RejectionIdentityTests(unittest.TestCase):
    def test_human_input_identity_uses_key_not_wording(self):
        first = owner_decision_key({
            "type": "human_input",
            "boss_request": "Allow me to ask for the Cloudflare token.",
            "input_request": {
                "key": "cloudflare_token",
                "prompt": "Cloudflare token",
                "reason": "Needed for authentication.",
                "sensitive": True,
            },
        })
        paraphrase = owner_decision_key({
            "type": "human_input_refresh",
            "boss_request": "May I ask you to re-enter that credential?",
            "input_request": {
                "key": "cloudflare_token",
                "prompt": "Token again",
                "reason": "The coordinator restarted.",
                "sensitive": True,
            },
        })
        self.assertEqual(first, "input:cloudflare_token")
        self.assertEqual(first, paraphrase)

    def test_generic_decision_identity_is_stable_under_whitespace_and_case(self):
        first = owner_decision_key({
            "type": "manager_escalation",
            "boss_request": "Allow changing the public hostname.",
        })
        second = owner_decision_key({
            "type": "manager_escalation",
            "boss_request": "  ALLOW   changing the public hostname. ",
        })
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("decision:"))
