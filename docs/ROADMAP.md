# Implementation milestones and status

## 0. Security contract

- [x] Untrusted model output separated from owner authority
- [x] Protected-file policy evaluator and tests
- [x] No arbitrary shell endpoint in ctx-agent
- [x] ChatGPT/Codex credentials kept out of the system coordinator
- [x] Actual-diff mutation-budget enforcement
- [ ] Credential-separated OS sandbox around the entire Codex worker process
- [ ] Host-specific negative tests against protected folders and sockets

## 1. Local service and task coordination

- [x] SQLite persistent job queue and audit events
- [x] Unix-socket CLI, pause/resume, cancel, review/apply
- [x] NixOS module and dedicated system account
- [x] Disposable Bubblewrap project snapshots
- [x] Exact-span patch tool for narrow local-model edits
- [ ] Finish host verification of the /proc Bubblewrap fix
- [ ] Make active cancellation interrupt model HTTP and Codex turns immediately
- [ ] File-descriptor-based race-resistant final copy/apply

## 2. Codex project orchestration

- [x] User-owned Codex bridge
- [x] Sign in with ChatGPT command through the official Codex CLI
- [x] Read-only Codex planning consultation
- [x] Local administrator reconciliation
- [x] Final Codex agreement required before presenting the plan
- [x] Owner approval before execution
- [x] Persistent plan, milestone, context, and Codex thread state
- [x] File and line mutation budgets
- [x] Dependency, configuration, deletion, and network capability flags
- [x] Maximum-mode-only autonomous milestone execution
- [x] Local administrator steering and retry limits
- [x] Owner-approved deviation flow
- [x] Milestone checkpoints and rollback after rejected deviations
- [x] Final owner review and explicit apply
- [ ] Deterministic project-specific build/test command contracts
- [ ] Git worktree and commit checkpoints where original Git metadata is safe
- [ ] Owner-reviewed export/sync back to the original checkout

## 3. Adaptive resources and media

- [x] Quiet, balanced, and maximum local-model profiles
- [x] Balanced profile tuned for desktop responsiveness
- [x] Opt-in OBS, owner-voice, TTS and ffmpeg companion
- [ ] Load-aware GPU scheduling and VRAM telemetry
- [ ] Desktop consent GUI for media and future interactive actions
- [ ] Verify speech synthesis compatibility on the target Radeon ROCm host

## 4. Integrated computer agent

- [ ] Trusted desktop broker with active-window and portal consent
- [ ] Expanded host filesystem access through a reviewed broker rather than an
      unrestricted user shell
- [ ] Distributed laptop and Raspberry Pi workers
- [ ] Full host security and regression suite
- [ ] Optional richer Codex App Server transport after the CLI bridge is
      host-validated

Release state: technical preview. Autonomous project work is deliberately
limited to sanitized managed copies and explicit owner-approved mutation leases.


## 2.5. SSH remote systems

- [x] User-owned SSH bridge keeps private keys out of the system daemon
- [x] Named SSH targets with host/user/port only
- [x] Strict host-key and non-interactive SSH policy
- [x] Fixed read-only remote inspection
- [x] Codex-led remote planning with local administrator reconciliation
- [x] Explicit owner approval before remote mutation
- [x] Typed remote action vocabulary with no arbitrary shell RPC
- [x] Remote path/service/package/domain/sudo/network budgets
- [x] Maximum-mode-only autonomous remote execution
- [x] Config-file backup metadata and current-milestone rollback
- [x] Remote deviations and owner approval/rejection
- [x] Final read-only verification before completion
- [ ] Typed third-party repository installation with domain/key pinning
- [ ] Owner-mediated secret handoff for credential-bearing setup flows
- [ ] Stronger service/package rollback snapshots where reversibility is safe
- [ ] Host integration test against a disposable Raspberry Pi or VM
