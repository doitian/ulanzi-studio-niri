"""Tests for the system metrics sampler, including NVML-backed GPU usage."""

from __future__ import annotations

from ulanzi_niri import stats


class _FakeUtil:
    gpu = 55


class _FakeNvml:
    class NVMLError(Exception):
        pass

    @staticmethod
    def nvmlInit() -> None:
        return None

    @staticmethod
    def nvmlDeviceGetHandleByIndex(_index: int) -> object:
        return object()

    @staticmethod
    def nvmlDeviceGetUtilizationRates(_handle: object) -> _FakeUtil:
        return _FakeUtil()


def _reset(monkeypatch, fake) -> None:
    monkeypatch.setattr(stats, "pynvml", fake)
    stats._nvml_state = "unknown"
    stats._nvml_handle = None


def test_gpu_percent_reads_nvml(monkeypatch) -> None:
    _reset(monkeypatch, _FakeNvml)
    assert stats.gpu_percent() == 55
    assert stats._nvml_state == "ready"


def test_gpu_percent_zero_when_pynvml_missing(monkeypatch) -> None:
    _reset(monkeypatch, None)
    assert stats.gpu_percent() == 0
    assert stats._nvml_state == "failed"


def test_gpu_percent_zero_when_init_fails(monkeypatch) -> None:
    class _BrokenNvml(_FakeNvml):
        @staticmethod
        def nvmlInit() -> None:
            raise _FakeNvml.NVMLError

    _reset(monkeypatch, _BrokenNvml)
    assert stats.gpu_percent() == 0
    assert stats._nvml_state == "failed"


def test_sample_uses_gpu_percent(monkeypatch) -> None:
    monkeypatch.setattr(stats, "gpu_percent", lambda: 33)
    snap = stats.StatsSnapshot.sample()
    assert 0 <= snap.cpu <= 100
    assert 0 <= snap.mem <= 100
    assert snap.gpu == 33
    assert len(snap.time) == 8
