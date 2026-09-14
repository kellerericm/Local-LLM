"""Keep the agent secondary to everything else on the computer.

- The model worker runs at below-normal priority (set in worker.py; child commands too).
- When other programs use the GPU heavily, generation pauses (per token) until they're done.
- The model unloads after an idle period, freeing VRAM.
- Background hours gate long-running jobs (used by the long-term task system).
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

BUSY_SAMPLES_TO_PAUSE = 2      # ~4s of sustained load before pausing
CLEAR_SAMPLES_TO_RESUME = 3


def in_hours(spec: str, now: _dt.datetime | None = None) -> bool:
    """'22-7' means 22:00 to 07:00. Empty means always."""
    spec = (spec or "").strip()
    if not spec:
        return True
    try:
        start, end = (int(x) for x in spec.split("-"))
    except ValueError:
        return True
    hour = (now or _dt.datetime.now()).hour
    return start <= hour < end if start <= end else hour >= start or hour < end


class GpuProbe:
    """Measures GPU use by processes other than ours. Falls back gracefully if NVML lacks per-process data."""

    def __init__(self):
        self.ok = False
        self._last_ts = 0
        try:
            import pynvml
            pynvml.nvmlInit()
            self._nvml = pynvml
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self.ok = True
        except Exception as e:
            log.info("NVML unavailable, GPU-busy pausing disabled: %s", e)

    def other_load(self, own_pids: set[int], we_are_generating: bool) -> tuple[float | None, float]:
        """Returns (other processes' util %, other processes' VRAM GB). util None = unknown."""
        nv = self._nvml
        mem_gb = 0.0
        for getter in ("nvmlDeviceGetComputeRunningProcesses", "nvmlDeviceGetGraphicsRunningProcesses"):
            try:
                for p in getattr(nv, getter)(self._handle):
                    if p.pid not in own_pids and p.usedGpuMemory:
                        mem_gb += p.usedGpuMemory / 2**30
            except Exception:
                pass
        util = None
        try:
            samples = nv.nvmlDeviceGetProcessUtilization(self._handle, self._last_ts)
            if samples:
                self._last_ts = max(s.timeStamp for s in samples)
            util = float(sum(s.smUtil for s in samples if s.pid not in own_pids))
        except Exception:
            if not we_are_generating:       # whole-device util is only meaningful while we're idle
                try:
                    util = float(nv.nvmlDeviceGetUtilizationRates(self._handle).gpu)
                except Exception:
                    util = None
        return util, mem_gb


class ResourceManager:
    def __init__(self, settings_getter: Callable, backend, emit: Callable[[dict], None]):
        self.settings_getter = settings_getter
        self.backend = backend
        self.emit = emit
        self.gpu_busy = False
        self.other_util: float | None = None
        self.other_mem_gb = 0.0
        self._busy_count = 0
        self._clear_count = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._probe: GpuProbe | None = None

    def start(self) -> None:
        self._probe = GpuProbe()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="resource-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        res = self.settings_getter().resources
        return {"gpu_busy": self.gpu_busy, "other_util_pct": self.other_util, "other_mem_gb": round(self.other_mem_gb, 2),
                "nvml": bool(self._probe and self._probe.ok), "in_background_hours": in_hours(res.background_hours)}

    def wait_until_clear(self, cancel: threading.Event, on_status: Callable[[str], None]) -> None:
        announced = False
        while self.gpu_busy and not cancel.is_set():
            if not announced:
                on_status("paused_gpu_busy")
                announced = True
            time.sleep(1)
        if announced:
            on_status("running")

    def _loop(self) -> None:
        while not self._stop.wait(2.0):
            try:
                self._tick()
            except Exception:
                log.exception("resource monitor tick failed")

    def _tick(self) -> None:
        res = self.settings_getter().resources
        # Idle unload
        if res.idle_unload_minutes > 0 and self.backend.is_loaded() and not self.backend.is_busy():
            idle = time.time() - getattr(self.backend, "last_used", time.time())
            if idle > res.idle_unload_minutes * 60 and self.backend.unload():
                log.info("unloaded model after %.0f idle minutes", idle / 60)
                self.emit({"type": "model_status", "status": self.backend.status()})
        # GPU contention
        if not (self._probe and self._probe.ok):
            return
        own = {os.getpid()}
        pid = getattr(self.backend, "worker_pid", None)
        if pid:
            own.add(pid)
        util, mem = self._probe.other_load(own, self.backend.is_busy())
        self.other_util, self.other_mem_gb = util, mem
        busy_now = res.pause_when_gpu_busy and (
            (util is not None and util >= res.gpu_busy_util_pct) or mem >= res.gpu_busy_mem_gb)
        if busy_now:
            self._busy_count += 1
            self._clear_count = 0
        else:
            self._clear_count += 1
            self._busy_count = 0
        changed = False
        if not self.gpu_busy and self._busy_count >= BUSY_SAMPLES_TO_PAUSE:
            self.gpu_busy = changed = True
        elif self.gpu_busy and (self._clear_count >= CLEAR_SAMPLES_TO_RESUME or not res.pause_when_gpu_busy):
            self.gpu_busy, changed = False, True
        if changed:
            self.backend.set_paused(self.gpu_busy)
            self.emit({"type": "resources", "status": self.status()})
