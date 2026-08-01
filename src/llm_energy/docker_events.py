"""Observe containers the agent starts, without constraining how it works.

When the agent decides for itself how to run the job, the harness is no longer
the thing launching containers, so it cannot time them directly. It can still
watch: the Docker daemon's event stream reports every container start and stop
with a timestamp, whoever started it.

That recovers the decomposition the harness would otherwise lose — energy
spent inside containers (the computation) versus outside them (the agent
working out what to do) — from a passive observer, with no cooperation
required from the agent and no restriction on what it may run.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# statuses that open and close a container's lifetime
_START = {"start"}
_END = {"die", "kill", "stop", "destroy"}


@dataclass
class ContainerWindow:
    container_id: str
    image: str
    started_at: datetime
    ended_at: datetime | None = None

    @property
    def wall_time_s(self) -> float:
        if self.ended_at is None:
            return 0.0
        return (self.ended_at - self.started_at).total_seconds()


class DockerEventRecorder:
    """Streams `docker events` to a file for the duration of a session.

    Recorded live rather than queried afterwards: the daemon's event history
    is not guaranteed to be retained, and a long session would age out of it.
    """

    def __init__(self, out_path: Path):
        self.out_path = out_path
        self._proc: subprocess.Popen | None = None
        self._fh = None
        # False once the stream is known to have failed. An empty event log is
        # ambiguous — no containers ran, or the stream never worked — and the
        # two must not be confused: the second would attribute every joule of
        # compute to coordination while looking like a clean measurement.
        self.healthy = False

    def start(self) -> bool:
        """True if the recorder is running; False if docker is unavailable."""
        try:
            self.out_path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.out_path.open("w")
            self._proc = subprocess.Popen(
                ["docker", "events", "--filter", "type=container",
                 "--format", "{{json .}}"],
                stdout=self._fh, stderr=subprocess.DEVNULL)
        except OSError:
            self._cleanup()
            return False
        self.healthy = True
        return True

    def _cleanup(self):
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def stop(self) -> list[ContainerWindow]:
        if self._proc is not None:
            # A stream that exited on its own never watched the session —
            # `docker events` returns immediately when the daemon is
            # unreachable, and the empty log it leaves behind is
            # indistinguishable from "the agent ran no containers".
            if self._proc.poll() is not None:
                self.healthy = False
            else:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=5)
            self._proc = None
        self._cleanup()
        if not self.out_path.exists():
            return []
        return parse_events(self.out_path.read_text().splitlines())


def _event_time(rec: dict) -> datetime | None:
    nano = rec.get("timeNano")
    if isinstance(nano, (int, float)) and nano:
        return datetime.fromtimestamp(nano / 1e9, tz=timezone.utc)
    secs = rec.get("time")
    if isinstance(secs, (int, float)) and secs:
        return datetime.fromtimestamp(secs, tz=timezone.utc)
    return None


def parse_events(lines) -> list[ContainerWindow]:
    """Pair container start/stop events into windows, oldest first.

    A container still running when the stream ends has `ended_at=None`; its
    energy is not attributed, since the window never closed.
    """
    open_windows: dict[str, ContainerWindow] = {}
    done: list[ContainerWindow] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        status = rec.get("status") or rec.get("Action")
        cid = rec.get("id") or rec.get("Actor", {}).get("ID", "")
        when = _event_time(rec)
        if not status or not cid or when is None:
            continue
        if status in _START:
            image = (rec.get("from")
                     or rec.get("Actor", {}).get("Attributes", {}).get("image", ""))
            open_windows[cid] = ContainerWindow(
                container_id=cid[:12], image=image, started_at=when)
        elif status in _END and cid in open_windows:
            w = open_windows.pop(cid)
            if w.ended_at is None:
                w.ended_at = when
            done.append(w)
    done.extend(open_windows.values())
    done.sort(key=lambda w: w.started_at)
    return done


def merge_windows(windows: list[ContainerWindow]) -> list[tuple[datetime, datetime]]:
    """Union of closed container windows, so overlapping runs are not
    double-counted when their energy is summed."""
    spans = sorted((w.started_at, w.ended_at) for w in windows
                   if w.ended_at is not None)
    merged: list[tuple[datetime, datetime]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
