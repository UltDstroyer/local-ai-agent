import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ctx_agent.remote import (
    RemoteBudget,
    RemoteContractError,
    RemotePlan,
    RemoteTarget,
    validate_remote_action,
)
from ctx_agent.remote_orchestrator import (
    _ask_json_object,
    _json_object,
    _completion_review,
    _manager_scope_review,
    _remote_actions_error,
    _remote_context_dir,
    _scope_risk,
    _validated_remote_plan,
    classify_remote_error,
)
from ctx_agent.remote_store import RemoteStore
from ctx_agent.ssh_bridge import SSHBridge, SSHBridgeError, _ssh_base


def budget(**overrides):
    raw = {
        "allowed_read_paths": [
            "/etc/nginx/**",
            "/etc/cloudflared/**",
            "/etc/os-release",
        ],
        "allowed_write_paths": [
            "/etc/nginx/**",
            "/etc/cloudflared/**",
            "/srv/jellyfin/**",
        ],
        "allowed_services": ["nginx", "jellyfin", "cloudflared"],
        "allowed_packages": ["nginx", "jellyfin", "cloudflared"],
        "allowed_domains": ["example.com"],
        "allow_sudo": True,
        "allow_package_install": True,
        "allow_network": True,
        "allow_remote_admin": False,
        "max_actions": 30,
        "max_file_writes": 8,
        "max_attempts_per_milestone": 3,
        "backup_required": True,
    }
    raw.update(overrides)
    return raw


class RemoteContractTests(unittest.TestCase):
    def test_target_parser(self):
        target = RemoteTarget.parse_ssh("pi", "pi@raspberrypi", 2222)
        self.assertEqual(target.user, "pi")
        self.assertEqual(target.host, "raspberrypi")
        self.assertEqual(target.port, 2222)

    def test_target_rejects_shell_metacharacters(self):
        with self.assertRaises(RemoteContractError):
            RemoteTarget.parse_ssh("pi", "pi@host;rm")

    def test_write_outside_scope_is_denied(self):
        b = RemoteBudget.from_dict(budget())
        with self.assertRaises(RemoteContractError):
            validate_remote_action({
                "op": "write_file",
                "path": "/etc/ssh/sshd_config",
                "content": "nope",
                "mode": "0644",
            }, b)

    def test_package_must_be_allowlisted(self):
        b = RemoteBudget.from_dict(budget())
        with self.assertRaises(RemoteContractError):
            validate_remote_action({
                "op": "package_install",
                "packages": ["openssh-server"],
            }, b)

    def test_service_mutation_requires_sudo_capability(self):
        b = RemoteBudget.from_dict(budget(allow_sudo=False))
        with self.assertRaises(RemoteContractError):
            validate_remote_action({
                "op": "service",
                "service": "nginx",
                "action": "restart",
            }, b)

    def test_service_scope_accepts_service_suffix_alias(self):
        b = RemoteBudget.from_dict(budget())
        action = validate_remote_action({
            "op": "check",
            "check": "journal_tail",
            "service": "jellyfin.service",
        }, b)
        self.assertEqual(action["service"], "jellyfin.service")

    def test_service_scope_still_rejects_unapproved_units(self):
        b = RemoteBudget.from_dict(budget())
        with self.assertRaisesRegex(
            RemoteContractError, "unapproved service: ssh.service"
        ):
            validate_remote_action({
                "op": "check",
                "check": "systemctl_is_active",
                "service": "ssh.service",
            }, b)

    def test_no_arbitrary_shell_action_exists(self):
        b = RemoteBudget.from_dict(budget())
        with self.assertRaises(RemoteContractError):
            validate_remote_action({
                "op": "shell",
                "command": "rm -rf /",
            }, b)

    def test_remote_exec_requires_explicit_admin_capability(self):
        b = RemoteBudget.from_dict(budget())
        with self.assertRaises(RemoteContractError):
            validate_remote_action({
                "op": "remote_exec",
                "command": "apt-get autoremove -y",
                "as_root": True,
            }, b)

    def test_remote_exec_can_be_scoped_to_ssh_plan(self):
        b = RemoteBudget.from_dict(budget(
            allow_remote_admin=True,
            allow_sudo=True,
        ))
        action = validate_remote_action({
            "op": "remote_exec",
            "command": "apt-get autoremove -y",
            "as_root": True,
            "timeout": 1200,
        }, b)
        self.assertEqual(action["op"], "remote_exec")
        self.assertTrue(action["as_root"])
        self.assertEqual(action["timeout"], 1200)

    def test_remote_plan_parses_visible_budget(self):
        raw = {
            "goal": "Set up media services",
            "milestones": ["Install", "Configure", "Verify"],
            "verification": ["All services active"],
            "risks": ["Remote outage"],
            "assumptions": ["Debian"],
            "remote_budget": budget(),
        }
        plan = RemotePlan.from_dict(raw, "pi")
        self.assertEqual(plan.target, "pi")
        self.assertTrue(plan.budget.backup_required)


class RemotePlanValidationRepairTests(unittest.TestCase):
    def test_invalid_domain_reports_offending_value(self):
        raw = budget(allowed_domains=["https://pkg.cloudflare.com"])
        with self.assertRaisesRegex(
            RemoteContractError,
            "https://pkg.cloudflare.com",
        ):
            RemoteBudget.from_dict(raw)

    def test_invalid_remote_plan_is_repaired_once(self):
        target = RemoteTarget.parse_ssh("pi", "pi@raspberrypi")
        invalid = {
            "goal": "Rebuild media server",
            "milestones": ["Configure"],
            "verification": ["Healthy"],
            "risks": [],
            "assumptions": [],
            "remote_budget": budget(
                allowed_domains=["https://pkg.cloudflare.com"],
                allow_remote_admin=True,
            ),
        }
        repaired = {
            **invalid,
            "remote_budget": budget(
                allowed_domains=["pkg.cloudflare.com"],
                allow_remote_admin=True,
            ),
        }
        with patch(
            "ctx_agent.remote_orchestrator._ask_json_object",
            return_value=repaired,
        ) as mocked:
            plan = _validated_remote_plan(
                SimpleNamespace(),
                invalid,
                target,
                "abc123def456",
                "Rebuild media server",
                "balanced",
            )
        self.assertEqual(plan.budget.allowed_domains, ("pkg.cloudflare.com",))
        self.assertTrue(plan.budget.allow_remote_admin)
        self.assertEqual(mocked.call_count, 1)


class RemoteJSONTests(unittest.TestCase):
    def test_json_object_accepts_markdown_fence(self):
        self.assertEqual(
            _json_object('```json\n{"agreement": true}\n```'),
            {"agreement": True},
        )

    def test_json_object_wraps_decode_error_as_contract_error(self):
        with self.assertRaises(RemoteContractError):
            _json_object('{"goal": "media",}')



class RemoteManagerApprovalTests(unittest.TestCase):
    def test_scope_risk_keeps_remote_exec_owner_gated(self):
        self.assertEqual(_scope_risk({"op": "remote_exec", "command": "id"}), "high")
        self.assertEqual(
            _scope_risk({"op": "check", "check": "journal_tail", "service": "nginx"}),
            "low",
        )

    def test_manager_can_approve_narrow_service_check(self):
        plan = RemotePlan.from_dict({
            "goal": "Check Jellyfin",
            "milestones": ["Inspect"],
            "verification": ["Healthy"],
            "risks": [],
            "assumptions": [],
            "remote_budget": budget(),
        }, "pi")
        with patch(
            "ctx_agent.remote_orchestrator._ask_json_object",
            return_value={"approve": True, "escalate": False, "reason": "on task"},
        ):
            review = _manager_scope_review(
                SimpleNamespace(),
                plan,
                "Inspect",
                {"op": "check", "check": "journal_tail", "service": "new.service"},
                "check references unapproved service: new.service",
                "new.service loaded",
                "balanced",
            )
        self.assertTrue(review["approve"])

    def test_manager_cannot_auto_approve_remote_exec(self):
        plan = RemotePlan.from_dict({
            "goal": "Maintain server",
            "milestones": ["Maintain"],
            "verification": ["Healthy"],
            "risks": [],
            "assumptions": [],
            "remote_budget": budget(),
        }, "pi")
        review = _manager_scope_review(
            SimpleNamespace(),
            plan,
            "Maintain",
            {"op": "remote_exec", "command": "id", "as_root": False},
            "arbitrary remote administration is not approved",
            "",
            "balanced",
        )
        self.assertFalse(review["approve"])
        self.assertTrue(review["escalate"])

    def test_error_taxonomy_distinguishes_policy_and_timeout(self):
        self.assertEqual(
            classify_remote_error(RemoteContractError("scope"))["type"],
            "policy_scope",
        )
        self.assertEqual(
            classify_remote_error(RuntimeError("operation timed out"))["type"],
            "timeout",
        )


class RemoteActionProposalTests(unittest.TestCase):
    def test_action_shape_error_reports_exact_problem(self):
        self.assertEqual(
            _remote_actions_error({"op": "check"}),
            "Codex proposal field 'actions' must be a list; got dict",
        )
        self.assertIn("at least one", _remote_actions_error([]))
        self.assertIn("21 remote actions", _remote_actions_error([{}] * 21))
        self.assertIsNone(_remote_actions_error([{"op": "check"}]))


class RemoteManagerRoleTests(unittest.TestCase):
    def test_completion_review_is_milestone_level(self):
        settings = SimpleNamespace()
        with patch("ctx_agent.remote_orchestrator.ask") as mocked:
            mocked.return_value = {
                "content": (
                    '{"state":"complete","guidance":"","context_note":"ok",'
                    '"reason":"verified"}'
                )
            }
            value = _completion_review(
                settings,
                RemotePlan.from_dict({
                    "goal": "Configure nginx",
                    "milestones": ["Configure"],
                    "verification": ["nginx active"],
                    "risks": [],
                    "assumptions": [],
                    "remote_budget": budget(),
                }, "pi"),
                "Configure",
                [{"ok": True}],
                "nginx active",
                "balanced",
            )
        self.assertEqual(value["state"], "complete")
        prompt = mocked.call_args.args[1][1]["content"]
        self.assertIn("Do not re-litigate individual in-contract actions", prompt)


class RemoteJSONRetryTests(unittest.TestCase):
    def test_structured_json_retries_once_with_larger_limits(self):
        settings = SimpleNamespace()
        malformed = {"content": '{"goal": "media",'}
        valid = {"content": '{"goal": "media"}'}
        with patch(
            "ctx_agent.remote_orchestrator.ask",
            side_effect=[malformed, valid],
        ) as mocked:
            value = _ask_json_object(settings, [{"role": "system", "content": "JSON"}], "balanced")
        self.assertEqual(value, {"goal": "media"})
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(mocked.call_args.kwargs["num_predict"], 3072)
        self.assertEqual(mocked.call_args.kwargs["num_ctx"], 8192)


class RemoteWorkspaceTests(unittest.TestCase):
    def test_context_dir_inherits_setgid_from_workspace_parent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspaces"
            root.mkdir(mode=0o770)
            root.chmod(0o2770)
            settings = SimpleNamespace(workspaces_dir=root)

            child = _remote_context_dir(settings, "abc123")

            self.assertTrue(child.is_dir())
            self.assertTrue(child.stat().st_mode & stat.S_ISGID)



class RemoteStoreTests(unittest.TestCase):
    def test_target_and_plan_persist(self):
        with tempfile.TemporaryDirectory() as td:
            store = RemoteStore(Path(td) / "remote.sqlite3")
            target = RemoteTarget.parse_ssh("pi", "pi@raspberrypi")
            store.put_target(target.to_dict())
            self.assertEqual(store.get_target("pi")["host"], "raspberrypi")

            plan = RemotePlan.from_dict({
                "goal": "Configure nginx",
                "milestones": ["Configure"],
                "verification": ["nginx test passes"],
                "risks": [],
                "assumptions": [],
                "remote_budget": budget(),
            }, "pi")
            store.create_plan("pi", plan.goal, plan.to_dict())
            self.assertIsNone(store.next_plan())
            self.assertTrue(store.approve_plan(plan.id))
            self.assertEqual(store.next_plan()["status"], "running")

    def test_running_remote_plan_is_recovered_after_restart(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "remote.sqlite3"
            store = RemoteStore(path)
            target = RemoteTarget.parse_ssh("pi", "pi@raspberrypi")
            store.put_target(target.to_dict())
            plan = RemotePlan.from_dict({
                "goal": "Configure nginx",
                "milestones": ["Configure"],
                "verification": ["nginx active"],
                "risks": [],
                "assumptions": [],
                "remote_budget": budget(),
            }, "pi")
            store.create_plan("pi", plan.goal, plan.to_dict())
            store.approve_plan(plan.id)
            self.assertEqual(store.next_plan()["status"], "running")
            store.db.close()

            recovered = RemoteStore(path)
            row = recovered.get_plan(plan.id)
            self.assertEqual(row["status"], "approved")
            self.assertIn("restarted", row["context"]["next_guidance"])
            events = recovered.events(plan.id)
            self.assertTrue(any(
                e["event"] == "remote_plan_recovered_after_restart"
                for e in events
            ))

    def test_active_plan_prevents_target_removal(self):
        with tempfile.TemporaryDirectory() as td:
            store = RemoteStore(Path(td) / "remote.sqlite3")
            target = RemoteTarget.parse_ssh("pi", "pi@raspberrypi")
            store.put_target(target.to_dict())
            plan = RemotePlan.from_dict({
                "goal": "Configure nginx",
                "milestones": ["Configure"],
                "verification": ["nginx active"],
                "risks": [],
                "assumptions": [],
                "remote_budget": budget(),
            }, "pi")
            store.create_plan("pi", plan.goal, plan.to_dict())
            self.assertFalse(store.delete_target("pi"))


class SSHBridgeTests(unittest.TestCase):
    def test_ssh_forces_batch_mode_and_strict_host_keys(self):
        target = RemoteTarget.parse_ssh("pi", "pi@raspberrypi")
        with patch("ctx_agent.ssh_bridge._ssh_binary", return_value="/bin/ssh"):
            args = _ssh_base(target)
        joined = " ".join(args)
        self.assertIn("BatchMode=yes", joined)
        self.assertIn("PasswordAuthentication=no", joined)
        self.assertIn("StrictHostKeyChecking=yes", joined)

    def test_bridge_rejects_unknown_operation(self):
        target = RemoteTarget.parse_ssh("pi", "pi@raspberrypi")
        with self.assertRaises(SSHBridgeError):
            SSHBridge().dispatch({
                "op": "raw_shell",
                "target": target.to_dict(),
                "budget": budget(),
            })

    def test_remote_exec_is_forwarded_only_through_ssh_runner(self):
        target = RemoteTarget.parse_ssh("pi", "pi@raspberrypi")
        with patch("ctx_agent.ssh_bridge._run_script") as runner:
            runner.return_value = {"ok": True, "returncode": 0, "output": "ok"}
            result = SSHBridge().dispatch({
                "op": "action",
                "target": target.to_dict(),
                "budget": budget(
                    allow_remote_admin=True,
                    allow_sudo=True,
                ),
                "action": {
                    "op": "remote_exec",
                    "command": "id",
                    "as_root": True,
                    "timeout": 60,
                },
                "plan": "abc123def456",
                "milestone": 0,
            })
        self.assertTrue(result["ok"])
        called_target, called_script = runner.call_args.args
        self.assertEqual(called_target.host, "raspberrypi")
        self.assertIn("sudo -n sh -c", called_script)
        self.assertEqual(runner.call_args.kwargs["timeout"], 60)


if __name__ == "__main__":
    unittest.main()


class RemoteInvalidProposalRecoveryTests(unittest.TestCase):
    def test_repeated_invalid_proposal_is_recovered_internally(self):
        # The manager should recover malformed Codex action batches without
        # escalating a boss deviation merely because the employee returned bad JSON.
        self.assertIn(
            "remote_manager_recovered_invalid_proposal",
            Path(__file__).resolve().parents[1]
            .joinpath("ctx_agent", "remote_orchestrator.py")
            .read_text(),
        )
        self.assertNotIn(
            '"type": "invalid_proposal"',
            Path(__file__).resolve().parents[1]
            .joinpath("ctx_agent", "remote_orchestrator.py")
            .read_text(),
        )
