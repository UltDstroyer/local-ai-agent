# Implementation milestones and status

## 0. Security contract

- [x] Untrusted AI tools separated from host service
- [x] Protected-file policy evaluator and tests
- [x] No arbitrary host shell and no credentials exposed to worker
- [ ] Host-specific isolation tests with real protected folders

## 1. Local service and task coordination

- [x] SQLite persistent job queue and event log
- [x] Unix-socket CLI, pause/resume, cancel, status, review/apply
- [x] NixOS module, dedicated service account, sandboxed project copy
- [ ] Confirm NixOS module builds and Bubblewrap works on the target desktop
- [ ] Make active cancellation interrupt running model HTTP and sandbox operations immediately
- [ ] Atomic conflict-safe patch application to concurrently edited source

## 2. Autonomous local development

- [x] Local Ollama tool-loop with bounded steps
- [x] Python source read/write and offline unittest/compile checks
- [x] Per-project user-provided notes
- [ ] Package manager and multi-language test runners with stronger sandbox profiles
- [ ] Git worktree branch/PR integration and configurable project import

## 3. Adaptive resources and media

- [x] Quiet/balanced/maximum model request profiles (quiet still uses GPU)
- [x] Opt-in companion: OBS recording control, owner-voice reference, F5-TTS and ffmpeg integration
- [ ] Load-aware GPU scheduling, VRAM telemetry and application priority detection
- [ ] Desktop consent GUI and agent-requested video/narration grants
- [ ] Verify speech synthesis and GPU compatibility on Radeon ROCm

## 4. Integrated computer agent

- [ ] Trusted desktop broker (active-window checks, portal consent and revocation)
- [ ] Expanded host filesystem access through reviewed broker, not an unsafe root shell
- [ ] ctx context lifecycle hooks
- [ ] Authenticated ChatGPT bridge and distributed laptop/Pi workers
- [ ] Full end-to-end host security and regression test suite

**Release state:** Technical preview. Do not call this fully unattended, host-wide, or plug-and-play yet.
