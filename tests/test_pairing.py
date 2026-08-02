"""Pairing a session-power run with the task run its agent invoked."""

import json

from llm_energy.pairing import find_task_results_for_sessions


def write_task(dir_, name, session_id, started_at="2026-08-01T10:00:00+00:00",
               kind="task-run"):
    p = dir_ / f"task-{name}.json"
    p.write_text(json.dumps({
        "kind": kind,
        "task_name": "madgraph-ttbar2j-lhe",
        "coordinating_session_id": session_id,
        "started_at": started_at,
    }))
    return p


def test_matches_only_the_stamped_session(tmp_path):
    mine = write_task(tmp_path, "mine", "sess-aaa")
    write_task(tmp_path, "theirs", "sess-bbb")
    write_task(tmp_path, "orphan", None)

    assert find_task_results_for_sessions(tmp_path, ["sess-aaa"]) == [mine]


def test_results_are_ordered_by_run_start(tmp_path):
    second = write_task(tmp_path, "b-second", "s", started_at="2026-08-01T12:00:00+00:00")
    first = write_task(tmp_path, "a-first", "s", started_at="2026-08-01T09:00:00+00:00")
    # filename order is a-first, b-second; started_at order agrees here, so use
    # names that would sort the other way to prove start time wins
    third = write_task(tmp_path, "a-third", "s", started_at="2026-08-01T15:00:00+00:00")

    assert find_task_results_for_sessions(tmp_path, ["s"]) == [first, second, third]


def test_several_sessions_can_be_matched_at_once(tmp_path):
    a = write_task(tmp_path, "a", "sess-a", started_at="2026-08-01T09:00:00+00:00")
    b = write_task(tmp_path, "b", "sess-b", started_at="2026-08-01T10:00:00+00:00")
    write_task(tmp_path, "c", "sess-c")

    assert find_task_results_for_sessions(tmp_path, ["sess-a", "sess-b"]) == [a, b]


def test_no_session_ids_matches_nothing(tmp_path):
    write_task(tmp_path, "a", "sess-a")
    assert find_task_results_for_sessions(tmp_path, []) == []
    # a task result with a null id must not be matched by a null lookup
    assert find_task_results_for_sessions(tmp_path, [""]) == []


def test_malformed_and_foreign_files_are_skipped(tmp_path):
    good = write_task(tmp_path, "good", "s")
    (tmp_path / "task-broken.json").write_text("{not json")
    (tmp_path / "task-empty.json").write_text("")
    (tmp_path / "task-list.json").write_text("[1, 2, 3]")
    # right session id, wrong kind of artifact
    write_task(tmp_path, "baseline-ish", "s", kind="baseline")

    assert find_task_results_for_sessions(tmp_path, ["s"]) == [good]
