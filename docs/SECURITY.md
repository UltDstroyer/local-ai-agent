# Security model

ctx-agent separates owner authority, local administration, Codex execution, and
project data. This is still a technical preview, not a completed security audit.

## Trust boundaries

1. Owner
   - approves the initial mutation contract
   - approves important deviations
   - explicitly applies final results

2. System ctx-agent
   - runs as the unprivileged ctx-agent user
   - owns durable project state, approvals, checkpoints, and policy decisions
   - has no ChatGPT or Codex login credential
   - exposes only a group-restricted Unix socket

3. Local Ollama administrator
   - receives project summaries, plans, diffs, and Codex reports
   - may recommend guidance
   - cannot directly expand an approved mutation contract
   - does not receive owner credentials

4. User-owned Codex bridge
   - runs in the desktop user's session
   - owns the normal Codex/ChatGPT authentication context
   - accepts only health, consultation, and managed-workspace work requests
   - exposes a Unix socket under /run/ctx-codex
   - does not expose arbitrary shell RPC

5. Codex worker
   - is the primary engineer for autonomous project mode
   - receives a sanitized managed project/workspace
   - is instructed to stay inside the approved mutation lease
   - runs with an explicit Codex sandbox mode
   - cannot cause a workspace edit to become approved merely by creating it

6. Repository and model content
   - always untrusted
   - cannot redefine policy, owner approval, mutation budgets, or credential
     boundaries

## Managed project boundary

Import intentionally filters:

- .git
- symlinks
- common credential filenames
- secret-like key and certificate suffixes
- browser and credential database names
- .env variants
- common cache, virtual-environment, and dependency directories

Autonomous work happens only in a copy under:

    /var/lib/ctx-agent/workspaces

The original user checkout is not automatically modified.

## Mutation-contract enforcement

The initial plan is created read-only and waits for owner approval.

After every Codex worker turn, ctx-agent measures the actual filesystem state
against the approved plan. Enforcement checks include:

- changed-file count
- approximate changed-line count
- write-scope membership
- new files
- file deletions
- dependency manifests and lock files
- Nix, CI, and selected configuration paths
- whether worker command network access was approved

If the observed change exceeds authority, the plan enters awaiting_deviation.
The managed project is not updated.

The model cannot approve its own deviation.

## Checkpoints

Each completed milestone receives a private filesystem checkpoint. If an owner
rejects a deviation, the active workspace is replaced with the most recent
approved checkpoint, or with the approved project baseline when no milestone
has completed yet.

This prevents unauthorized edits from becoming the starting point for the next
attempt.

## Final apply

ready_for_review is not completion. Before apply, ctx-agent repeats mutation
contract checks and compares relevant managed-project files against the baseline
captured at initial plan approval.

Only a local owner command may copy the final workspace into the managed project.

## ChatGPT and Codex credentials

Owner authentication is initiated with:

    ctx agent codex login

or:

    ctx agent chatgpt login

The command delegates directly to the official Codex CLI in the owner's desktop
session. The system ctx-agent daemon does not receive the login credential.

The Codex bridge runs as the user specifically so normal Codex token refresh and
account management remain outside the system service.

## Important current limitation: Codex process visibility

The current bridge invokes Codex as the desktop user. Codex itself therefore
runs with the host read permissions of that user before its own tool sandbox is
applied.

The bridge disables user Codex configuration and rules for orchestrated turns,
uses read-only mode for consultation, workspace-write for approved work, and
keeps worker command network access off unless the owner-approved budget enables
it. This materially reduces risk, but it is not equivalent to placing the whole
Codex process behind a credential-separated OS sandbox.

Do not treat the current bridge as proof that adversarial repository text can
never induce Codex to inspect another user-readable host path.

A later hardening milestone should separate Codex authentication from Codex tool
execution so the worker process can be placed in a stronger mount namespace
without exposing reusable credentials inside that namespace.

## Local Ollama sandbox

The bounded local worker uses Bubblewrap around fixed file tools and predefined
test commands. It receives a sanitized snapshot, not the user's home directory.

Existing-file changes use an exact-span apply_patch operation. write_file only
creates new files.

Bubblewrap runs with unshared namespaces and without host network access.

The outer systemd service intentionally permits the minimum facilities required
for Bubblewrap to create its namespaces. In particular, AF_NETLINK is permitted
for namespace setup and ProtectKernelTunables is not enabled because it can
prevent creation of the sandbox's fresh /proc mount.

## Permanent denials and protected material

The policy layer permanently denies operations such as:

- changing policy or audit controls
- privilege escalation
- process-memory reads
- credential access
- disk formatting or partitioning
- background desktop capture

These names are defense in depth, not a substitute for Linux isolation.

## Approval policy

Automatic:

- work inside the approved write scope and mutation budget
- administrator guidance that does not expand authority
- retries below the approved milestone retry budget
- checkpoint creation

Owner approval required:

- initial autonomous plan
- new write paths
- material line or file budget expansion
- dependency changes not already approved
- Nix, CI, or selected configuration changes not already approved
- file deletion not already approved
- worker command network access not already approved
- repeated stalls or blocked milestones
- final apply

## No remote control plane

ctx-agent has no HTTP listener.

The system daemon listens on a local Unix socket. The user Codex bridge also
listens on a local Unix socket. Socket permissions and local group membership
are part of the authorization boundary.

Never reverse-proxy either socket.

## Remaining hardening work

- credential-separated Codex execution sandbox
- deterministic project-specific verification runners beyond the existing
  bounded local test helpers
- owner-reviewed export/sync back to original user Git checkouts
- stronger race-resistant file-descriptor based copy/apply operations
- dedicated audit log retention and optional signed approval records
- host-verified integration tests for Bubblewrap, user services, ROCm, and Codex
  on the target NixOS environment


## SSH remote boundary

Remote SSH work uses a separate user-owned ctx-ssh-bridge. The system
coordinator never receives private keys, passwords, SSH_AUTH_SOCK, or the user's
OpenSSH credential files.

The bridge exposes no arbitrary local-shell RPC. It normally translates a fixed
action vocabulary into quoted OpenSSH commands and re-validates every action
against the owner-approved remote mutation budget. A remote plan may explicitly
enable allow_remote_admin, which permits a remote_exec action on the named SSH
target only.

SSH connections require BatchMode, disable password and keyboard-interactive
authentication, and require a previously trusted host key.

Remote plans explicitly budget paths, services, packages, domains, sudo,
network access, remote-admin authority, action count, and file writes.
Out-of-contract actions pause as owner-visible deviations before they execute.

Remote file writes are backed up before their first mutation in a milestone.
Rejected deviations roll back current-milestone file changes. Package
installation and systemd service state are not automatically reversed because
uninstalling or stopping an approved service may itself be destructive.

Without allow_remote_admin the broker excludes arbitrary remote shell
execution. With allow_remote_admin, arbitrary commands are permitted on the
named SSH target, including root commands when allow_sudo is also approved.
This does not create a desktop command-execution path. The SSH bridge does not
enable agent forwarding. remote_exec is intentionally high trust and its
mutations are not covered by the typed file rollback mechanism.

Because ctx-ssh-bridge runs as the desktop user, compromise of that bridge would
have the network and SSH reach of that user. Its narrow local Unix-socket
protocol and group permissions are therefore critical security boundaries.
See docs/REMOTE.md.
