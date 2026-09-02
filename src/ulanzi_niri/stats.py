"""System metrics sampler for the wide tile (CPU, RAM, GPU, clock)."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime

import psutil

try:
    import pynvml
except ImportError:  # pragma: no cover - only when nvidia-ml-py is absent
    pynvml = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

_nvml_lock = threading.Lock()
_nvml_state: str = "unknown"  # "unknown" | "ready" | "failed"
_nvml_handle: object | None = None


@dataclass
class StatsSnapshot:
    cpu: int  # 0..100
    mem: int  # 0..100
    gpu: int  # 0..100
    time: str  # HH:MM:SS

    @classmethod
    def sample(cls, time_format: str = "%H:%M:%S") -> StatsSnapshot:
        return cls(
            cpu=int(round(psutil.cpu_percent(interval=None))),
            mem=int(round(psutil.virtual_memory().percent)),
            gpu=gpu_percent(),
            time=datetime.now().strftime(time_format),
        )


def prime_cpu_sampler() -> None:
    """Call once at startup so the first sample isn't 0.0."""
    psutil.cpu_percent(interval=None)


def gpu_percent() -> int:
    """GPU utilization percent (0..100) or 0 when no NVIDIA GPU is available."""
    if not _ensure_nvml():
        return 0
    assert _nvml_handle is not None
    try:
        return int(pynvml.nvmlDeviceGetUtilizationRates(_nvml_handle).gpu)
    except pynvml.NVMLError:
        return 0


def _ensure_nvml() -> bool:
    global _nvml_state, _nvml_handle
    if _nvml_state != "unknown":
        return _nvml_state == "ready"
    with _nvml_lock:
        if _nvml_state != "unknown":
            return _nvml_state == "ready"
        if pynvml is None:
            _nvml_state = "failed"
            return False
        try:
            pynvml.nvmlInit()
            _nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        except (pynvml.NVMLError, IndexError):
            log.debug("NVML unavailable; GPU usage reads 0", exc_info=True)
            _nvml_state = "failed"
            return False
        _nvml_state = "ready"
        return True
