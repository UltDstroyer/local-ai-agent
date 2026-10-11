# GPU / CPU scheduling

The agent has 3 Ollama request profiles:

| Mode | GPU layers | CPU threads | Context | Batch | Max output |
| --- | ---: | ---: | ---: | ---: | ---: |
| quiet (default) | 6 | 2 | 3072 | 64 | 1024 |
| balanced | 24 | 4 | 4096 | 128 | 1024 |
| maximum | full (`-1`) | Ollama selected | 8192 | 256 | 3072 |

Modes:

    ctx-agent mode quiet
    ctx-agent mode balanced
    ctx-agent mode maximum
    ctx-agent health

Quiet mode still uses the GPU. Its conservative GPU layer count is a *soft*
offloading limit, not a GPU utilization or VRAM cap. Balanced requests 24 GPU
layers and 4 CPU evaluation threads, deliberately leaving headroom for an
interactive desktop. Maximum requests full GPU offload (`num_gpu = -1`) and
does not set a CPU thread limit. Backend drivers may still create small helper
threads or adapt offload counts. Concurrency safety must be verified on the host.

Quiet and balanced disable Qwen's explicit thinking tokens for lower latency.
Maximum enables thinking for tasks where deeper reasoning is worth the delay.
Ollama keeps a used model resident for five minutes between requests so a
short interactive work session does not repeatedly pay model-load latency.

The NixOS systemd CPUWeight and IOWeight lower the coordinator's priority
but do NOT directly limit the Ollama service, which is separate. Resource
mode controls apply to new inference calls, not currently running requests.
Balanced mode is the normal interactive profile: the model request is limited
to 4 CPU evaluation threads and a smaller GPU batch. Maximum removes that
request-level CPU thread cap, enables full GPU offload, uses the larger
context/output budget, and enables model thinking. These modes do not remove
Linux cgroup quotas or security policy.

The optional F5-TTS media companion runs independently and its GPU demands
are not throttled by Ollama modes. It is only started by a human desktop
command. CPU priority is lowered unless invoked with --maximum.

Pause prevents new jobs but does not instantly stop an active generation.
To ensure quiet while gaming, stop the agent daemon and consider unloading
models from Ollama separately.


## Autonomous project scheduling

The local resource profile controls only the Ollama administrator. Codex runs
through the separate user-owned bridge and follows its own account/runtime
limits.

Plan creation and owner approval are available in every mode. New autonomous
Codex milestones are leased only while ctx-agent is in maximum mode. Switching
to balanced or quiet prevents the orchestrator from starting another milestone
after the current turn returns.

This makes maximum mean both full local-model resources and autonomous project
execution, while balanced remains the normal profile for using the desktop at
the same time.
