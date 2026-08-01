"""Dataclasses for all result objects, with JSON round-tripping.

Every result written to disk carries schema_version, tool_version, a
TZ-aware creation timestamp, and machine metadata so runs remain
interpretable long after the machine that produced them changed.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from llm_energy import __version__

SCHEMA_VERSION = 1


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MachineInfo:
    hostname: str = ""
    platform: str = ""          # "Darwin" | "Linux"
    os_version: str = ""
    arch: str = ""              # "arm64" | "x86_64"
    chip: str = ""              # e.g. "Apple M2 Pro"
    cpu_count: int = 0
    mem_bytes: int = 0
    docker: dict[str, Any] = field(default_factory=dict)


@dataclass
class PowerSample:
    t_rel_s: float              # seconds since trace start (sum of prior windows)
    elapsed_s: float            # actual sample window reported by the backend
    combined_mw: float          # package power used for integration
    cpu_mw: float | None = None
    gpu_mw: float | None = None
    ane_mw: float | None = None


@dataclass
class PowerTrace:
    backend: str                # "powermetrics" | "rapl" | "tdp-model"
    samples: list[PowerSample] = field(default_factory=list)
    combined_source: str = ""   # "combined-line" | "component-sum" | backend-specific

    def integrate_joules(self, t_start: float | None = None, t_end: float | None = None) -> float:
        """Rectangle-rule integral over samples whose window midpoint lies in
        [t_start, t_end] (trace-relative seconds). Windows tile the trace, so
        with no bounds this is exact given the samples."""
        total = 0.0
        for s in self.samples:
            mid = s.t_rel_s + s.elapsed_s / 2.0
            if t_start is not None and mid < t_start:
                continue
            if t_end is not None and mid > t_end:
                continue
            total += (s.combined_mw / 1000.0) * s.elapsed_s
        return total

    def duration_s(self) -> float:
        if not self.samples:
            return 0.0
        last = self.samples[-1]
        return last.t_rel_s + last.elapsed_s

    def mean_watts(self) -> float:
        dur = self.duration_s()
        return self.integrate_joules() / dur if dur > 0 else 0.0


@dataclass
class BaselineResult:
    duration_s: float
    mean_w: float
    std_w: float
    joules: float
    n_samples: int
    min_w: float
    max_w: float
    backend: str
    docker_running: bool
    machine: MachineInfo
    raw_trace_file: str | None = None
    schema_version: int = SCHEMA_VERSION
    tool_version: str = __version__
    created_at: str = field(default_factory=now_iso)
    kind: str = "baseline"


@dataclass
class TaskRunResult:
    task_name: str
    image: str
    image_arch: str
    emulated: bool
    wall_time_s: float
    gross_joules: float
    baseline_ref: str | None
    baseline_mean_w: float | None
    net_joules: float | None
    mean_power_w: float
    alignment_uncertainty_j: float
    backend: str
    container_cpu_seconds: float | None
    docker_stats_samples: list[dict] = field(default_factory=list)
    exit_code: int = 0
    outputs: dict[str, Any] = field(default_factory=dict)
    power_trace_file: str | None = None
    task_metadata: dict[str, Any] = field(default_factory=dict)
    machine: MachineInfo = field(default_factory=MachineInfo)
    # Claude Code session that invoked this run, captured from
    # CLAUDE_CODE_SESSION_ID. Lets `analyze-session --for-task` pair the run
    # with the exact conversation that coordinated it, instead of guessing by
    # mtime. None when run-task was invoked outside a Claude Code session.
    coordinating_session_id: str | None = None
    started_at: str = ""
    ended_at: str = ""
    schema_version: int = SCHEMA_VERSION
    tool_version: str = __version__
    created_at: str = field(default_factory=now_iso)
    kind: str = "task-run"


@dataclass
class SessionPowerResult:
    """Package power measured across a whole coordination session.

    `run-task` measures only the container's window; this covers the entire
    time the agent was working locally — thinking, reading files, running
    exploratory commands — with the measured task run nested inside it.
    """
    command: list[str]
    wall_time_s: float
    gross_joules: float
    baseline_ref: str | None
    baseline_mean_w: float | None
    net_joules: float | None
    mean_power_w: float
    alignment_uncertainty_j: float
    backend: str
    exit_code: int
    # False when the sampler died mid-session: the session ids remain valid
    # but every energy figure here must be ignored
    power_ok: bool = True
    started_at: str = ""
    ended_at: str = ""
    # sessions whose transcript began inside the measured window
    session_ids: list[str] = field(default_factory=list)
    cwd: str | None = None
    power_trace_file: str | None = None
    notes: list[str] = field(default_factory=list)
    machine: MachineInfo = field(default_factory=MachineInfo)
    schema_version: int = SCHEMA_VERSION
    tool_version: str = __version__
    created_at: str = field(default_factory=now_iso)
    kind: str = "session-power"


@dataclass
class ModelUsage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    requests: int = 0           # deduped assistant API responses

    def total_tokens(self) -> int:
        return (self.input_tokens + self.output_tokens
                + self.cache_creation_tokens + self.cache_read_tokens)


@dataclass
class SessionUsage:
    session_ids: list[str] = field(default_factory=list)
    cwd: str | None = None
    started_at: str = ""
    ended_at: str = ""
    wall_time_s: float = 0.0
    assistant_turns: int = 0
    user_turns: int = 0
    sidechain_turns: int = 0
    per_model: list[ModelUsage] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class EnergyBand:
    low_j: float = 0.0
    central_j: float = 0.0
    high_j: float = 0.0

    def as_wh(self) -> tuple[float, float, float]:
        return (self.low_j / 3600.0, self.central_j / 3600.0, self.high_j / 3600.0)

    def __add__(self, other: "EnergyBand") -> "EnergyBand":
        return EnergyBand(self.low_j + other.low_j,
                          self.central_j + other.central_j,
                          self.high_j + other.high_j)


@dataclass
class SessionEnergyResult:
    usage: SessionUsage
    coefficients_file: str
    coefficients_sha256: str
    per_model_bands: dict[str, EnergyBand] = field(default_factory=dict)
    total_band: EnergyBand = field(default_factory=EnergyBand)
    pue: float = 1.0
    notes: list[str] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION
    tool_version: str = __version__
    created_at: str = field(default_factory=now_iso)
    kind: str = "session-energy"


# --- JSON I/O -------------------------------------------------------------

def to_json_dict(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_json_dict(v) for k, v in dataclasses.asdict(obj).items()}
    return obj


def write_result(obj: Any, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_json_dict(obj), indent=2) + "\n")
    return path


def _from_dict(cls, data: dict):
    """Build a dataclass from a dict, ignoring unknown keys (forward compat)."""
    names = {f.name: f for f in dataclasses.fields(cls)}
    kwargs = {}
    for k, v in data.items():
        if k not in names:
            continue
        kwargs[k] = v
    return cls(**kwargs)


def load_baseline(path: Path) -> BaselineResult:
    data = json.loads(path.read_text())
    data["machine"] = _from_dict(MachineInfo, data.get("machine", {}))
    return _from_dict(BaselineResult, data)


def load_task_result(path: Path) -> TaskRunResult:
    data = json.loads(path.read_text())
    data["machine"] = _from_dict(MachineInfo, data.get("machine", {}))
    return _from_dict(TaskRunResult, data)


def load_session_power(path: Path) -> SessionPowerResult:
    data = json.loads(path.read_text())
    data["machine"] = _from_dict(MachineInfo, data.get("machine", {}))
    return _from_dict(SessionPowerResult, data)


def load_session_result(path: Path) -> SessionEnergyResult:
    data = json.loads(path.read_text())
    usage = data.get("usage", {})
    usage["per_model"] = [_from_dict(ModelUsage, m) for m in usage.get("per_model", [])]
    data["usage"] = _from_dict(SessionUsage, usage)
    data["per_model_bands"] = {
        k: _from_dict(EnergyBand, v) for k, v in data.get("per_model_bands", {}).items()
    }
    data["total_band"] = _from_dict(EnergyBand, data.get("total_band", {}))
    return _from_dict(SessionEnergyResult, data)
