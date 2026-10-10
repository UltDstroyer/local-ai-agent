{ config, lib, pkgs, ... }:
let
  inherit (lib) mkEnableOption mkOption types mkIf mkMerge;
  cfg = config.services.ctxAgent;
  package = pkgs.callPackage ./package.nix { };
  codexUser =
    if cfg.codexOperatorUser != null then cfg.codexOperatorUser
    else if cfg.operatorUsers != [] then builtins.head cfg.operatorUsers
    else "";
  runtimeConfig = pkgs.writeText "ctx-agent-runtime.json" (builtins.toJSON {
    state_dir = "/var/lib/ctx-agent";
    socket_path = "/run/ctx-agent/agent.sock";
    projects_dir = "/var/lib/ctx-agent/projects";
    workspaces_dir = "/var/lib/ctx-agent/workspaces";
    checkpoints_dir = "/var/lib/ctx-agent/checkpoints";
    codex_bridge_socket =
      if codexUser == "" then null else "/run/ctx-codex/${codexUser}.sock";
    ollama_url = "http://127.0.0.1:11434/api/chat";
    model = cfg.model;
    max_steps = cfg.maxSteps;
    worker_timeout = cfg.workerTimeout;
    bubblewrap = "${pkgs.bubblewrap}/bin/bwrap";
    python = "${pkgs.python3}/bin/python3";
    socket_group = "ctx-agent-users";
  });
in {
  options.services.ctxAgent = {
    enable = mkEnableOption "local ctx-agent background task service";
    operatorUsers = mkOption {
      type = types.listOf types.str;
      default = [];
      example = [ "lena" ];
      description = "Existing accounts allowed to use the local ctx-agent socket.";
    };
    codexOperatorUser = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = "Desktop user whose Codex/ChatGPT login powers autonomous project work. Defaults to the first operatorUsers entry.";
    };
    model = mkOption {
      type = types.str;
      default = "qwen3:8b";
      description = "Already downloaded Ollama model.";
    };
    manageOllama = mkOption {
      type = types.bool;
      default = false;
      description = "Enable NixOS Ollama without downloading a model.";
    };
    gpuBackend = mkOption {
      type = types.enum [ "rocm" "vulkan" "cpu" ];
      default = "rocm";
      description = "Acceleration backend when ctx-agent manages Ollama. ROCm is the AMD default.";
    };
    maxSteps = mkOption {
      type = types.ints.between 1 32;
      default = 12;
    };
    workerTimeout = mkOption {
      type = types.ints.between 5 180;
      default = 90;
    };
  };
  config = mkIf cfg.enable (mkMerge [
    {
      users.groups.ctx-agent-users = {};
      users.users = (lib.genAttrs cfg.operatorUsers (_: {
        extraGroups = [ "ctx-agent-users" ];
      })) // {
        ctx-agent = {
          isSystemUser = true;
          group = "ctx-agent-users";
          home = "/var/lib/ctx-agent";
          description = "Unprivileged isolated local AI coordinator";
        };
      };
      environment.systemPackages = [ package ];
      systemd.tmpfiles.rules = [
        "d /var/lib/ctx-agent/projects 2770 ctx-agent ctx-agent-users - -"
        "d /var/lib/ctx-agent/jobs 0700 ctx-agent ctx-agent-users - -"
        "d /var/lib/ctx-agent/workspaces 2770 ctx-agent ctx-agent-users - -"
        "d /var/lib/ctx-agent/checkpoints 0700 ctx-agent ctx-agent-users - -"
        "d /run/ctx-codex 0770 root ctx-agent-users - -"
      ];

      # The Codex bridge runs as the desktop user so ChatGPT/Codex credentials
      # never enter the system ctx-agent process. Non-operator users exit cleanly.
      systemd.user.services.ctx-codex-bridge = {
        description = "User-owned Codex bridge for ctx-agent";
        wantedBy = [ "default.target" ];
        serviceConfig = {
          Type = "simple";
          ExecStart = "${package}/bin/ctx-agent-codex-bridge --socket /run/ctx-codex/%u.sock --group ctx-agent-users";
          Restart = "on-failure";
          RestartSec = 3;
        };
      };
      systemd.services.ctx-agent = {
        description = "ctx-agent local task queue and isolated model coordinator";
        wantedBy = [ "multi-user.target" ];
        after = [ "network.target" ] ++ lib.optional cfg.manageOllama "ollama.service";
        serviceConfig = {
          Type = "simple";
          ExecStart = "${package}/bin/ctx-agent-server --config ${runtimeConfig}";
          User = "ctx-agent";
          Group = "ctx-agent-users";
          Restart = "on-failure";
          RestartSec = 4;
          UMask = "0007";
          StateDirectory = "ctx-agent";
          StateDirectoryMode = "0770";
          RuntimeDirectory = "ctx-agent";
          RuntimeDirectoryMode = "0750";
          NoNewPrivileges = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          ReadWritePaths = [ "/var/lib/ctx-agent" "/run/ctx-agent" ];
          PrivateTmp = true;
          PrivateDevices = true;
          # Do not enable ProtectKernelTunables here. Its outer mount-namespace
          # restrictions can prevent Bubblewrap from mounting the fresh /proc
          # required by the inner PID namespace. Bubblewrap still isolates the
          # worker with --unshare-all, and the coordinator remains unprivileged.
          ProtectKernelModules = true;
          ProtectControlGroups = true;
          ProtectClock = true;
          ProtectHostname = true;
          RestrictSUIDSGID = true;
          LockPersonality = true;
          CapabilityBoundingSet = "";
          AmbientCapabilities = "";
          # Bubblewrap needs AF_NETLINK to configure the isolated loopback
          # interface. The sandbox still has no host/network access because
          # its worker runs with --unshare-all.
          RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" "AF_NETLINK" ];
          IPAddressDeny = [ "any" ];
          IPAddressAllow = [ "localhost" ];
          SystemCallArchitectures = "native";
          Nice = 10;
          CPUWeight = 10;
          IOWeight = 10;
          TasksMax = 128;
          MemoryMax = "4G";
        };
      };
    }
    (mkIf cfg.manageOllama {
      services.ollama = {
        enable = true;
        package =
          if cfg.gpuBackend == "rocm" then pkgs.ollama-rocm
          else if cfg.gpuBackend == "vulkan" then pkgs.ollama-vulkan
          else pkgs.ollama-cpu;
      };
    })
  ]);
}
