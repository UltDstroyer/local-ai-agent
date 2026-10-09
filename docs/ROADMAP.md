# Roadmap

## Milestone 0: security foundation (this branch)

- [x] Record user-selected expanded access / critical approvals / authorized desktop policy.
- [x] Define permanently forbidden operations and sensitive-path defaults.
- [x] Implement deterministic, fail-closed policy inspection CLI.
- [x] Add tests for protected paths, symlinks, sensitive filenames, approval requirements and unknown operations.
- [x] Define NixOS isolation and desktop-broker blueprint.
- [ ] Validate host-specific private mounts, secrets and systemd restrictions on the actual NixOS desktop.
- [ ] Threat-review policy with the owner, especially broad home write access and personal data.

## Milestone 1: enforceable sandbox and quiet service

- [ ] Unprivileged worker UID and trusted policy controller with root-owned configuration.
- [ ] Namespace/mount restrictions, process limits and network restrictions validated by escape tests.
- [ ] SQLite task queue with pause/stop and immutable audit trail.
- [ ] Typed, expiring, single-purpose approval grants through a trusted UI.
- [ ] Systemd service installation only after explicit owner approval.

## Milestone 2: local action tools

- [ ] Safe file tools implemented against authorized file descriptors.
- [ ] Restricted task shell isolated by Linux, not only command filtering.
- [ ] Git worktrees, executable test suites and bounded job output.
- [ ] Download and upload gates with network and data-exfiltration controls.

## Milestone 3: autonomous local reasoning

- [ ] Ollama backend, bounded tool-call loops and budget controls.
- [ ] Project/task memory stores, redaction and retention controls.
- [ ] Sandboxed retry, rollback, and explicit escalation on repeated failures.

## Milestone 4: contexts and device integration

- [ ] ctx context memory and permissions mapping.
- [ ] Portal-based opt-in desktop broker with per-task access.
- [ ] Authenticated remote bridge for laptop, Pi and ChatGPT; no public unauthenticated execution endpoint.
