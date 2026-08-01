"""Power measurement backend protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from llm_energy.schemas import PowerTrace


@dataclass
class ProbeResult:
    ok: bool
    backend: str
    messages: list[str] = field(default_factory=list)


@runtime_checkable
class PowerBackend(Protocol):
    name: str

    def start(self, interval_ms: int, raw_output_path: Path) -> None:
        """Begin sampling; must return quickly."""
        ...

    def stop(self) -> PowerTrace:
        """Stop sampling and return the full trace."""
        ...

    def probe(self) -> ProbeResult:
        """Non-destructive check that this backend can run (for `doctor`)."""
        ...
