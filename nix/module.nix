{ config, lib, pkgs, ... }:
let
  inherit (lib) mkEnableOption mkOption types mkIf mkMerge;
  cfg = config.services.ctxAgent;
  package = pkgs.callPackage ./package.nix { };
  runtimeConfig = pkgs.writeText "ctx-agent-runtime.json" (builtins.toJSON {
    state_dir = "/var/lib/ctx-agent";
    socket_path = "/run/ctx-agent/agent.sock";
    projects_dir = "/var/lib/ctx-agent/projects";
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
      users.users.ctx-agent = {
        isSystemUser = true;
        group = "ctx-agent-users";
        home = "/var/lib/ctx-agent";
        description = "Unprivileged isolated local AI coordinator";
      };
      users.users = lib.genAttrs cfg.operatorUsers (_: {
        extraGroups = [ "ctx-agent-users" ];
      });
      environment.systemPackages = [ package ];
      systemd.tmpfiles.rules = [
        "d /var/lib/ctx-agent/projects 0770 ctx-agent ctx-agent-users - -"
        "d /var/lib/ctx-agent/jobs 0700 ctx-agent ctx-agent-users - -"
      ];
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
          ProtectKernelTunables = true;
          ProtectKernelModules = true;
          ProtectControlGroups = true;
          ProtectClock = true;
          ProtectHostname = true;
          RestrictSUIDSGID = true;
          LockPersonality = true;
          CapabilityBoundingSet = "";
          AmbientCapabilities = "";
          RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" ];
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
