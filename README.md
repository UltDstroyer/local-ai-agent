# ctx-agent

Local-first administrative coding agent for NixOS. ctx-agent keeps durable project
context, negotiates mutation budgets, supervises a Codex worker, and requires
explicit owner approval before autonomous project execution or material scope
expansion.

The local Ollama model is the administrator. Codex is the primary engineer for
large project mode. Small bounded jobs can still run entirely through Ollama.

**Status: technical preview.** The security model is intentionally conservative,
but it is not a completed security audit. Use managed project copies and inspect
final diffs before applying them.

## Architecture

    owner
      |
      v
    ctx / ctx-agent
      |
      +-- local Qwen administrator
      |     - project memory
      |     - plan reconciliation
      |     - mutation-budget enforcement
      |     - Codex steering
      |
      +-- user-owned Codex bridge
      |     - ChatGPT/Codex authentication
      |     - read-only planning consultation
      |     - workspace-scoped engineering turns
      |
      +-- durable SQLite state
            - plans
            - milestones
            - checkpoints
            - deviations
            - owner approvals

Codex credentials are not copied into the system ctx-agent daemon. The bridge
runs in the desktop user's session and exposes a narrow Unix socket. The system
agent sees task results and thread IDs, not ChatGPT tokens.

## Install as a NixOS flake module

In the inputs of your system flake:

    local-ai-agent = {
      url = "github:UltDstroyer/local-ai-agent/milestone-0-security";
      inputs.nixpkgs.follows = "nixpkgs";
    };

Include:

    local-ai-agent.nixosModules.default

Then configure:

    services.ctxAgent = {
      enable = true;
      operatorUsers = [ "lena" ];
      manageOllama = true;
      gpuBackend = "rocm";
      model = "qwen3:8b";
      codexOperatorUser = "lena";
    };

The codexOperatorUser option is optional and defaults to the first operatorUsers
entry. Rebuild, pull the configured Ollama model, and log out/in once so group
membership and the user Codex bridge are refreshed.

The package includes the Nixpkgs Codex CLI.

## Sign in with ChatGPT for Codex

Authentication is owner-initiated and stays in the desktop user's normal Codex
credential store:

    ctx agent codex login

or the equivalent alias:

    ctx agent chatgpt login

Choose Sign in with ChatGPT in the Codex flow. Check it with:

    ctx agent codex status

Other owner-only helpers:

    ctx agent codex logout
    ctx agent codex doctor

The local system daemon never receives the login credential.

## Small bounded jobs

The original local-only job workflow remains available:

    ctx agent import --project demo ~/src/demo
    ctx agent submit --project demo "Fix the failing parser test"
    ctx agent status JOB_ID
    ctx agent diff JOB_ID
    ctx agent apply JOB_ID

These jobs run in disposable Bubblewrap snapshots. Existing-file edits use a
narrow exact-span patch tool; whole-file write operations only create new files.

## Autonomous project workflow

Large projects use a plan-first contract.

Start planning:

    ctx switch demo
    ctx agent plan "Implement the requested feature"

ctx-agent allows only one active plan at a time. Once a task exists, normal
commands operate on that current task automatically, so you do not need project
names, target names, plan IDs, or deviation IDs for routine work.

During planning the CLI prints throttled stage updates, elapsed time, and a rough
time-left estimate. Once Codex has drafted the plan it also prints short proposed
execution stages. Owner-facing plan output is concise by default; the complete
machine-readable contract remains stored internally.

1. Codex inspects the sanitized managed project read-only and proposes a plan.
2. The local administrator reviews and narrows the plan.
3. A machine-readable mutation budget is stored.
4. Nothing is modified yet.
5. The plan is shown to the owner and waits for approval.

Check the current task from another terminal at any time:

    ctx agent status

or:

    ctx agent progress

The progress dashboard shows a progress bar, current stage, elapsed time,
estimated work remaining, concise stage states, and any owner decision that is
blocking progress. When approval is required it prints the exact simple action:

    ctx agent approve

or:

    ctx agent reject

Estimates are deliberately rough and update using observed durations as stages
finish.

Inspect it:

    ctx agent plan show

Approve it:

    ctx agent approve

Autonomous execution intentionally runs only in maximum mode:

    ctx agent mode maximum

The administrator then works milestone by milestone with Codex. Codex does the
main implementation work; ctx-agent keeps durable context, measures the actual
filesystem diff, steers Codex when it stalls, checkpoints completed milestones,
and stops on material deviations.

Useful commands:

    ctx agent progress
    ctx agent plan show
    ctx agent plan diff
    ctx agent deviations
    ctx agent deviation show DEVIATION_ID
    ctx agent deviation approve DEVIATION_ID
    ctx agent deviation reject DEVIATION_ID
    ctx agent plan cancel

When all milestones finish inside the approved contract, the plan enters
ready_for_review. Nothing is copied into the managed project until the owner
reviews the final diff and runs:

    ctx agent plan apply

## Mutation budgets

A plan contains:

- project goal and milestones
- read scope and write scope
- maximum changed files
- maximum changed lines
- whether new files are allowed
- whether deletions are allowed
- whether dependency changes are allowed
- whether Nix, CI, and configuration changes are allowed
- whether worker command network access is allowed
- maximum retry count per milestone
- acceptance and verification criteria

Codex may propose changes, but it cannot expand its own authority.

After every worker turn ctx-agent computes the real diff. Out-of-scope files,
budget overruns, unapproved config or dependency changes, deletions, or other
material scope changes create a deviation request and pause the project.

Owner approval amends the lease. Rejection restores the most recent completed
checkpoint and tells the administrator to find an alternative inside the
original scope.

## Project context and checkpoints

The database, not a model context window, owns long-running project state.
ctx-agent persists the approved plan, current milestone, mutation budget,
Codex thread ID, administrator guidance, prior Codex summaries, retry count,
deviations and owner decisions, and final review state.

Completed milestones receive private filesystem checkpoints. If a proposed
deviation is rejected, the current workspace rolls back to the most recent
checkpoint rather than preserving unauthorized edits.

## Resource modes

    ctx agent mode quiet
    ctx agent mode balanced
    ctx agent mode maximum

quiet minimizes interference. balanced uses four CPU evaluation threads and
partial GPU offload for the local administrator. maximum removes the local CPU
thread cap, requests full GPU offload, enables Qwen thinking, and permits
approved autonomous project plans to advance.

Codex usage is separate from the local GPU profile. See docs/RESOURCES.md.

## Safety boundaries

The design deliberately separates authority:

- ctx-agent is the policy and administration layer.
- Codex is the main engineering worker.
- Codex works only on sanitized managed copies and workspaces.
- An unauthorized edit in a disposable workspace is not an approved edit.
- Actual diffs, not model claims, decide whether the mutation contract passed.
- Scope expansion requires owner approval.
- Final application requires an explicit owner command.
- Credentials, .git, secret-like files, symlinks, and common private folders
  are filtered from imported project snapshots.
- ChatGPT and Codex credentials remain with the user-owned bridge.
- No HTTP listener is exposed by ctx-agent or the bridge.

The current Codex bridge invokes Codex with user configuration and rules
disabled and with an explicit sandbox mode. Read docs/SECURITY.md before using
this with untrusted repositories.

## Context Switcher integration

Context Switcher remains the platform shell. This repository provides the
ctx-ext-agent Extension API v1 adapter.

Examples:

    ctx extensions
    ctx switch research
    ctx agent import ~/src/research-project
    ctx agent codex login
    ctx agent plan "Finish the project"
    ctx agent approve
    ctx agent mode maximum
    ctx agent status

Use --project NAME when no ctx context is active.

## Imported-project boundary

Import copies allowlisted source files into /var/lib/ctx-agent/projects.
Autonomous work happens in /var/lib/ctx-agent/workspaces. Applying a job or
project updates the managed project copy, not the original Git checkout from
which it was imported.

A separate owner-reviewed export or sync path back to an original checkout is
still a future milestone. Until then, inspect the managed diff and transfer
approved changes deliberately.

## SSH targets and remote systems

ctx-agent can also manage machines that are reachable only through SSH without
giving the system daemon your SSH private keys. Remote plans prefer a typed
action vocabulary, with an explicit high-trust remote-admin capability available
when the owner wants hands-off control of that SSH target.

The NixOS module starts a second user-owned bridge:

    ctx-ssh-bridge.service

That bridge runs as the desktop user, so OpenSSH can use the user's existing
known_hosts, SSH config, keys, and ssh-agent. The system ctx-agent communicates
with it through a local Unix socket and only sends typed remote operations.

Register a target:

    ctx agent target add pi --ssh pi@raspberrypi

Use a nonstandard SSH port if needed:

    ctx agent target add pi --ssh pi@raspberrypi --port 2222

The target record stores only host, user, port, and a local name. It does not
store an SSH password or private key.

Before planning, inspect it read-only:

    ctx agent target inspect pi

The broker collects OS, disk, mount, listener, command, and common-service
information without changing the remote machine.

After one-time target registration, create an owner-reviewed remote plan with:

    ctx agent plan "Set up Jellyfin, nginx, and Cloudflare Tunnel as a reliable media server"

If there is no active local project and exactly one SSH target is registered,
ctx-agent selects that target automatically. Codex receives the read-only
inspection and proposes the technical plan. The local Qwen administrator narrows
that plan into a remote mutation budget.

Routine control is target-free:

    ctx agent plan show
    ctx agent status
    ctx agent approve
    ctx agent reject
    ctx agent deviation

The same commands work for local project plans. ctx-agent resolves the one
current task automatically. Plan and approval commands show a short owner
summary rather than dumping the full contract. If the task pauses on a
deviation, `ctx agent deviation` shows only the current decision with a concise
summary and recommendation. Historical deviations stay in the audit database
instead of stacking up in normal terminal output.

Approved plans execute autonomously in maximum mode:

    ctx agent mode maximum

Only maximum mode leases remote milestones. Codex remains the primary engineer,
but it cannot call SSH directly. It proposes typed actions and ctx-agent checks
them against the owner-approved contract before the SSH bridge executes them.

Owner-facing plan and deviation output is intentionally concise. Remote
deviations are summarized by the local Qwen administrator instead of printing
raw command transcripts and logs.


The normal typed action vocabulary supports:

- reading approved remote files
- writing approved config files
- creating approved directories
- installing explicitly approved apt packages
- starting, restarting, and enabling explicitly approved systemd services
- deterministic checks such as nginx -t, systemctl is-active, journal tails,
  listening sockets, path/command existence, and HTTP checks

A remote plan may also explicitly set allow_remote_admin=true. That enables the
SSH-only remote_exec action for arbitrary commands on the named remote target,
including optional non-interactive sudo. This capability does not expose a local
desktop shell. Typed path/package/service allowlists do not constrain
remote_exec, so its authority is visible in the plan and must be owner-approved
before execution.

Remote config writes are backed up under:

    /var/lib/ctx-agent-remote-backups

If a later deviation is rejected, ctx-agent rolls back file and directory
changes made during the current milestone before asking Codex for an
in-contract alternative.

If Codex needs authority it does not have, the project pauses:

    ctx agent deviations --target pi
    ctx agent remote-deviation show DEVIATION_ID
    ctx agent remote-deviation approve DEVIATION_ID

or reject it:

    ctx agent remote-deviation reject DEVIATION_ID

Examples of changes that become owner-visible deviations include an unexpected
/etc path, a new package, a new systemd service, extra public network access, or
exceeding the approved action/file-write budget.

The SSH bridge uses BatchMode, disables password and keyboard-interactive
prompts, and requires an already trusted host key. Connect to a machine manually
with ssh once before registering it if its host key is not already present.

Without allow_remote_admin, the remote worker retains the narrow typed-action
boundary. With allow_remote_admin, Codex can perform broader administration on
the SSH target, including cleanup, package removal, repository management, and
other shell-level maintenance. Credential-bearing interactive workflows and
physical operations can still require the owner. remote_exec changes are not
automatically reversible by the typed file-backup mechanism.

## Documentation

- docs/ORCHESTRATION.md - plan, approval, and worker lifecycle
- docs/CODEX.md - ChatGPT sign-in and bridge behavior
- docs/SECURITY.md - trust boundaries and known limitations
- docs/NIXOS.md - NixOS service details
- docs/RESOURCES.md - local resource profiles
- docs/ROADMAP.md - remaining milestones
- docs/MEDIA.md - owner-initiated media companion
