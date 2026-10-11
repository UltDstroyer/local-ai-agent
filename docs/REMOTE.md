# SSH remote administration

## Design goal

Remote administration should feel like the local Codex workflow while keeping
desktop authority separate from remote-target authority. Typed actions are the
default, and an owner-approved high-trust mode can intentionally grant arbitrary
administration on the named SSH target without granting a local desktop shell.

The system coordinator owns policy and approval state. The desktop user owns SSH
credentials. Codex remains the primary engineer, but it proposes typed actions
instead of receiving direct SSH access.

## Components

System service:

    ctx-agent.service

User service:

    ctx-ssh-bridge.service

Socket:

    /run/ctx-ssh/USERNAME.sock

The bridge uses the user's normal OpenSSH configuration, known_hosts, keys, and
ssh-agent. Those credentials are never copied into /var/lib/ctx-agent or passed
to the system ctx-agent process.

## Target records

A target stores only:

    name
    host
    user
    port

Example:

    ctx agent target add pi --ssh pi@raspberrypi

The SSH bridge uses:

    BatchMode=yes
    PasswordAuthentication=no
    KbdInteractiveAuthentication=no
    StrictHostKeyChecking=yes

This prevents an autonomous task from getting stuck at an interactive password
or host-key prompt.

## Read-only inspection

Before a remote plan is negotiated, ctx-agent performs a fixed read-only
inspection that collects:

- identity and kernel
- OS release
- uptime
- filesystem utilization
- block devices and mount points
- listening sockets
- common media/proxy service status
- presence of common commands
- presence of selected configuration files

The inspection is written to a managed local context file so Codex can reason
about the machine without being given SSH itself.

## Remote mutation contract

A remote plan includes:

    allowed_read_paths
    allowed_write_paths
    allowed_services
    allowed_packages
    allowed_domains
    allow_sudo
    allow_package_install
    allow_network
    allow_remote_admin
    max_actions
    max_file_writes
    max_attempts_per_milestone
    backup_required

Codex and the local manager negotiate this contract before the owner sees it.
No mutation starts until the owner approves it. After approval, the contract and
deterministic validator are the execution authority: the manager does not
approve each individual in-contract action batch.

## Typed actions

The bridge supports only a fixed vocabulary.

read_file:
    Reads one approved absolute path.

write_file:
    Writes one approved absolute path. Requires sudo approval. Existing files
    receive a backup before the first write in the milestone.

ensure_dir:
    Creates an approved directory if it does not already exist.

package_install:
    Uses apt-get for explicitly allowlisted packages. Requires sudo, package
    installation, and network approval.

service:
    Supports status, start, restart, enable, and enable_now for explicitly
    approved services.

check:
    Supports systemctl_is_active, http_get, http_head, nginx_test,
    journal_tail, listen_sockets, command_exists, and path_exists.

remote_exec:
    Available only when allow_remote_admin=true in the approved remote plan.
    Runs an arbitrary command on the named SSH target, optionally as root via
    non-interactive sudo. Typed path/package/service allowlists do not constrain
    this action. The command is transported as an SSH argument and is never
    executed by a local desktop shell.

## Backups and rollback

Before replacing a remote file, the bridge saves its original contents and
metadata beneath:

    /var/lib/ctx-agent-remote-backups/PLAN_ID/mNNN/

If the path did not previously exist, a marker records that fact.

Remote proposed actions are validated before execution. Rejecting a deviation
therefore does not roll back earlier successful in-contract work from the same
milestone; the unapproved proposed action was never executed. Qwen tells Codex
to preserve completed work and find an alternative inside existing authority.

Typed file backups remain available for deterministic recovery from failed
mutations. Package and service actions are not automatically uninstalled or
disabled because doing so can be more destructive than leaving an approved
change in place.

## Planning and execution

Planning:

    ctx agent plan --target pi "Set up the media server"

Inspection and planning are read-only.

Review:

    ctx agent plan --target pi show

Approval:

    ctx agent approve --target pi

Execution:

    ctx agent mode maximum

For each milestone:

1. ctx-agent gets a fresh read-only inspection.
2. Codex proposes typed actions, or remote_exec when the approved plan grants
   remote-admin authority.
3. The deterministic contract validator checks scopes and capabilities.
4. The SSH broker executes valid in-contract actions without a second model
   approval step.
5. Results are returned to ctx-agent and persisted.
6. A fresh inspection is taken.
7. The local manager reviews the milestone outcome, not every individual action,
   and decides whether the milestone is complete, should continue, or is blocked.
8. Codex and Qwen check in on the result. Technical failures, malformed
   proposals, retries, bookkeeping limits, and inspectable system state are
   repaired internally with bounded recovery.
9. Only a concrete new authority, required package/service, critical remote
   action, credential/human-only input, or real owner preference is escalated.

This makes Qwen a manager rather than a foreman: it preserves context and
policy, challenges and refines Codex's approach, and filters boss escalations.
Codex remains the senior technical engineer and can propose a stronger strategy
when Qwen's approach is weaker.

## Deviations

List:

    ctx agent deviations --target pi

Inspect:

    ctx agent remote-deviation show ID

Approve:

    ctx agent remote-deviation approve ID

Reject:

    ctx agent remote-deviation reject ID

A scope deviation approval makes only the exact owner-visible contract
expansion needed for the proposed action. A rejection leaves earlier successful
in-contract work intact and asks Codex and Qwen for an alternative.

## Final verification

After the final milestone, Codex receives one more read-only inspection and must
return a structured ready/not-ready verdict.

The remote plan is marked completed only when both Codex and Qwen sign off on
the final verification. Technical final-review problems return to bounded
internal rework. They do not become a boss deviation unless that rework exposes
a concrete new authority or human-only need.

## Deliberate limitations

Without allow_remote_admin, the broker remains limited to the typed action
vocabulary above.

With allow_remote_admin, the broker can run broad arbitrary maintenance on the
named SSH target. Package/service mutations must still use typed actions so they
cannot bypass per-dependency/service owner approval. Critical operations that
can destroy data, sever access, change authentication/privilege/network boot
state, or power/reboot the target require a fresh exact-action owner approval
even under remote-admin authority. These critical approvals are single-use.
Remote-admin authority does not grant arbitrary local desktop execution, and
SSH agent forwarding is not enabled by the bridge.

Credential-bearing interactive login flows, secrets the agent does not possess,
physical media changes, and operations that destroy SSH connectivity can still
require a human step. remote_exec changes are not automatically reversible by
the typed file-backup mechanism.

The user-owned SSH bridge itself has the same network reach and filesystem access
as the desktop user because it must own the user's SSH credentials. The narrow
Unix-socket request vocabulary is therefore a critical security boundary.


## Manager escalation and autonomy

Codex may request authority beyond the current remote contract by proposing the
minimum typed action it needs. The deterministic validator still rejects that
action first; the rejection becomes a scope request rather than an execution.

Qwen is the manager and escalation filter, not a substitute owner:

- bookkeeping growth, read-only diagnosis, inspectable machine state, formatting
  repair, retries, and technical replanning stay internal;
- Qwen may approve only genuinely non-authority operational adjustments;
- new packages, first service mutations, write/sudo/network authority,
  credentials/human-only input, and critical remote-admin actions require the
  owner;
- Codex and Qwen check each other's technical reasoning before non-deterministic
  escalations;
- model failure, malformed model output, or disagreement is never itself a boss
  decision;
- every boss escalation contains one concrete request.

The authority ladder is therefore:

    owner-approved plan
        -> Codex senior technical engineer
        <-> Qwen manager / policy / memory / escalation filter
        -> deterministic contract and SSH tooling
        -> owner only for a concrete authority or human decision

Qwen reviews milestone outcomes and meaningful decision points rather than every
in-contract action. Codex can challenge Qwen with a better technical strategy.

## Automatic recovery and self-repair

Running remote plans are durable across coordinator restarts. If ctx-agent is
restarted while a milestone is leased, the lease is returned to the approved
queue, an audit event records the recovery, and Codex is instructed to take a
fresh inspection before continuing. The system never assumes an interrupted
remote command completed.

Failed typed actions feed their exact output back into the Codex/Qwen recovery
loop. Internal recovery is bounded and transient counters reset after successful
milestones. If the agents cannot converge without new authority, the plan stops
cleanly rather than asking the owner merely for more retries.

## Error categories

Remote autonomous failures are classified before escalation. Most categories
remain internal and do not create deviations. Current categories include:

- policy_scope
- codex_format
- codex_worker
- ssh_transport
- remote_command
- timeout
- credentials

The suggested recovery action is specific to the category. Formatting,
policy-routing, transport, timeout, worker, and ordinary remote-command failures
are repaired internally with bounded recovery. A credential/access failure may
produce a concrete human-access request. A validation error must not be
presented as an SSH connectivity failure.

## Live deviations

After an owner approval command, the CLI remains attached while autonomous work
is active. Progress is streamed to stderr. If the plan reaches a pending
deviation, Qwen prints a compact boss brief with the summary, one concrete
request, risk, recommendation, decision, and approve/reject shorthand. Raw
employee/tool details remain diagnostic-only.

The plan continues to exist independently of the attached terminal; closing the
terminal does not cancel autonomous execution.

## Dynamic read-only discovery

The fixed inspection now includes bounded lists of discovered systemd service
units and currently running services. This gives Codex and Qwen current
machine-specific context instead of relying only on a static list of common
services. Newly discovered resources still pass through deterministic scope
validation before mutation.


## Boss-facing communication boundary

The human owner is treated as the boss, Qwen as the manager, and Codex as the
employee. Codex and low-level tools never speak directly to the boss in normal
operation.

When a deviation reaches the owner, Qwen receives the full internal deviation
payload and rewrites it into a short executive brief containing only:

- what is blocked;
- the practical risk;
- Qwen's recommendation;
- the decision Qwen needs from the boss.

Raw Codex output, validator details, stack traces, SSH/tool diagnostics, and
internal remediation instructions remain in the audit store for Qwen and
debugging. They are not printed in the ordinary boss-facing terminal flow.

If Qwen cannot produce a reliable summary, the fallback uses only the current
deviation's safe structured fields and never stringifies raw employee/tool
objects into the boss request. Explicit events/logs commands remain the
diagnostic escape hatch.
