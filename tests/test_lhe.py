import gzip
from pathlib import Path

import pytest

from llm_energy.lhe import compare_lhe, event_summary, iter_events

HEADER = """<LesHouchesEvents version="3.0">
<header>
<!-- generated on host-a at 2026-08-01 -->
<MGVersion>3.5.7</MGVersion>
</header>
<init>
2212 2212 6.8e3 6.8e3 0 0 247000 247000 -4 1
8.0e2 1.2e0 1.0e0 1
</init>
"""

EVENT_1 = """<event>
 6      1 +8.0e+02 1.72e+02 7.54e-03 1.18e-01
       21 -1    0    0  501  502 +0.0e+00 +0.0e+00 +5.6e+02 5.6e+02 0.0e+00 0. 1.
        6  1    1    2  501  503 +1.2e+02 -3.4e+01 +2.1e+02 3.2e+02 1.73e+02 0. 1.
</event>
"""

EVENT_1_REORDERED_WS = """<event>
 6      1 +8.0e+02   1.72e+02 7.54e-03 1.18e-01
       21 -1    0    0  501  502 +0.0e+00 +0.0e+00 +5.6e+02 5.6e+02 0.0e+00 0. 1.
        6  1    1    2  501  503 +1.2e+02 -3.4e+01 +2.1e+02 3.2e+02 1.73e+02 0. 1.
</event>
"""

EVENT_1_ULP = EVENT_1.replace("1.72e+02", "1.7200000001e+02")
EVENT_2 = EVENT_1.replace("+1.2e+02", "+9.9e+01")

FOOTER = "</LesHouchesEvents>\n"


def write_lhe(path: Path, events: list[str], header_note: str = "host-a") -> Path:
    text = HEADER.replace("host-a", header_note) + "".join(events) + FOOTER
    if str(path).endswith(".gz"):
        with gzip.open(path, "wt") as fh:
            fh.write(text)
    else:
        path.write_text(text)
    return path


def test_iter_events_and_summary(tmp_path):
    f = write_lhe(tmp_path / "a.lhe", [EVENT_1, EVENT_2])
    events = list(iter_events(f))
    assert len(events) == 2
    assert events[0][0].startswith("6 1")  # whitespace collapsed
    n, h = event_summary(f)
    assert n == 2
    assert len(h) == 64


def test_gzip_transparent(tmp_path):
    a = write_lhe(tmp_path / "a.lhe", [EVENT_1])
    b = write_lhe(tmp_path / "b.lhe.gz", [EVENT_1])
    assert event_summary(a) == event_summary(b)


def test_header_differences_ignored(tmp_path):
    a = write_lhe(tmp_path / "a.lhe", [EVENT_1], header_note="host-a")
    b = write_lhe(tmp_path / "b.lhe", [EVENT_1], header_note="host-b 2026-09-99")
    comp = compare_lhe([a, b])
    assert comp.identical


def test_whitespace_normalization(tmp_path):
    a = write_lhe(tmp_path / "a.lhe", [EVENT_1])
    b = write_lhe(tmp_path / "b.lhe", [EVENT_1_REORDERED_WS])
    assert compare_lhe([a, b]).identical


def test_different_events_detected(tmp_path):
    a = write_lhe(tmp_path / "a.lhe", [EVENT_1])
    b = write_lhe(tmp_path / "b.lhe", [EVENT_2])
    comp = compare_lhe([a, b])
    assert not comp.identical
    assert comp.numerically_equal is False
    assert "event 0" in comp.first_difference


def test_ulp_difference_numerically_equal(tmp_path):
    a = write_lhe(tmp_path / "a.lhe", [EVENT_1])
    b = write_lhe(tmp_path / "b.lhe", [EVENT_1_ULP])
    comp = compare_lhe([a, b], rtol=1e-6)
    assert not comp.identical            # hashes differ
    assert comp.numerically_equal        # but physics agrees within rtol
    comp_strict = compare_lhe([a, b], rtol=1e-15)
    assert comp_strict.numerically_equal is False


def test_event_count_mismatch(tmp_path):
    a = write_lhe(tmp_path / "a.lhe", [EVENT_1, EVENT_2])
    b = write_lhe(tmp_path / "b.lhe", [EVENT_1])
    comp = compare_lhe([a, b])
    assert not comp.identical
    assert "event counts differ" in comp.first_difference


def test_three_way_comparison(tmp_path):
    a = write_lhe(tmp_path / "a.lhe", [EVENT_1])
    b = write_lhe(tmp_path / "b.lhe", [EVENT_1])
    c = write_lhe(tmp_path / "c.lhe", [EVENT_1])
    assert compare_lhe([a, b, c]).identical
