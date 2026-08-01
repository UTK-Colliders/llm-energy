"""macOS powermetrics backend.

Invocation: sudo -n powermetrics --samplers cpu_power -i <ms> -o <file>
Stopped with SIGINT; the output file is then parsed. We parse the plain-text
format (not plist): the text lines are stable across macOS releases and
trivially fixture-testable, and the raw file doubles as an audit artifact.

powermetrics reports SoC package power (CPU+GPU+ANE) — not wall power.
DRAM (partially), SSD, display, and PSU losses are excluded; see
docs/methodology.md.
"""

from __future__ import annotations

import re
import shutil
import signal
import subprocess
from pathlib import Path

from llm_energy.power.base import ProbeResult
from llm_energy.schemas import PowerSample, PowerTrace

# "*** Sampled system activity (Fri Aug  1 10:00:01 2026 -0400) (1003.42ms elapsed) ***"
_BLOCK_RE = re.compile(
    r"^\*\*\* Sampled system activity \((?P<when>.+?)\) \((?P<elapsed>[\d.]+)ms elapsed\)"
)
_CPU_RE = re.compile(r"^CPU Power:\s+([\d.]+)\s*mW")
_GPU_RE = re.compile(r"^GPU Power:\s+([\d.]+)\s*mW")
_ANE_RE = re.compile(r"^ANE Power:\s+([\d.]+)\s*mW")
_COMBINED_RE = re.compile(r"^Combined Power \(CPU \+ GPU \+ ANE\):\s+([\d.]+)\s*mW")


def parse_powermetrics_text(text: str) -> PowerTrace:
    """Parse powermetrics --samplers cpu_power text output into a PowerTrace.

    Tolerates a truncated final block (SIGINT mid-write): a block is only
    emitted once its power lines are seen; a header with no power lines is
    dropped.
    """
    samples: list[PowerSample] = []
    t_rel = 0.0
    cur: dict | None = None
    used_combined_line = False
    used_component_sum = False

    def flush(block: dict | None):
        nonlocal t_rel, used_combined_line, used_component_sum
        if block is None:
            return
        combined = block.get("combined")
        if combined is None:
            parts = [block.get(k) for k in ("cpu", "gpu", "ane")]
            present = [p for p in parts if p is not None]
            if not present:
                return  # truncated block: header only, no power data
            combined = sum(present)
            used_component_sum = True
        else:
            used_combined_line = True
        elapsed_s = block["elapsed_ms"] / 1000.0
        samples.append(PowerSample(
            t_rel_s=t_rel,
            elapsed_s=elapsed_s,
            combined_mw=combined,
            cpu_mw=block.get("cpu"),
            gpu_mw=block.get("gpu"),
            ane_mw=block.get("ane"),
        ))
        t_rel += elapsed_s

    for line in text.splitlines():
        m = _BLOCK_RE.match(line)
        if m:
            flush(cur)
            cur = {"elapsed_ms": float(m.group("elapsed"))}
            continue
        if cur is None:
            continue
        for regex, key in ((_COMBINED_RE, "combined"), (_CPU_RE, "cpu"),
                           (_GPU_RE, "gpu"), (_ANE_RE, "ane")):
            pm = regex.match(line)
            if pm:
                cur[key] = float(pm.group(1))
                break
    flush(cur)

    if used_combined_line and not used_component_sum:
        source = "combined-line"
    elif used_component_sum and not used_combined_line:
        source = "component-sum"
    elif used_combined_line and used_component_sum:
        source = "mixed"
    else:
        source = "empty"
    return PowerTrace(backend="powermetrics", samples=samples, combined_source=source)


class PowermetricsBackend:
    name = "powermetrics"

    def __init__(self, binary: str = "/usr/bin/powermetrics"):
        self.binary = binary
        self._proc: subprocess.Popen | None = None
        self._raw_path: Path | None = None

    def probe(self) -> ProbeResult:
        msgs: list[str] = []
        if not (Path(self.binary).exists() or shutil.which("powermetrics")):
            return ProbeResult(False, self.name, ["powermetrics binary not found (macOS only)"])
        rc = subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode
        if rc != 0:
            msgs.append(
                "sudo requires a password (powermetrics needs root). Either run "
                "`sudo -v` right before measuring, or install the sudoers rule — "
                "see scripts/sudoers-powermetrics.md."
            )
            return ProbeResult(False, self.name, msgs)
        # one real 100 ms sample to verify it runs and parses
        try:
            out = subprocess.run(
                ["sudo", "-n", self.binary, "--samplers", "cpu_power", "-i", "100", "-n", "1"],
                capture_output=True, text=True, timeout=15,
            )
        except subprocess.TimeoutExpired:
            return ProbeResult(False, self.name, ["powermetrics probe timed out"])
        if out.returncode != 0:
            return ProbeResult(False, self.name, [f"powermetrics failed: {out.stderr.strip()[:200]}"])
        trace = parse_powermetrics_text(out.stdout)
        if not trace.samples:
            return ProbeResult(False, self.name,
                               ["powermetrics ran but no samples parsed — output format may have "
                                "changed; first 300 chars:\n" + out.stdout[:300]])
        s = trace.samples[0]
        msgs.append(f"parsed sample: combined={s.combined_mw:.0f} mW "
                    f"(cpu={s.cpu_mw}, gpu={s.gpu_mw}, ane={s.ane_mw}), "
                    f"source={trace.combined_source}")
        return ProbeResult(True, self.name, msgs)

    def start(self, interval_ms: int, raw_output_path: Path) -> None:
        raw_output_path.parent.mkdir(parents=True, exist_ok=True)
        self._raw_path = raw_output_path
        self._proc = subprocess.Popen(
            ["sudo", "-n", self.binary, "--samplers", "cpu_power",
             "-i", str(interval_ms), "-o", str(raw_output_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> PowerTrace:
        if self._proc is None or self._raw_path is None:
            raise RuntimeError("powermetrics backend was never started")
        died_early = self._proc.poll() is not None
        if died_early:
            stderr = (self._proc.stderr.read().decode(errors="replace")
                      if self._proc.stderr else "")
            raise RuntimeError(
                f"powermetrics exited early (rc={self._proc.returncode}); "
                f"energy for this run is invalid. stderr: {stderr.strip()[:300]}"
            )
        # sudo forwards SIGINT to powermetrics, which flushes and exits
        self._proc.send_signal(signal.SIGINT)
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        text = self._raw_path.read_text() if self._raw_path.exists() else ""
        trace = parse_powermetrics_text(text)
        if not trace.samples:
            raise RuntimeError(
                f"no powermetrics samples parsed from {self._raw_path}; "
                "run is invalid"
            )
        return trace
