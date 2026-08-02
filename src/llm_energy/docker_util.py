"""Thin wrapper around the docker CLI: run, stats sampling, cgroup CPU readout.

We shell out rather than depend on docker-py: the docker CLI is present
wherever Docker Desktop/colima is, and the calls are simple.
"""

from __future__ import annotations

import json
import platform
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from llm_energy.config import ImageSpec, TaskSpec


@dataclass
class ContainerRun:
    exit_code: int
    wall_time_s: float
    t0_monotonic: float
    t1_monotonic: float
    stats_samples: list[dict] = field(default_factory=list)
    cpu_seconds: float | None = None
    stdout_tail: str = ""


def docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True,
                              timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def running_containers() -> list[tuple[str, str]]:
    """(short id, image) for every container running right now.

    A container left over from an earlier run keeps drawing power, and the
    next run's idle baseline captures it as though it were the idle floor.
    That number is then subtracted from every measurement made against the
    baseline — so one leaked container quietly corrupts subsequent runs, and
    the drift check cannot see it because every recent baseline is wrong the
    same way.
    """
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.ID}}\t{{.Image}}"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    found = []
    for line in out.stdout.splitlines():
        cid, _, image = line.partition("\t")
        if cid.strip():
            found.append((cid.strip(), image.strip()))
    return found


def image_exists(tag: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", tag],
                          capture_output=True).returncode == 0


def image_arch(tag: str) -> str:
    out = subprocess.run(["docker", "image", "inspect", "-f", "{{.Architecture}}", tag],
                         capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ""


def host_arch() -> str:
    m = platform.machine()
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(m, m)


def is_emulated(tag: str) -> bool:
    ia = image_arch(tag)
    return bool(ia) and ia != host_arch()


def build_image(spec: ImageSpec, quiet: bool = False) -> None:
    cmd = ["docker", "build", "-t", spec.tag, "-f",
           str(spec.build_context / (spec.dockerfile or "Dockerfile"))]
    if spec.platform:
        cmd += ["--platform", spec.platform]
    if quiet:
        cmd += ["--quiet"]
    cmd.append(str(spec.build_context))
    subprocess.run(cmd, check=True)


def pull_image(spec: ImageSpec) -> None:
    cmd = ["docker", "pull"]
    if spec.platform:
        cmd += ["--platform", spec.platform]
    cmd.append(spec.tag)
    subprocess.run(cmd, check=True)


def ensure_image(spec: ImageSpec) -> None:
    if image_exists(spec.tag):
        return
    if spec.pull:
        pull_image(spec)
    elif spec.build_context:
        build_image(spec)
    else:
        raise RuntimeError(f"image {spec.tag} missing and no build/pull source")


def _read_cpu_seconds(container: str) -> float | None:
    """Cumulative container CPU time from the cgroup, via docker exec.

    cgroup v2: /sys/fs/cgroup/cpu.stat usage_usec; v1 fallback:
    /sys/fs/cgroup/cpuacct/cpuacct.usage (ns). Must be read while the
    container is alive (--rm removes it on exit).
    """
    out = subprocess.run(
        ["docker", "exec", container, "sh", "-c",
         "cat /sys/fs/cgroup/cpu.stat 2>/dev/null || "
         "cat /sys/fs/cgroup/cpuacct/cpuacct.usage 2>/dev/null"],
        capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        return None
    text = out.stdout.strip()
    if not text:
        return None
    if "usage_usec" in text:
        for line in text.splitlines():
            if line.startswith("usage_usec"):
                return int(line.split()[1]) / 1e6
        return None
    try:
        return int(text.splitlines()[0]) / 1e9
    except ValueError:
        return None


def _stats_once(container: str) -> dict | None:
    out = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", container],
        capture_output=True, text=True, timeout=15)
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return json.loads(out.stdout.strip().splitlines()[0])
    except json.JSONDecodeError:
        return None


class StatsSampler:
    """Samples docker stats and the cgroup CPU counter every interval_s.

    Keeps the last successful cgroup readout so CPU-seconds survive the
    container's --rm removal at exit.
    """

    def __init__(self, container: str, interval_s: float = 2.0):
        self.container = container
        self.interval_s = interval_s
        self.samples: list[dict] = []
        self.last_cpu_seconds: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self):
        t0 = time.monotonic()
        while not self._stop.is_set():
            s = _stats_once(self.container)
            if s is not None:
                s["t_rel_s"] = round(time.monotonic() - t0, 3)
                self.samples.append(s)
            try:
                cpu = _read_cpu_seconds(self.container)
            except subprocess.TimeoutExpired:
                cpu = None
            if cpu is not None:
                self.last_cpu_seconds = cpu
            self._stop.wait(self.interval_s)

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval_s + 20)


def run_container(task: TaskSpec, image: ImageSpec, run_dir: Path,
                  stats_interval_s: float = 2.0,
                  command: list[str] | None = None,
                  timeout_s: int | None = None) -> ContainerRun:
    """Run one container to completion, sampling stats along the way.

    `command` overrides the task's own for multi-phase tasks; each phase gets
    a fresh container over the same run directory and appends to one log.
    """
    name = f"llm-energy-{int(time.time() * 1000)}"
    cmd = ["docker", "run", "--rm", "--name", name]
    if image.platform:
        cmd += ["--platform", image.platform]
    for m in task.mounts:
        src = run_dir if m.source == "{run_dir}" else (task.task_dir / m.source).resolve()
        cmd += ["-v", f"{src}:{m.target}:{m.mode}"]
    if task.workdir:
        cmd += ["-w", task.workdir]
    for k, v in task.env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd.append(image.tag)
    phase_command = command if command is not None else task.command
    cmd += phase_command

    sampler = StatsSampler(name, interval_s=stats_interval_s)
    log_path = run_dir / "container.log"
    t0 = time.monotonic()
    # append: a multi-phase task keeps every phase's output in one log
    with log_path.open("a") as log:
        log.write(f"\n===== {' '.join(phase_command)} =====\n")
        log.flush()
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        sampler.start()
        try:
            exit_code = proc.wait(timeout=timeout_s or task.timeout_s)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", name], capture_output=True)
            proc.wait(timeout=30)
            exit_code = -1
    t1 = time.monotonic()
    sampler.stop()

    tail = ""
    try:
        text = log_path.read_text(errors="replace")
        tail = text[-2000:]
    except OSError:
        pass
    return ContainerRun(
        exit_code=exit_code,
        wall_time_s=t1 - t0,
        t0_monotonic=t0,
        t1_monotonic=t1,
        stats_samples=sampler.samples,
        cpu_seconds=sampler.last_cpu_seconds,
        stdout_tail=tail,
    )
