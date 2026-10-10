# SSH remote administration

## Design goal

Remote administration should feel like the local Codex workflow without turning
ctx-agent into an unrestricted SSH shell.

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
    max_actions
    max_file_writes
    max_attempts_per_milestone
    backup_required

Codex and the local administrator negotiate this contract before the owner sees
it. No mutation starts until the owner approves it.

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

There is no arbitrary command or raw shell action.

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
2. Codex proposes typed actions.
3. The local administrator reviews necessity and task alignment.
4. The contract validator checks paths, services, packages, domains, counts,
   sudo, and network capabilities.
5. The SSH broker executes approved typed actions.
6. Results are returned to ctx-agent and persisted.
7. A fresh inspection is taken.
8. The local administrator decides whether the milestone is complete.
9. Scope expansion or repeated failure pauses for owner approval.

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

The broker does not currently provide:

- arbitrary shell execution
- sshd configuration changes
- disk formatting or partitioning
- firewall reset
- user/password/key changes
- direct secret injection
- general-purpose package-repository installation
- arbitrary downloads
- unattended interactive login flows

Those omissions are intentional. A first-time Cloudflare Tunnel login, a new
third-party apt repository, or another credential-bearing workflow may require a
human step.

The user-owned SSH bridge itself has the same network reach and filesystem access
as the desktop user because it must own the user's SSH credentials. The narrow
Unix-socket request vocabulary is therefore a critical security boundary.
