# NixOS implementation blueprint (not yet deployable)

## Service separation

1. **Policy controller:** system-managed unit using an owner-controlled policy installed under /etc/ctx-agent (root-owned). It handles approvals and immutable audit records. The AI worker cannot write its binaries, config, database or IPC control sockets.
2. **Worker:** dedicated non-login Linux account, no sudo, no supplementary docker/podman/input/video groups. Prefer systemd system service or transient scoped workers with strict sandboxing. Do not run as the interactive user.
3. **Desktop broker:** separately authorized foreground component running within the graphical user session. Use xdg-desktop-portal and Wayland consent where supported. Grant narrowly scoped actions, with revocation. Never pass general session D-Bus/Wayland/X11 access to the background worker.

## Target systemd protections (to validate on this NixOS host)

- ProtectSystem=strict, ProtectHome=tmpfs plus explicit read-only/read-write bind mounts for permitted files.
- NoNewPrivileges=true, PrivateTmp=true, PrivateDevices=true, ProtectKernelTunables=true, ProtectControlGroups=true, ProtectKernelModules=true.
- CapabilityBoundingSet= (empty), RestrictSUIDSGID=true, LockPersonality=true, RestrictNamespaces=true or the narrowest functioning namespace controls.
- ProcSubset=pid, ProtectProc=invisible, SystemCallArchitectures=native, a tested SystemCallFilter profile, and limits for TasksMax/MemoryMax/CPUQuota/IOWeight.
- RestrictAddressFamilies to necessary transports; route outbound traffic via an egress policy or disabled network namespace by default.
- Restrict access to /run sockets and device nodes. All filesystem and mount allowlists must be verified inside the service's actual namespace.

## Resource and quiet modes

Implement a persistent coordinator with short-lived workers, each in a systemd cgroup. Set CPUWeight/IOWeight to low values and explicitly reserve GPU time for interactive processes. Pause heavy work on user-specified contexts (games, video calls, long GPU experiments). Limit maximum parallel workers.

## Deployment gates

Do **not** install or enable a systemd service from this branch. Milestone 1 must first introduce a tested kernel-enforced sandbox and a stop mechanism. Installation must be approved from an independent trusted shell or UI. Run systemd-analyze security on deployed units and verify behavior with live negative tests.

Relevant docs: https://wiki.nixos.org/wiki/Systemd_Hardening
