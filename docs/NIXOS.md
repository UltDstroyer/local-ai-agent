# NixOS deployment

ctx-agent 0.2 is split into a system coordinator and a user-owned Codex bridge.

## System coordinator

The NixOS module creates:

- the ctx-agent system user
- the ctx-agent-users operator group
- /var/lib/ctx-agent/projects
- /var/lib/ctx-agent/jobs
- /var/lib/ctx-agent/workspaces
- /var/lib/ctx-agent/checkpoints
- /run/ctx-agent/agent.sock

The system service stores project state, approvals, mutation budgets and
checkpoints. It talks to Ollama only over loopback and to the Codex bridge only
over a local Unix socket.

Example:

    services.ctxAgent = {
      enable = true;
      operatorUsers = [ "lena" ];
      codexOperatorUser = "lena";
      manageOllama = true;
      gpuBackend = "rocm";
      model = "qwen3:8b";
    };

codexOperatorUser is optional. If omitted, the first operatorUsers entry is used.

## User Codex bridge

The module also installs a user service:

    ctx-codex-bridge.service

For an authorized operator it creates:

    /run/ctx-codex/USERNAME.sock

The bridge runs as the desktop user because the official Codex CLI owns that
user's ChatGPT authentication state. The system coordinator receives no login
token.

Users outside ctx-agent-users exit the bridge cleanly without creating a socket.

After the first install or group-membership change, log out of the graphical
session and back in so the user service inherits ctx-agent-users.

## ChatGPT login

Run from the normal user shell:

    ctx agent codex login

or:

    ctx agent chatgpt login

Then verify:

    ctx agent codex status

Authentication is deliberately not configured in Nix and must not be placed in
configuration.nix, flake.nix, or the Nix store.

## System service hardening

The system coordinator currently uses:

- User=ctx-agent
- Group=ctx-agent-users
- NoNewPrivileges=true
- ProtectSystem=strict
- ProtectHome=true
- PrivateTmp=true
- PrivateDevices=true
- ProtectKernelModules=true
- ProtectControlGroups=true
- ProtectClock=true
- ProtectHostname=true
- RestrictSUIDSGID=true
- LockPersonality=true
- empty capability and ambient-capability sets
- localhost-only IP policy
- AF_UNIX, AF_INET, AF_INET6 and AF_NETLINK address families
- low CPUWeight and IOWeight
- TasksMax and MemoryMax limits

ProtectKernelTunables is intentionally not enabled on the coordinator. On the
target host it prevented Bubblewrap from mounting the fresh /proc required by
the inner PID namespace.

AF_NETLINK is intentionally allowed because Bubblewrap needs it to configure
the isolated loopback interface. Bubblewrap still creates an unshared network
namespace for the local worker.

These exceptions are there to let the stronger inner sandbox function, not to
give the LLM host privileges.

## Resource modes

quiet and balanced are intended for interactive desktop use.

maximum is the explicit autonomous-project mode. Only maximum leases new Codex
project milestones. Changing back to balanced or quiet prevents the orchestrator
from starting another milestone after the current worker turn returns.

## Runtime directories

Managed source:

    /var/lib/ctx-agent/projects

Autonomous scratch work:

    /var/lib/ctx-agent/workspaces

Approved milestone checkpoints:

    /var/lib/ctx-agent/checkpoints

Persistent SQLite state:

    /var/lib/ctx-agent/state.sqlite3

Neither the project import nor the autonomous workspace carries the original
checkout's .git directory or filtered secret-like files.

## Validation after rebuild

Check:

    systemctl status ctx-agent --no-pager
    systemctl --user status ctx-codex-bridge --no-pager
    ctx agent health
    ctx agent codex status

Then use a disposable project to verify Bubblewrap and plan creation before
running the orchestrator on valuable code.

Run systemd-analyze security ctx-agent.service when hardening changes are made.


## SSH bridge user service

The module also installs:

    ctx-ssh-bridge.service

Authorized operator sessions create:

    /run/ctx-ssh/USERNAME.sock

The service runs as the desktop user so OpenSSH can use that user's normal
known_hosts, SSH config, keys, and ssh-agent. The system ctx-agent service sees
only the local broker socket.

The package includes the OpenSSH client. Remote hosts must already have a trusted
host key because autonomous connections use StrictHostKeyChecking=yes.

Check it after rebuild with:

    systemctl --user status ctx-ssh-bridge --no-pager
    ctx agent target list

See docs/REMOTE.md for the remote contract and supported typed actions.
