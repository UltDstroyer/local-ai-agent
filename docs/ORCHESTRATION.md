# Autonomous orchestration

ctx-agent treats long-running software work as an owner-approved contract, not
an open-ended prompt.

## Roles

The local Ollama model is the administrator. It owns project memory, mutation
scope, milestone state, retry policy, and escalation decisions.

Codex is the primary engineering worker. It inspects the managed project,
implements one milestone at a time, runs project-local checks, and reports its
results back to the administrator.

The owner approves the initial plan, any important scope expansion, and the
final apply.

## Lifecycle

1. The owner gives ctx-agent a project goal.
2. Codex performs a read-only planning consultation.
3. The local administrator reconciles the proposal and narrows the mutation
   lease.
4. The plan is persisted with status awaiting_approval.
5. The owner reviews and approves or rejects it.
6. In maximum mode, the orchestrator leases one milestone to Codex.
7. After each turn, ctx-agent measures the real filesystem diff.
8. If the diff is inside budget, the local administrator reviews progress and
   either completes the milestone or sends guidance back to Codex.
9. Completed milestones receive a private checkpoint.
10. Material deviations pause the plan until the owner decides.
11. After all milestones, Codex performs a read-only final review and ctx-agent
    performs a final mutation-contract check.
12. The plan becomes ready_for_review.
13. Only an explicit owner apply updates the managed project copy.

## Mutation lease

The lease contains:

    goal
    milestones
    read_scope
    write_scope
    verification criteria
    max_files_changed
    max_lines_changed
    allow_new_files
    allow_file_deletion
    allow_dependency_changes
    allow_config_changes
    allow_network
    max_attempts_per_milestone

The worker is instructed to stay inside the lease, but enforcement never relies
on that instruction. ctx-agent computes the actual change report after every
worker turn.

A worker may temporarily create an out-of-scope edit inside its disposable
workspace. That edit does not gain authority merely because it exists. The
project immediately enters awaiting_deviation and no managed project files are
updated.

## Deviations

Typical deviations include:

- writing outside the approved scope
- exceeding file or line budgets
- adding or changing dependencies without permission
- modifying Nix, CI, or deployment configuration without permission
- deleting files without permission
- requesting command network access
- repeatedly failing the same milestone
- a tool, authentication, or bridge failure that prevents autonomous progress

An approved mutation deviation amends the stored lease. A rejected deviation
restores the latest completed checkpoint and instructs the administrator to find
an alternative inside the original authority.

A stalled milestone does not automatically receive new filesystem authority.
Owner approval only permits the orchestrator to retry or replan within the
existing lease unless a concrete mutation amendment is also present.

## Context ownership

Long projects do not depend on a single model context window. SQLite stores:

- plan and approval status
- milestone index
- Codex thread ID
- retry count
- administrator guidance
- compact Codex and administrator history
- deviation requests and owner decisions
- final result

The next Codex turn receives the relevant compact context and current lease.

## Minimal-change rule

The plan is intentionally biased toward the smallest practical solution.
Codex is told not to refactor unrelated code. The administrator measures the
real diff and checks:

- number of changed files
- approximate added and removed lines
- new files
- deleted files
- dependency files
- configuration files
- write-scope membership

Final readiness requires the measured diff to remain inside the approved
contract.

## Checkpoints and rollback

Workspaces live under:

    /var/lib/ctx-agent/workspaces

Completed milestone checkpoints live under:

    /var/lib/ctx-agent/checkpoints

Checkpoints contain only the sanitized managed project data. They intentionally
do not contain the original repository's .git directory or secret-like files.

## Maximum mode

Planning and approval are available in every resource mode. Autonomous project
advancement only occurs when:

    ctx agent mode maximum

This gives the owner a simple global switch. Changing to quiet or balanced
stops the orchestrator from leasing another milestone after the current turn
returns.

## Final apply

ready_for_review is not completion. The owner should inspect:

    ctx agent plan show
    ctx agent plan diff

Then explicitly apply:

    ctx agent plan apply

The apply operation checks the approved baseline for conflicting managed-project
changes before copying the workspace into the managed project.
