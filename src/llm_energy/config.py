"""Load and validate task definitions (tasks/<name>/task.yaml)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ImageSpec:
    variant: str                      # "native" | "scailfin" | ...
    tag: str                          # image reference to run
    platform: str | None = None       # e.g. "linux/arm64"
    build_context: Path | None = None # set for build-type images
    dockerfile: str | None = None
    pull: str | None = None           # set for pull-type images (== tag)


@dataclass
class MountSpec:
    source: str                       # path relative to task dir, or "{run_dir}"
    target: str
    mode: str = "rw"


@dataclass
class ExpectedOutput:
    glob: str
    record_as: str


@dataclass
class PhaseSpec:
    """One measured step of a task.

    A task with several phases runs each in its own container, sharing the run
    directory, and gets its own energy figure per phase — so e.g. compilation
    can be separated from the work it enables.
    """
    name: str
    command: list[str]
    description: str = ""
    timeout_s: int | None = None       # falls back to the task's timeout


@dataclass
class TaskSpec:
    name: str
    description: str
    task_dir: Path
    images: dict[str, ImageSpec]
    default_image: str
    phases: list[PhaseSpec]
    mounts: list[MountSpec]
    workdir: str | None
    env: dict[str, str]
    timeout_s: int
    expected_outputs: list[ExpectedOutput]
    metadata: dict = field(default_factory=dict)

    @property
    def command(self) -> list[str]:
        """The single phase's command; kept for single-phase tasks."""
        if len(self.phases) != 1:
            raise ValueError(
                f"task '{self.name}' has {len(self.phases)} phases — use "
                "task.phases rather than task.command")
        return self.phases[0].command

    @property
    def multiphase(self) -> bool:
        return len(self.phases) > 1

    def image(self, variant: str | None = None) -> ImageSpec:
        v = variant or self.default_image
        if v not in self.images:
            raise ValueError(
                f"task '{self.name}' has no image variant '{v}' "
                f"(available: {', '.join(self.images)})")
        return self.images[v]


def load_task(task_dir: Path) -> TaskSpec:
    path = task_dir / "task.yaml"
    if not path.exists():
        raise FileNotFoundError(f"no task.yaml in {task_dir}")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a mapping")
    for key in ("name", "images", "default_image"):
        if key not in data:
            raise ValueError(f"{path}: missing required key '{key}'")
    if ("command" in data) == ("phases" in data):
        raise ValueError(
            f"{path}: needs exactly one of 'command' (single measured step) or "
            "'phases' (several, measured separately)")

    images: dict[str, ImageSpec] = {}
    for variant, spec in data["images"].items():
        has_build, has_pull = "build" in spec, "pull" in spec
        if has_build == has_pull:
            raise ValueError(
                f"{path}: image '{variant}' needs exactly one of build/pull")
        if has_build:
            images[variant] = ImageSpec(
                variant=variant,
                tag=spec["tag"],
                platform=spec.get("platform"),
                build_context=(task_dir / spec["build"].get("context", ".")).resolve(),
                dockerfile=spec["build"].get("dockerfile", "Dockerfile"),
            )
        else:
            images[variant] = ImageSpec(
                variant=variant,
                tag=spec["pull"],
                platform=spec.get("platform"),
                pull=spec["pull"],
            )
    if data["default_image"] not in images:
        raise ValueError(f"{path}: default_image '{data['default_image']}' not defined")

    mounts = []
    for m in data.get("mounts", []):
        if not str(m.get("target", "")).startswith("/"):
            raise ValueError(f"{path}: mount target must be absolute: {m}")
        mounts.append(MountSpec(source=m["source"], target=m["target"],
                                mode=m.get("mode", "rw")))

    expected = [ExpectedOutput(glob=o["glob"], record_as=o.get("record_as", o["glob"]))
                for o in data.get("expect", {}).get("outputs", [])]

    if "command" in data:
        phases = [PhaseSpec(name="run", command=[str(c) for c in data["command"]])]
    else:
        raw = data["phases"]
        if not isinstance(raw, list) or not raw:
            raise ValueError(f"{path}: 'phases' must be a non-empty list")
        phases = []
        for i, p in enumerate(raw):
            if "name" not in p or "command" not in p:
                raise ValueError(f"{path}: phase {i} needs 'name' and 'command'")
            phases.append(PhaseSpec(
                name=str(p["name"]),
                command=[str(c) for c in p["command"]],
                description=str(p.get("description", "")),
                timeout_s=int(p["timeout_s"]) if "timeout_s" in p else None,
            ))
        names = [p.name for p in phases]
        if len(set(names)) != len(names):
            raise ValueError(f"{path}: phase names must be unique, got {names}")

    return TaskSpec(
        name=data["name"],
        description=data.get("description", ""),
        task_dir=task_dir,
        images=images,
        default_image=data["default_image"],
        phases=phases,
        mounts=mounts,
        workdir=data.get("workdir"),
        env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
        timeout_s=int(data.get("timeout_s", 7200)),
        expected_outputs=expected,
        metadata=data.get("metadata", {}),
    )


def find_task(name: str, task_root: Path) -> TaskSpec:
    return load_task(task_root / name)
