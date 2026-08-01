"""Identifying the coordinating session: env var, and start-time windows."""

import json
from datetime import datetime, timedelta, timezone

from llm_energy.session.locate import (current_session_id,
                                       find_sessions_started_in_window)

T0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def write_transcript(root, session_id, cwd, started_at):
    d = root / str(cwd).replace("/", "-").replace(".", "-")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session_id}.jsonl"
    p.write_text(json.dumps({
        "type": "user", "cwd": str(cwd),
        "timestamp": started_at.isoformat().replace("+00:00", "Z"),
    }) + "\n")
    return p


def test_current_session_id_reads_the_env_var():
    assert current_session_id({"CLAUDE_CODE_SESSION_ID": "abc-123"}) == "abc-123"


def test_current_session_id_absent_or_blank():
    assert current_session_id({}) is None
    assert current_session_id({"CLAUDE_CODE_SESSION_ID": "   "}) is None


def test_window_includes_sessions_that_began_inside_it(tmp_path):
    root = tmp_path / "projects"
    cwd = tmp_path / "work"
    inside = write_transcript(root, "inside", cwd, T0 + timedelta(minutes=1))
    write_transcript(root, "before", cwd, T0 - timedelta(minutes=5))
    write_transcript(root, "after", cwd, T0 + timedelta(minutes=30))

    found = find_sessions_started_in_window(T0, T0 + timedelta(minutes=10),
                                            cwd=cwd, root=root)
    assert [f.path for f in found] == [inside]


def test_window_boundaries_are_inclusive(tmp_path):
    root = tmp_path / "projects"
    cwd = tmp_path / "work"
    write_transcript(root, "at-start", cwd, T0)
    write_transcript(root, "at-end", cwd, T0 + timedelta(minutes=10))

    found = find_sessions_started_in_window(T0, T0 + timedelta(minutes=10),
                                            cwd=cwd, root=root)
    assert {f.session_id for f in found} == {"at-start", "at-end"}


def test_results_are_oldest_first(tmp_path):
    root = tmp_path / "projects"
    cwd = tmp_path / "work"
    write_transcript(root, "later", cwd, T0 + timedelta(minutes=8))
    write_transcript(root, "earlier", cwd, T0 + timedelta(minutes=2))

    found = find_sessions_started_in_window(T0, T0 + timedelta(minutes=10),
                                            cwd=cwd, root=root)
    assert [f.session_id for f in found] == ["earlier", "later"]


def test_other_working_directories_are_excluded(tmp_path):
    root = tmp_path / "projects"
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    write_transcript(root, "mine-1", mine, T0 + timedelta(minutes=1))
    write_transcript(root, "theirs-1", theirs, T0 + timedelta(minutes=1))

    found = find_sessions_started_in_window(T0, T0 + timedelta(minutes=10),
                                            cwd=mine, root=root)
    assert [f.session_id for f in found] == ["mine-1"]


def test_missing_projects_root_is_not_an_error(tmp_path):
    assert find_sessions_started_in_window(
        T0, T0 + timedelta(minutes=10), root=tmp_path / "nope") == []
