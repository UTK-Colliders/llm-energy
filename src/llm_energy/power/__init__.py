"""Power backend selection."""

from __future__ import annotations

import platform


def get_backend(name: str | None = None):
    """Return a PowerBackend. Auto-detects by platform unless name is given
    ("powermetrics" | "rapl" | "tdp-model")."""
    from llm_energy.power.linux_fallback import RaplBackend, TdpModelBackend
    from llm_energy.power.powermetrics import PowermetricsBackend

    if name == "powermetrics":
        return PowermetricsBackend()
    if name == "rapl":
        return RaplBackend()
    if name == "tdp-model":
        return TdpModelBackend()
    if name is not None:
        raise ValueError(f"unknown power backend: {name}")

    if platform.system() == "Darwin":
        return PowermetricsBackend()
    rapl = RaplBackend()
    if rapl.probe().ok:
        return rapl
    return TdpModelBackend()
