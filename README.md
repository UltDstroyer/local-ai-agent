# ctx-agent

Local-first AI coding assistant for NixOS. Work is queued persistently and processed through a local Ollama model. Untrusted file tools and test runs use offline Bubblewrap namespaces against disposable source-code snapshots. No Codex usage is needed.

**Status: technical preview, not yet host-verified.** Python tests run in GitHub CI. NixOS installation, Bubblewrap user namespaces, Ollama ROCm offloading and OBS interaction still require a test on your own hardware. Do not treat the security sandbox as audited.

## Install as a NixOS flake module

In the inputs of your current system flake add:

    ctx-agent.url = "github:UltDstroyer/local-ai-agent/milestone-0-security";

and include the module in your system modules list:

    ctx-agent.nixosModules.default

Then in your NixOS module configuration:

    services.ctxAgent = {
      enable = true;
      operatorUsers = [ "lena" ]; # replace if your login differs
      manageOllama = true; # false if you already manage Ollama
      model = "qwen3:8b";
    };

Review your configuration and rebuild with your existing nixos-rebuild workflow. Manually download the model using ollama pull qwen3:8b. Log out and back in so your group membership is refreshed.

Untrusted work happens only inside an allowlisted source-code snapshot. The daemon cannot inspect your usual /home, browser or credentials, and the model cannot apply edits to an original project on its own.

## Use

    ctx-agent health
    ctx-agent import demo ~/src/your-project
    # import creates a new named project from sanitized source files
    ctx-agent submit demo "Inspect this project and fix failing Python tests"
    ctx-agent list
    ctx-agent events JOB_ID
    ctx-agent diff JOB_ID
    ctx-agent apply JOB_ID # human review required

Only the local owner CLI can request apply; agent tools do not expose it. Apply does not delete files, though it can replace ordinary project files. Inspect the diff before approving.

## Resource modes

    ctx-agent mode quiet      # DEFAULT: GPU on, 6 layers, 2 CPU threads
    ctx-agent mode balanced   # GPU on, 18 layers, 4 CPU threads
    ctx-agent mode maximum    # no voluntary GPU-layer or CPU-thread cap
    ctx-agent pause
    ctx-agent resume

The Ollama GPU budgets are *hints*, not fixed VRAM or GPU-compute shares. Quiet inference may still temporarily stress the GPU. Details: docs/RESOURCES.md.

## Video and your own voice (separate desktop companion)

The Nix package also installs ctx-agent-media. It can use your manually configured OBS WebSocket scene for an explicitly started recording, enroll a voice reference, produce F5-TTS narration, and mix WAV narration onto MP4 video with ffmpeg. These features cannot be called autonomously by the AI worker, and F5-TTS itself must be installed separately for the Radeon ROCm environment. See docs/MEDIA.md.

## Access and privacy

- Trusted users in the ctx-agent-users group can submit tasks and request review/apply.
- No remote listener or ChatGPT connection is enabled.
- Model data goes to the Ollama service at loopback only.
- No arbitrary host shell command tool, sudo escalation, or unattended GUI access.
- Sensitive names, symlinks and hidden tool/cache folders are filtered when making snapshots.
- The original policy evaluator is retained for classification and tests; it is NOT a replacement for OS isolation.

Design: docs/SECURITY.md, docs/NIXOS.md, docs/ROADMAP.md. This version is a practical foundation rather than unrestricted machine control.
