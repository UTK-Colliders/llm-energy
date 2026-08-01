"""Detect compiler output written during a measured window.

Splitting a task into "compile" and "run" phases is only meaningful if the
compilation really finished in the compile phase. Build systems are free to
disagree: a missed make target leaves object files to be produced during the
run phase, which silently moves energy from one bucket to the other.

Rather than trust the split, every phase counts the build artifacts written
inside its own window. Compiler output appearing in a phase that should not be
compiling is reported, not assumed away.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Extensions that only a compiler/archiver produces. `.mod` is a Fortran
# module file, which matters here: MadGraph's generated code is Fortran.
BUILD_SUFFIXES = frozenset({".o", ".a", ".so", ".mod", ".dylib", ".lo", ".obj"})


@dataclass
class BuildActivity:
    count: int = 0
    examples: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return self.count > 0


def count_build_artifacts(root: Path, t_start: datetime, t_end: datetime,
                          max_examples: int = 5) -> BuildActivity:
    """Build artifacts under `root` last modified within [t_start, t_end].

    Paths that vanish mid-walk are skipped: the run directory is a live bind
    mount and a build can delete its own intermediates.
    """
    if not root.exists():
        return BuildActivity()
    start, end = t_start.timestamp(), t_end.timestamp()
    activity = BuildActivity()
    for p in root.rglob("*"):
        if p.suffix not in BUILD_SUFFIXES:
            continue
        try:
            if not p.is_file():
                continue
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if start <= mtime <= end:
            activity.count += 1
            if len(activity.examples) < max_examples:
                activity.examples.append(str(p.relative_to(root)))
    return activity
