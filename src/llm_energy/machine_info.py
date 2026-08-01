"""Collect machine metadata recorded with every result."""

from __future__ import annotations

import json
import os
import platform
import socket
import subprocess

from llm_energy.schemas import MachineInfo


def _run(cmd: list[str], timeout: int = 10) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _chip() -> str:
    sys = platform.system()
    if sys == "Darwin":
        return _run(["sysctl", "-n", "machdep.cpu.brand_string"])
    if sys == "Linux":
        try:
            for line in open("/proc/cpuinfo"):
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return ""


def _mem_bytes() -> int:
    if platform.system() == "Darwin":
        v = _run(["sysctl", "-n", "hw.memsize"])
        return int(v) if v.isdigit() else 0
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def docker_info_summary() -> dict:
    raw = _run(["docker", "info", "--format", "{{json .}}"], timeout=20)
    if not raw:
        return {"available": False}
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        return {"available": False}
    return {
        "available": True,
        "server_version": info.get("ServerVersion"),
        "operating_system": info.get("OperatingSystem"),
        "architecture": info.get("Architecture"),
        "ncpu": info.get("NCPU"),
        "mem_total": info.get("MemTotal"),
        "name": info.get("Name"),
    }


def collect(include_docker: bool = True) -> MachineInfo:
    sys = platform.system()
    if sys == "Darwin":
        os_version = f"macOS {_run(['sw_vers', '-productVersion'])}"
    else:
        os_version = platform.release()
    return MachineInfo(
        hostname=socket.gethostname(),
        platform=sys,
        os_version=os_version,
        arch=platform.machine(),
        chip=_chip(),
        cpu_count=os.cpu_count() or 0,
        mem_bytes=_mem_bytes(),
        docker=docker_info_summary() if include_docker else {},
    )
