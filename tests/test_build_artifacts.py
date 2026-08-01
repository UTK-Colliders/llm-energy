"""Detecting compiler output written inside a measured phase window."""

import os
from datetime import datetime, timedelta, timezone

from llm_energy.build_artifacts import count_build_artifacts

T0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def touch(path, when):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    ts = when.timestamp()
    os.utime(path, (ts, ts))
    return path


def test_counts_compiler_output_inside_the_window(tmp_path):
    touch(tmp_path / "SubProcesses/P1/matrix.o", T0 + timedelta(seconds=30))
    touch(tmp_path / "lib/libmodel.a", T0 + timedelta(seconds=40))
    touch(tmp_path / "Source/mod_file.mod", T0 + timedelta(seconds=50))

    got = count_build_artifacts(tmp_path, T0, T0 + timedelta(minutes=1))
    assert got.count == 3
    assert got.any


def test_ignores_output_written_outside_the_window(tmp_path):
    touch(tmp_path / "old.o", T0 - timedelta(minutes=5))
    touch(tmp_path / "later.o", T0 + timedelta(minutes=5))

    got = count_build_artifacts(tmp_path, T0, T0 + timedelta(minutes=1))
    assert got.count == 0
    assert not got.any


def test_ignores_non_build_files(tmp_path):
    """Event generation writes plenty of files; none of them are compiler output."""
    for name in ("unweighted_events.lhe", "run_01.log", "results.dat",
                 "banner.txt", "madevent"):
        touch(tmp_path / name, T0 + timedelta(seconds=10))

    assert count_build_artifacts(tmp_path, T0, T0 + timedelta(minutes=1)).count == 0


def test_examples_are_relative_and_capped(tmp_path):
    for i in range(9):
        touch(tmp_path / f"deep/dir/obj{i}.o", T0 + timedelta(seconds=i))

    got = count_build_artifacts(tmp_path, T0, T0 + timedelta(minutes=1),
                                max_examples=3)
    assert got.count == 9
    assert len(got.examples) == 3
    assert all(e.startswith("deep/dir/obj") for e in got.examples)


def test_boundaries_are_inclusive(tmp_path):
    touch(tmp_path / "start.o", T0)
    touch(tmp_path / "end.o", T0 + timedelta(minutes=1))
    assert count_build_artifacts(tmp_path, T0, T0 + timedelta(minutes=1)).count == 2


def test_directories_named_like_artifacts_are_skipped(tmp_path):
    (tmp_path / "weird.o").mkdir()
    assert count_build_artifacts(tmp_path, T0 - timedelta(days=1),
                                 T0 + timedelta(days=1)).count == 0


def test_missing_root_is_not_an_error(tmp_path):
    got = count_build_artifacts(tmp_path / "nope", T0, T0 + timedelta(minutes=1))
    assert got.count == 0 and got.examples == []
