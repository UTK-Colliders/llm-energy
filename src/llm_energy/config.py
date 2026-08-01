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
class TaskSpec:
    name: str
    description: str
    task_dir: Path
    images: dict[str, ImageSpec]
    default_image: str
    command: list[str]
    mounts: list[MountSpec]
    workdir: str | None
    env: dict[str, str]
    timeout_s: int
    expected_outputs: list[ExpectedOutput]
    metadata: dict = field(default_factory=dict)

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
    for key in ("name", "images", "default_image", "command"):
        if key not in data:
            raise ValueError(f"{path}: missing required key '{key}'")

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

    return TaskSpec(
        name=data["name"],
        description=data.get("description", ""),
        task_dir=task_dir,
        images=images,
        default_image=data["default_image"],
        command=[str(c) for c in data["command"]],
        mounts=mounts,
        workdir=data.get("workdir"),
        env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
        timeout_s=int(data.get("timeout_s", 7200)),
        expected_outputs=expected,
        metadata=data.get("metadata", {}),
    )


def find_task(name: str, task_root: Path) -> TaskSpec:
    return load_task(task_root / name)
