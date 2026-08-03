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
        """Rectangle-rule integral over [t_start, t_end] (trace-relative
        seconds). Each sample is constant power over its own window, and a
        sample straddling a bound contributes only the overlapping fraction.
        Windows tile the trace, so with no bounds this is exact given the
        samples.

        Attribution used to be all-or-nothing on the sample midpoint, which
        breaks down once a window is comparable to the sampling interval: a
        0.4 s container either swallowed a whole 1 s sample — reporting 21 J
        at 264 W — or missed one and reported a clean zero. Clipping keeps
        short windows proportionate and still sums to the same total.
        """
        total = 0.0
        for s in self.samples:
            lo, hi = s.t_rel_s, s.t_rel_s + s.elapsed_s
            if t_start is not None:
                lo = max(lo, t_start)
            if t_end is not None:
                hi = min(hi, t_end)
            if hi <= lo:
                continue
            total += (s.combined_mw / 1000.0) * (hi - lo)
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
    # Containers running during the capture. Their draw is inside this
    # baseline and gets subtracted from every measurement made against it.
    containers_running: list[str] = field(default_factory=list)
    raw_trace_file: str | None = None
    schema_version: int = SCHEMA_VERSION
    tool_version: str = __version__
    created_at: str = field(default_factory=now_iso)
    kind: str = "baseline"


@dataclass
class PhaseResult:
    """Energy for one measured step of a multi-phase task."""
    name: str
    command: list[str]
    wall_time_s: float
    gross_joules: float
    net_joules: float | None
    mean_power_w: float
    alignment_uncertainty_j: float
    exit_code: int
    container_cpu_seconds: float | None = None
    started_at: str = ""
    ended_at: str = ""
    # compiler output written during this phase — the check that a
    # compile/run split actually held
    build_artifacts_written: int = 0
    build_artifact_examples: list[str] = field(default_factory=list)
    description: str = ""


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
    # per-phase breakdown; single-phase tasks carry one entry named "run", so
    # the top-level totals always equal the sum over phases
    phases: list[PhaseResult] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION
    tool_version: str = __version__
    created_at: str = field(default_factory=now_iso)
    kind: str = "task-run"


@dataclass
class ObservedContainer:
    """A container the agent started, seen via the Docker event stream."""
    container_id: str
    image: str
    started_at: str
    ended_at: str
    wall_time_s: float
    gross_joules: float
    mean_power_w: float
    # True when the container outlived the session: its window was clamped to
    # the session end, so this is a lower bound and the run was cut off
    still_running: bool = False


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
    # Dispersion of the baseline capture. Sets the resolution of every net
    # figure derived from it: subtracting a 0.48 +/- 0.10 W floor from a
    # 2700 s window carries about +/-270 J of slack before the answer means
    # anything, and a coordination term smaller than that is a zero.
    baseline_std_w: float | None = None
    started_at: str = ""
    ended_at: str = ""
    # sessions whose transcript began inside the measured window
    session_ids: list[str] = field(default_factory=list)
    cwd: str | None = None
    power_trace_file: str | None = None
    # Containers the agent ran, and the energy inside them. For an open task
    # this is the closest thing to E_task: the harness did not launch the work,
    # so it timed it by watching the Docker daemon instead. `container_joules`
    # is over the *union* of windows, so concurrent containers are not
    # double-counted and it stays subtractable from the session total.
    containers: list[ObservedContainer] = field(default_factory=list)
    container_joules: float | None = None
    container_wall_s: float | None = None
    notes: list[str] = field(default_factory=list)
    machine: MachineInfo = field(default_factory=MachineInfo)
    schema_version: int = SCHEMA_VERSION
    tool_version: str = __version__
    created_at: str = field(default_factory=now_iso)
    kind: str = "session-power"

    def baseline_usable(self) -> bool:
        """Whether the idle baseline can be subtracted from this session.

        A baseline at or above the session's own mean power is not an idle
        floor: it was captured while the machine was busier than the session
        it is meant to correct. Subtracting it yields negative "energy", which
        is not a small correction pointing the wrong way but a sign the
        measurement is unusable.
        """
        if not self.power_ok or self.baseline_mean_w is None:
            return False
        return self.baseline_mean_w < self.mean_power_w

    def outside_container_joules(self, net: bool = True) -> float | None:
        """Energy spent outside any container — the agent thinking and reading.

        Net of the idle baseline by default. An agent session is mostly spent
        waiting on the network, so gross would be dominated by draw the machine
        would have had anyway; the marginal cost of the agent working is the
        quantity of interest. Falls back to gross when no baseline was applied
        or when the one on file is not usable.
        """
        if not self.power_ok or self.container_joules is None:
            return None
        outside = self.gross_joules - self.container_joules
        if not net or not self.baseline_usable():
            return outside
        idle_s = self.wall_time_s - (self.container_wall_s or 0.0)
        return outside - self.baseline_mean_w * idle_s

    def coordination_resolution_j(self) -> float | None:
        """How small a coordination figure has to be before it means nothing.

        E_coord,local is the difference of two nearly equal numbers: what the
        machine drew outside the containers, and what an idle machine would
        have drawn over the same stretch. When an agent spends that stretch
        waiting on the network those two agree to within the baseline's own
        scatter, and the difference is noise wearing the units of energy.
        """
        if self.baseline_std_w is None or not self.baseline_usable():
            return None
        idle_s = self.wall_time_s - (self.container_wall_s or 0.0)
        return abs(self.baseline_std_w) * max(idle_s, 0.0)

    def coordination_is_zero(self) -> bool:
        """True when E_coord,local is smaller than the baseline's own noise."""
        outside = self.outside_container_joules()
        resolution = self.coordination_resolution_j()
        return (outside is not None and resolution is not None
                and abs(outside) < resolution)

    def container_net_joules(self) -> float | None:
        """Container energy net of the idle baseline over their windows."""
        if not self.power_ok or self.container_joules is None:
            return None
        if not self.baseline_usable():
            return self.container_joules
        return (self.container_joules
                - self.baseline_mean_w * (self.container_wall_s or 0.0))

    def energy_basis(self) -> str:
        if self.baseline_usable():
            return "net of idle baseline"
        if self.baseline_mean_w is not None and self.power_ok:
            return "gross — idle baseline rejected"
        return "gross"


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
    data["phases"] = [_from_dict(PhaseResult, p) for p in data.get("phases", [])]
    return _from_dict(TaskRunResult, data)


def load_session_power(path: Path) -> SessionPowerResult:
    data = json.loads(path.read_text())
    data["machine"] = _from_dict(MachineInfo, data.get("machine", {}))
    data["containers"] = [_from_dict(ObservedContainer, c)
                          for c in data.get("containers", [])]
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
