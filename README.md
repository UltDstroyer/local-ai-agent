# ctx-agent

Local-first AI agent for NixOS, built around user-controlled permissions, isolated workers and quiet background operation.

**Current status: Milestone 0 only.** This repository currently provides a Python policy classifier and security design documents. It **cannot** run commands, access the desktop, enforce Linux filesystem permissions, store agent memories or autonomously execute tasks yet.

## Your selected policy

- **Expanded filesystem access:** ordinary reads/writes allowed by policy outside protected paths; subject to real OS permissions.
- **Critical approval:** destructive changes, system configuration, deployment, uploads and other critical operations require trusted user authorization.
- **Authorized desktop interaction:** screen observation and input require explicit per-task consent. Background screen capture is permanently forbidden.

## Dry-run the policy

Requires Python 3.11+; no third-party Python dependencies.

    python -m unittest discover -s tests -v
    python -m ctx_agent filesystem.read /home/USER/projects/example.py
    python -m ctx_agent filesystem.write /etc/nixos/configuration.nix
    python -m ctx_agent desktop.control
    python -m ctx_agent command.execute

The CLI only reports allow / deny / approval_required. It never performs those operations. Non-allow results return exit code 3; invalid policy returns 2.

## Documents

- [Threat model and security policy](docs/SECURITY.md)
- [NixOS service and sandbox plan](docs/NIXOS.md)
- [Milestones and acceptance gates](docs/ROADMAP.md)

**Important:** Real security will come from a separate Linux identity, kernel-enforced sandbox, restricted sockets, and a trusted approval broker. Never use an allow result from this classifier as permission to execute arbitrary shell commands outside a sandbox.
