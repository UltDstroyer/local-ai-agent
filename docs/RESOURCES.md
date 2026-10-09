# GPU / CPU scheduling

The agent has 3 Ollama request profiles:

| Mode | GPU layers | CPU threads | Context | Batch | Max output |
| --- | ---: | ---: | ---: | ---: | ---: |
| quiet (default) | 6 | 2 | 3072 | 64 | 1024 |
| balanced | 18 | 4 | 4096 | 128 | 2048 |
| maximum | Ollama selected | Ollama selected | 8192 | Ollama selected | 3072 |

Modes:

    ctx-agent mode quiet
    ctx-agent mode balanced
    ctx-agent mode maximum
    ctx-agent health

Quiet mode still uses the GPU. A conservative GPU layer count is a *soft*
offloading limit, not a GPU utilization or VRAM cap. Ollama may still briefly
occupy most of the GPU compute capacity, and backend drivers may adapt
offload counts. Concurrency safety must be verified on the host.

The NixOS systemd CPUWeight and IOWeight lower the coordinator's priority
but do NOT directly limit the Ollama service, which is separate. Resource
mode controls apply to new inference calls, not currently running requests.
Maximum mode removes voluntary thread/batch/offload-layer restrictions,
not Linux cgroup quotas or security policy.

The optional F5-TTS media companion runs independently and its GPU demands
are not throttled by Ollama modes. It is only started by a human desktop
command. CPU priority is lowered unless invoked with --maximum.

Pause prevents new jobs but does not instantly stop an active generation.
To ensure quiet while gaming, stop the agent daemon and consider unloading
models from Ollama separately.
