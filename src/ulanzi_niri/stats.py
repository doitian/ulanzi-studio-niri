"""System metrics sampler for the wide tile (CPU, RAM, clock).

GPU is intentionally not sampled (per project decision); the GPU field of the
firmware payload is hardcoded to 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psutil


@dataclass
class StatsSnapshot:
    cpu: int  # 0..100
    mem: int  # 0..100
    gpu: int  # always 0
    time: str  # HH:MM:SS

    @classmethod
    def sample(cls, time_format: str = "%H:%M:%S") -> "StatsSnapshot":
        return cls(
            cpu=int(round(psutil.cpu_percent(interval=None))),
            mem=int(round(psutil.virtual_memory().percent)),
            gpu=0,
            time=datetime.now().strftime(time_format),
        )


def prime_cpu_sampler() -> None:
    """Call once at startup so the first sample isn't 0.0."""
    psutil.cpu_percent(interval=None)
