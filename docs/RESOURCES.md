# GPU / CPU scheduling

The agent has 3 Ollama request profiles:

| Mode | GPU layers | CPU threads | Context | Batch | Max output |
| --- | ---: | ---: | ---: | ---: | ---: |
| quiet (default) | 6 | 2 | 3072 | 64 | 1024 |
| balanced | full (`-1`) | 4 | 4096 | 256 | 1024 |
| maximum | full (`-1`) | Ollama selected | 8192 | 256 | 3072 |

Modes:

    ctx-agent mode quiet
    ctx-agent mode balanced
    ctx-agent mode maximum
    ctx-agent health

Quiet mode still uses the GPU. Its conservative GPU layer count is a *soft*
offloading limit, not a GPU utilization or VRAM cap. Balanced and maximum
request full GPU offload (`num_gpu = -1`), which lets Ollama fit as much of the
model on the accelerator as possible. Backend drivers may still adapt offload
counts. Concurrency safety must be verified on the host.

Quiet and balanced disable Qwen's explicit thinking tokens for lower latency.
Maximum enables thinking for tasks where deeper reasoning is worth the delay.
Ollama keeps a used model resident for five minutes between requests so a
short interactive work session does not repeatedly pay model-load latency.

The NixOS systemd CPUWeight and IOWeight lower the coordinator's priority
but do NOT directly limit the Ollama service, which is separate. Resource
mode controls apply to new inference calls, not currently running requests.
Maximum mode enables the larger context/output budget and model thinking;
it does not remove Linux cgroup quotas or security policy.

The optional F5-TTS media companion runs independently and its GPU demands
are not throttled by Ollama modes. It is only started by a human desktop
command. CPU priority is lowered unless invoked with --maximum.

Pause prevents new jobs but does not instantly stop an active generation.
To ensure quiet while gaming, stop the agent daemon and consider unloading
models from Ollama separately.
