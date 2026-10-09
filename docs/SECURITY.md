# Milestone 0: Threat model and security contract

**Status:** Design and policy prototype only. Do not grant this Python process broad real-machine permissions. There is no secure command runner yet.

## Selected operating posture

- Expanded ordinary filesystem access: permitted by policy for reading and writing non-protected files, but actual Linux access remains limited by the operating-system identity and eventual sandbox mounts.
- Critical approval: delete/move/chmod, OS configuration, service deployment, global installs, and upload operations require an approval delivered by a separate trusted local controller. The model must not be able to mint, forge or replay grants.
- Authorized desktop interaction: graphical capture, input, and launch requests require per-task user authorization from a separate desktop broker. No continuous screen scraping or silent capture.
- The agent may be quiet in the background, but quiet mode never grants extra rights.

## Trust boundaries

1. **Untrusted:** LLM text, tool responses, repository content, terminal output, websites, downloaded files and stored task memory.
2. **Trusted control plane:** owner-managed policy, approval records, audit sink, executor launcher, and desktop broker. Keep these inaccessible to the agent's UID and never in an agent-writable Git checkout.
3. **Worker boundary:** separate unprivileged UID, explicit filesystem mounts/permissions, bounded processes, resource limits, no inherited login session cookies or SSH/GitHub credentials.
4. **Human approval:** a trusted UI shows the exact action, resource, scope and expiry; no implicit approval from natural-language instructions or an AI-generated boolean.

## Built-in permanent denials

Changing policy or audit settings, obtaining new privileges, raw process-memory reads, direct credential access, disk formatting/partitioning, and background desktop capture. These must also be enforced through Linux isolation, not just operation names.

## Sensitive locations

Example policy protects SSH/GPG keys, browser profiles, password managers, agent policy/state, API virtual filesystems and likely credential filenames. Expand it for personal vaults, mounted drives, backups, BitLocker recovery keys and any device-specific secrets **before** installation.

System paths such as /etc and /opt are readable where Linux permissions permit, but writes require a separate approval flow. /boot, /root and kernel/process interfaces are not available to the agent.

## Known limits of this prototype

- The Python evaluator is only a classifier. A malicious or mistaken executable can ignore it; an LLM can mislabel a request. The future launcher must own the permission decision and map high-level operations to fixed, typed executors.
- Symlink checks here are **not race-safe**. Kernel-enforced mount namespaces, dedicated UIDs, safe openat2-style resolution, and file-descriptor-based operations will be necessary. Do not perform open/write/delete based on a prior check.
- Command blacklists cannot stop equivalent operations through Python, shell expansion, scripts, IPC, network services, or other applications. Arbitrary command execution stays disabled through Milestone 0.
- Filesystem namespace restrictions do not automatically isolate Unix sockets or prevent later host mounts from propagating. Avoid passing D-Bus, SSH agent, Docker, Podman, Wayland, X11, cloud, or browser-control sockets to background workers.
- Expanded write access and non-root isolation are in tension: a separate UID cannot automatically write to everything owned by the normal user. Use approved directories/bind mounts or a narrow mediated broker; do not solve this by giving broad sudo access.
- The prototype includes no cryptographic approvals, no trusted UI, no real sandbox, no automatic memory redaction, no filesystem watcher, and no desktop interactions. Those are later milestones.

## Acceptance tests for enabling execution

- An untrusted worker cannot read protected files, including through symlinks, bind mounts, alternate paths and open descriptors.
- An untrusted worker cannot edit the deployed policy, broker, audit log, service definitions or approval database.
- A worker cannot escape its mount namespace, acquire elevated privileges, access the user's desktop/session sockets, or connect to credential agents.
- Critical actions fail without a scoped, expiring approval from the trusted controller.
- GUI automation fails closed unless the owner has explicitly authorized that task and application; permission revocation stops further actions.
- Kill/pause terminates all descendants and prevents further operations, without affecting interactive user applications.
- No AI-authored text, scripts or repository instructions are allowed to redefine trust boundaries.

See the official systemd sandbox documentation: https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html
