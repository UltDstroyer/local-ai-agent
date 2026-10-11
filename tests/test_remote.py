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
    _auto_expand_execution_budget,
    _json_object,
    _completion_review,
    _codex_manager_checkin_remote,
    _dependency_key,
    _dependency_owner_request,
    _dependency_request_from_checkin,
    _human_input_error,
    _is_remote_action_format_error,
    _manager_scope_review,
    _remote_actions_error,
    _remote_context_dir,
    _scope_risk,
    _validated_remote_plan,
    classify_remote_error,
)
from ctx_agent.remote_store import RemoteStore
from ctx_agent.ssh_bridge import (
    SSHBridge,
    SSHBridgeError,
    _resolve_action_inputs,
    _ssh_base,
)


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
            "remote_manager_repaired_invalid_proposal",
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


class EphemeralHumanInputTests(unittest.TestCase):
    def test_placeholder_resolves_only_inside_ssh_bridge(self):
        action = {
            "op": "remote_exec",
            "command": "tool --token {{input:cloudflare_token}}",
            "as_root": False,
            "timeout": 60,
        }
        resolved = _resolve_action_inputs(
            action, {"cloudflare_token": "secret-value"}
        )
        self.assertEqual(
            resolved["command"],
            "tool --token secret-value",
        )
        self.assertIn("{{input:cloudflare_token}}", action["command"])
        self.assertNotIn("secret-value", action["command"])

    def test_missing_ephemeral_input_fails_closed(self):
        action = {
            "op": "remote_exec",
            "command": "tool --token {{input:cloudflare_token}}",
            "as_root": False,
            "timeout": 60,
        }
        with self.assertRaisesRegex(SSHBridgeError, "human input is unavailable"):
            _resolve_action_inputs(action, {})

    def test_completion_review_accepts_one_input_request(self):
        settings = SimpleNamespace()
        response = {
            "content": (
                '{"state":"blocked","guidance":"continue after input",'
                '"context_note":"","reason":"authentication required",'
                '"owner_needed":true,'
                '"owner_request":"Allow me to request one credential.",'
                '"risk":"medium",'
                '"input_request":{'
                '"key":"cloudflare_token",'
                '"prompt":"Cloudflare token",'
                '"reason":"Needed to authenticate the existing tunnel.",'
                '"sensitive":true}}'
            )
        }
        with patch("ctx_agent.remote_orchestrator.ask", return_value=response):
            value = _completion_review(
                settings,
                RemotePlan.from_dict({
                    "goal": "Configure tunnel",
                    "milestones": ["Authenticate"],
                    "verification": ["Tunnel works"],
                    "risks": [],
                    "assumptions": [],
                    "remote_budget": budget(),
                }, "pi"),
                "Authenticate",
                [{"ok": False}],
                "authentication required",
                "balanced",
            )
        self.assertTrue(value["owner_needed"])
        self.assertEqual(value["input_request"]["key"], "cloudflare_token")
        self.assertTrue(value["input_request"]["sensitive"])


class HumanInputBoundaryTests(unittest.TestCase):
    def test_service_status_is_not_human_input(self):
        request = {
            "key": "cloudflared_status",
            "prompt": "Is cloudflared service running and properly configured?",
            "reason": "Needed to validate external routing.",
            "sensitive": False,
        }
        self.assertIn("system state", _human_input_error(request))

    def test_secret_credential_can_be_human_input(self):
        request = {
            "key": "cloudflare_token",
            "prompt": "Cloudflare token",
            "reason": "Needed to authenticate the existing tunnel.",
            "sensitive": True,
        }
        self.assertIsNone(_human_input_error(request))


class RemoteDependencyApprovalTests(unittest.TestCase):
    def test_cloudflared_install_is_owner_gated_even_when_budget_allows_it(self):
        plan = RemotePlan.from_dict({
            "goal": "Expose Jellyfin through Cloudflare",
            "milestones": ["Configure Cloudflare Tunnel"],
            "verification": ["External HTTPS works"],
            "risks": [],
            "assumptions": [],
            "remote_budget": budget(),
        }, "pi")
        action = {"op": "package_install", "packages": ["cloudflared"]}
        self.assertEqual(_dependency_key(action), "packages:cloudflared")
        request = _dependency_owner_request(
            plan,
            action,
            {"summary": "Cloudflared is absent and the tunnel requires it."},
        )
        self.assertEqual(request["type"], "dependency_install")
        self.assertIn("cloudflared", request["boss_request"])
        self.assertIn("download and install", request["boss_request"])

    def test_new_service_activation_is_owner_gated(self):
        plan = RemotePlan.from_dict({
            "goal": "Expose Jellyfin through Cloudflare",
            "milestones": ["Configure Cloudflare Tunnel"],
            "verification": ["Tunnel active"],
            "risks": [],
            "assumptions": [],
            "remote_budget": budget(),
        }, "pi")
        action = {
            "op": "service",
            "service": "cloudflared.service",
            "action": "enable_now",
        }
        self.assertEqual(_dependency_key(action), "service:cloudflared")
        request = _dependency_owner_request(
            plan,
            action,
            {"summary": "The tunnel requires the Cloudflare daemon."},
        )
        self.assertEqual(request["type"], "service_activation")
        self.assertIn("cloudflared.service", request["boss_request"])

    def test_service_restart_does_not_create_new_dependency_approval(self):
        self.assertIsNone(_dependency_key({
            "op": "service",
            "service": "nginx.service",
            "action": "restart",
        }))

    def test_manager_cannot_auto_approve_package_install(self):
        plan = RemotePlan.from_dict({
            "goal": "Configure tunnel",
            "milestones": ["Configure"],
            "verification": ["Works"],
            "risks": [],
            "assumptions": [],
            "remote_budget": budget(allowed_packages=[]),
        }, "pi")
        review = _manager_scope_review(
            SimpleNamespace(),
            plan,
            "Configure",
            {"op": "package_install", "packages": ["cloudflared"]},
            "package is not approved",
            "cloudflared command not found",
            "balanced",
        )
        self.assertFalse(review["approve"])
        self.assertTrue(review["escalate"])
        self.assertIn("owner approval", review["reason"])


class CodexManagerCollaborationTests(unittest.TestCase):
    def test_codex_can_override_manager_with_better_strategy(self):
        plan = RemotePlan.from_dict({
            "goal": "Configure Cloudflare Tunnel",
            "milestones": ["Make tunnel available"],
            "verification": ["Tunnel works"],
            "risks": [],
            "assumptions": [],
            "remote_budget": budget(),
        }, "pi")
        row = {
            "current_milestone": 0,
            "context": {"history": []},
        }
        response = {
            "ok": True,
            "message": json.dumps({
                "agreement": False,
                "better_strategy": "Install cloudflared first, then configure the tunnel.",
                "guidance_to_manager": "Ask for installation approval before continuing.",
                "owner_needed": True,
                "owner_request": "Allow installation of cloudflared.",
                "risk": "medium",
            }),
        }
        with patch(
            "ctx_agent.remote_orchestrator.bridge_call",
            return_value=response,
        ):
            value = _codex_manager_checkin_remote(
                SimpleNamespace(codex_bridge_socket=Path("/tmp/fake")),
                plan,
                row,
                Path("/tmp"),
                "thread",
                "cloudflared not found",
                [],
                {
                    "state": "blocked",
                    "guidance": "inspect again",
                    "reason": "tunnel unavailable",
                    "owner_needed": False,
                },
                "balanced",
            )
        self.assertFalse(value["agreement"])
        self.assertIn("Install cloudflared", value["better_strategy"])
        self.assertTrue(value["owner_needed"])
        self.assertEqual(
            value["owner_request"],
            "Allow installation of cloudflared.",
        )


class ExecutionBudgetAutonomyTests(unittest.TestCase):
    def test_qwen_can_raise_action_counter_without_owner(self):
        plan = RemotePlan.from_dict({
            "goal": "Finish server setup",
            "milestones": ["Validate routing"],
            "verification": ["Routing works"],
            "risks": [],
            "assumptions": [],
            "remote_budget": budget(max_actions=30),
        }, "pi")
        class Store:
            def __init__(self):
                self.updated = None
                self.events = []
            def update_plan(self, pid, **kwargs):
                self.updated = (pid, kwargs)
            def event(self, pid, event, detail=""):
                self.events.append((pid, event, detail))
        store = Store()
        context = {}
        updated, new_context = _auto_expand_execution_budget(
            store,
            plan,
            context,
            requested_actions=31,
        )
        self.assertEqual(updated.budget.max_actions, 31)
        self.assertEqual(
            new_context["original_execution_budget"]["max_actions"], 30
        )
        self.assertTrue(any(
            event == "remote_execution_budget_expanded_by_manager"
            for _, event, _ in store.events
        ))

    def test_qwen_budget_autonomy_is_bounded(self):
        plan = RemotePlan.from_dict({
            "goal": "Finish server setup",
            "milestones": ["Validate routing"],
            "verification": ["Routing works"],
            "risks": [],
            "assumptions": [],
            "remote_budget": budget(max_actions=30),
        }, "pi")
        class Store:
            def update_plan(self, *args, **kwargs):
                pass
            def event(self, *args, **kwargs):
                pass
        with self.assertRaisesRegex(
            Exception, "above manager cap"
        ):
            _auto_expand_execution_budget(
                Store(),
                plan,
                {},
                requested_actions=200,
            )


class MissingDependencyEscalationTests(unittest.TestCase):
    def test_cloudflared_missing_becomes_install_approval(self):
        plan = RemotePlan.from_dict({
            "goal": "Expose Jellyfin through Cloudflare",
            "milestones": ["Validate Cloudflare Tunnel routing"],
            "verification": ["External HTTPS works"],
            "risks": [],
            "assumptions": [],
            "remote_budget": budget(),
        }, "pi")
        request = _dependency_request_from_checkin(
            plan,
            {
                "kind": "package",
                "name": "cloudflared",
                "reason": (
                    "The cloudflared binary is absent and is required to run "
                    "the Cloudflare Tunnel."
                ),
            },
        )
        self.assertIsNotNone(request)
        self.assertEqual(request["type"], "dependency_install")
        self.assertIn("cloudflared", request["boss_request"])
        self.assertIn("download and install", request["boss_request"])


class TypedActionFormatRecoveryTests(unittest.TestCase):
    def test_bad_package_shape_is_internal_format_error(self):
        b = RemoteBudget.from_dict(budget())
        with self.assertRaisesRegex(
            RemoteContractError,
            "invalid package_install format",
        ):
            validate_remote_action({
                "op": "package_install",
                "packages": "cloudflared",
            }, b)
        self.assertTrue(_is_remote_action_format_error(
            "invalid package_install format: packages must be a non-empty list"
        ))

    def test_bad_service_shape_is_internal_format_error(self):
        b = RemoteBudget.from_dict(budget())
        with self.assertRaisesRegex(
            RemoteContractError,
            "invalid service format",
        ):
            validate_remote_action({
                "op": "service",
                "service": "",
                "action": "enable_now",
            }, b)
        self.assertTrue(_is_remote_action_format_error(
            "invalid service format: service must be a non-empty unit name"
        ))

    def test_valid_new_dependency_remains_owner_gated(self):
        b = RemoteBudget.from_dict(budget())
        action = validate_remote_action({
            "op": "package_install",
            "packages": ["cloudflared"],
        }, b)
        self.assertEqual(action["packages"], ["cloudflared"])
        self.assertFalse(_is_remote_action_format_error(
            "package installation is not approved"
        ))


class RemoteWorkerEscalationBoundaryTests(unittest.TestCase):
    def test_action_format_failure_is_not_owner_deviation(self):
        source = (
            Path(__file__).resolve().parents[1]
            .joinpath("ctx_agent", "server.py")
            .read_text()
        )
        self.assertIn(
            'if failure["type"] in ("action_format", "codex_format")',
            source,
        )
        self.assertIn("remote_internal_format_recovery", source)
