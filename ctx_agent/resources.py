"""Voluntary Ollama request budgets. These are NOT hard GPU usage caps."""

from __future__ import annotations

RESOURCE_MODES = ("quiet", "balanced", "maximum")

PROFILES = {
    # num_gpu controls approximate offloaded layers, not GPU utilization %.
    # Quiet minimizes interference. Balanced caps CPU work and uses partial
    # GPU offload so the desktop stays responsive. Maximum removes the CPU
    # thread cap and requests full GPU offload.
    "quiet": {"num_gpu": 6, "num_thread": 2,
              "num_ctx": 3072, "num_batch": 64, "num_predict": 1024},
    "balanced": {"num_gpu": 24, "num_thread": 4,
                 "num_ctx": 4096, "num_batch": 128, "num_predict": 1024},
    "maximum": {"num_gpu": -1, "num_ctx": 8192,
                "num_batch": 256, "num_predict": 3072},
}


def ollama_options(mode: str) -> dict:
    if mode not in PROFILES:
        raise ValueError("Unknown resource mode")
    return dict(PROFILES[mode])
