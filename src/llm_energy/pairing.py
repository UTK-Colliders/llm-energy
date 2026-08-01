"""Match result artifacts that belong to the same measured run.

A measured run leaves three files in `results/`: a session-power result (the
wrapper), a task result (what the agent ran), and a session-energy result (the
tokens). The task result carries `coordinating_session_id`, and the
session-power result carries the ids of sessions that started inside its
window — so the three can be tied together exactly, without matching on
timestamps or picking the newest file and hoping.
"""

from __future__ import annotations

import json
from pathlib import Path


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def find_task_results_for_sessions(results_dir: Path,
                                   session_ids: list[str]) -> list[Path]:
    """Task results coordinated by any of `session_ids`, oldest run first.

    Unreadable or malformed JSON in the directory is skipped rather than
    raising: `results/` accumulates files from many runs and one bad artifact
    should not break pairing for the rest.
    """
    wanted = {s for s in session_ids if s}
    if not wanted:
        return []
    matches: list[tuple[str, Path]] = []
    for p in sorted(results_dir.glob("task-*.json")):
        data = _read_json(p)
        if data is None or data.get("kind") != "task-run":
            continue
        if data.get("coordinating_session_id") in wanted:
            matches.append((data.get("started_at") or data.get("created_at") or "", p))
    matches.sort(key=lambda m: m[0])
    return [p for _, p in matches]
