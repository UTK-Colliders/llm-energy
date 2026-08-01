"""Linux fallbacks: RAPL sysfs counters, else a CPU-time × TDP model.

These exist mainly so the pipeline is exercisable on Linux (dev/CI). The
tdp-model backend is an estimate, not a measurement, and is labeled as such
in every result that uses it.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from llm_energy.power.base import ProbeResult
from llm_energy.schemas import PowerSample, PowerTrace

RAPL_ROOT = Path("/sys/class/powercap")


def _rapl_packages(root: Path = RAPL_ROOT) -> list[Path]:
    if not root.exists():
        return []
    pkgs = []
    for d in sorted(root.glob("intel-rapl:*")):
        # top-level package domains only (intel-rapl:0), not subzones (intel-rapl:0:0)
        if d.name.count(":") == 1 and (d / "energy_uj").exists():
            pkgs.append(d)
    return pkgs


class RaplBackend:
    """Snapshot-based: reads cumulative energy_uj before/after, handling wraparound.

    Emits a single synthetic sample spanning the whole measurement window, which
    integrates to the exact counter delta.
    """

    name = "rapl"

    def __init__(self):
        self._packages: list[Path] = []
        self._start_uj: list[int] = []
        self._max_uj: list[int] = []
        self._t0: float = 0.0

    def probe(self) -> ProbeResult:
        pkgs = _rapl_packages()
        if not pkgs:
            return ProbeResult(False, self.name, ["no readable /sys/class/powercap/intel-rapl:* domains"])
        try:
            for p in pkgs:
                int((p / "energy_uj").read_text())
        except PermissionError:
            return ProbeResult(False, self.name, ["energy_uj not readable (needs root)"])
        return ProbeResult(True, self.name, [f"{len(pkgs)} RAPL package domain(s)"])

    def start(self, interval_ms: int, raw_output_path: Path) -> None:
        self._packages = _rapl_packages()
        if not self._packages:
            raise RuntimeError("RAPL not available")
        self._start_uj = [int((p / "energy_uj").read_text()) for p in self._packages]
        self._max_uj = []
        for p in self._packages:
            mx = p / "max_energy_range_uj"
            self._max_uj.append(int(mx.read_text()) if mx.exists() else 2**63)
        self._t0 = time.monotonic()

    def stop(self) -> PowerTrace:
        elapsed = time.monotonic() - self._t0
        total_uj = 0
        for p, start, mx in zip(self._packages, self._start_uj, self._max_uj):
            end = int((p / "energy_uj").read_text())
            delta = end - start if end >= start else end + (mx - start)
            total_uj += delta
        joules = total_uj / 1e6
        mean_mw = (joules / elapsed) * 1000.0 if elapsed > 0 else 0.0
        sample = PowerSample(t_rel_s=0.0, elapsed_s=elapsed, combined_mw=mean_mw)
        return PowerTrace(backend=self.name, samples=[sample], combined_source="rapl-counter")


class TdpModelBackend:
    """E ≈ Σ (system CPU utilization × TDP) over sampled windows.

    Utilization is read from /proc/stat; TDP defaults to a config value or
    the LLM_ENERGY_TDP_W env var. Clearly an estimate — results carry
    backend="tdp-model".
    """

    name = "tdp-model"

    def __init__(self, tdp_w: float | None = None, sample_s: float = 1.0):
        env = os.environ.get("LLM_ENERGY_TDP_W")
        self.tdp_w = tdp_w if tdp_w is not None else (float(env) if env else 65.0)
        self.sample_s = sample_s
        self._samples: list[PowerSample] = []
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None

    def probe(self) -> ProbeResult:
        if not Path("/proc/stat").exists():
            return ProbeResult(False, self.name, ["/proc/stat not available"])
        return ProbeResult(True, self.name,
                           [f"estimate-only model, TDP={self.tdp_w} W (override with LLM_ENERGY_TDP_W)"])

    @staticmethod
    def _read_proc_stat() -> tuple[int, int]:
        fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        vals = [int(x) for x in fields]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
        return sum(vals), idle

    def _loop(self):
        prev_total, prev_idle = self._read_proc_stat()
        t_rel = 0.0
        prev_t = time.monotonic()
        while not self._stop_evt.wait(self.sample_s):
            total, idle = self._read_proc_stat()
            now = time.monotonic()
            dt = now - prev_t
            d_total, d_idle = total - prev_total, idle - prev_idle
            util = (d_total - d_idle) / d_total if d_total > 0 else 0.0
            self._samples.append(PowerSample(
                t_rel_s=t_rel, elapsed_s=dt, combined_mw=util * self.tdp_w * 1000.0))
            t_rel += dt
            prev_total, prev_idle, prev_t = total, idle, now

    def start(self, interval_ms: int, raw_output_path: Path) -> None:
        self.sample_s = max(interval_ms / 1000.0, 0.1)
        self._samples = []
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> PowerTrace:
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=self.sample_s + 2)
        return PowerTrace(backend=self.name, samples=list(self._samples),
                          combined_source="cpu-util-x-tdp")
