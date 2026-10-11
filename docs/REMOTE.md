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

When the owner rejects a deviation, ctx-agent asks the broker to reverse current
milestone file and newly-created-directory mutations, then tells Codex to find a
solution inside the original authority.

Package and service actions are not automatically uninstalled or disabled on
rollback because doing so can be more destructive than leaving an approved
installation in place. These actions therefore must already be visible in the
owner-approved contract before execution.

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
8. Scope expansion, repeated malformed proposals, or repeated execution failure
   pauses for owner approval.

This makes Qwen a governor rather than a foreman: it shapes the plan, enforces
the owner-approved boundary, preserves durable context, and reviews milestone
outcomes while Codex owns technical execution inside that boundary.

## Deviations

List:

    ctx agent deviations --target pi

Inspect:

    ctx agent remote-deviation show ID

Approve:

    ctx agent remote-deviation approve ID

Reject:

    ctx agent remote-deviation reject ID

A scope deviation approval makes the smallest inferred contract expansion needed
for the exact proposed action. A rejection rolls back current-milestone file
mutations and asks Codex for a smaller alternative.

## Final verification

After the final milestone, Codex receives one more read-only inspection and must
return a structured ready/not-ready verdict.

The remote plan is marked completed only when that final review reports that the
goal and verification criteria are satisfied. Otherwise the plan pauses as a
final-verification deviation.

## Deliberate limitations

Without allow_remote_admin, the broker remains limited to the typed action
vocabulary above.

With allow_remote_admin, the broker can run arbitrary commands on the named SSH
target and can therefore perform package removal, repository setup, service
migration, cleanup, and other system administration. This is intentionally a
high-trust capability. It does not grant arbitrary local command execution on
the desktop, and SSH agent forwarding is not enabled by the bridge.

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

Qwen then acts as the first approval tier:

- low-risk and ordinary medium-risk typed expansions can be approved
  automatically when Qwen determines they are minimal, on-task, and supported
  by the latest inspection;
- every manager approval is persisted as an audit event and becomes part of the
  plan contract;
- high-trust requests, including arbitrary remote_exec authority and sensitive
  SSH, boot, disk, credential, or network-lockout paths, cannot be auto-approved
  by Qwen and are escalated to the owner;
- ambiguous requests are escalated rather than guessed.

The authority ladder is therefore:

    owner-approved plan
        -> Codex engineer
        -> deterministic contract validator
        -> Qwen manager for narrow scope requests
        -> owner only when Qwen cannot safely approve

Qwen does not review every in-contract action. Its normal review is
milestone-level and lightweight. Review depth increases after retries or
failures.

## Automatic recovery and self-repair

Running remote plans are durable across coordinator restarts. If ctx-agent is
restarted while a milestone is leased, the lease is returned to the approved
queue, an audit event records the recovery, and Codex is instructed to take a
fresh inspection before continuing. The system never assumes an interrupted
remote command completed.

Failed typed actions feed their exact output back into the same Codex thread.
Codex gets bounded self-repair attempts inside the existing contract before the
manager or owner is asked for more authority.

## Error categories

Remote autonomous failures are classified before a deviation is created.
Current categories include:

- policy_scope
- codex_format
- codex_worker
- ssh_transport
- remote_command
- timeout
- credentials

The suggested recovery action is specific to the category. A validation error
must not be presented as an SSH connectivity failure.

## Live deviations

After an owner approval command, the CLI remains attached while autonomous work
is active. Progress is streamed to stderr. If the plan reaches a pending
deviation, the deviation ID, concrete reason, manager decision, error detail,
and recommended approve/reject command are printed immediately in that terminal.

The plan continues to exist independently of the attached terminal; closing the
terminal does not cancel autonomous execution.

## Dynamic read-only discovery

The fixed inspection now includes bounded lists of discovered systemd service
units and currently running services. This gives Codex and Qwen current
machine-specific context instead of relying only on a static list of common
services. Newly discovered resources still pass through deterministic scope
validation before mutation.
