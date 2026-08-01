"""Observing containers the agent started, from the Docker event stream."""

import json

from llm_energy.docker_events import parse_events, merge_windows

BASE = 1785000000


def ev(status, cid, image="img:1", t_offset=0):
    return json.dumps({
        "status": status, "Type": "container", "id": cid, "from": image,
        "timeNano": int((BASE + t_offset) * 1e9),
        "Actor": {"ID": cid, "Attributes": {"image": image}},
    })


def offsets(spans):
    return [(round(a.timestamp() - BASE), round(b.timestamp() - BASE))
            for a, b in spans]


def test_start_and_die_become_a_window():
    [w] = parse_events([ev("start", "a" * 64, "mg5:3.5.16", 10),
                        ev("die", "a" * 64, "mg5:3.5.16", 610)])
    assert w.container_id == "a" * 12
    assert w.image == "mg5:3.5.16"
    assert w.wall_time_s == 600.0


def test_lifecycle_noise_is_ignored():
    """create/attach/exec events must not open spurious windows."""
    ws = parse_events([ev("create", "a" * 64, t_offset=0),
                       ev("attach", "a" * 64, t_offset=1),
                       ev("start", "a" * 64, t_offset=2),
                       ev("exec_start", "a" * 64, t_offset=3),
                       ev("die", "a" * 64, t_offset=8)])
    assert len(ws) == 1 and ws[0].wall_time_s == 6.0


def test_malformed_lines_are_skipped():
    ws = parse_events(["", "not json", "[1,2]",
                       ev("start", "a" * 64, t_offset=0),
                       ev("die", "a" * 64, t_offset=5)])
    assert len(ws) == 1


def test_container_still_running_has_no_end():
    [w] = parse_events([ev("start", "a" * 64, t_offset=0)])
    assert w.ended_at is None
    assert w.wall_time_s == 0.0


def test_windows_are_ordered_oldest_first():
    ws = parse_events([ev("start", "b" * 64, t_offset=100),
                       ev("die", "b" * 64, t_offset=200),
                       ev("start", "a" * 64, t_offset=0),
                       ev("die", "a" * 64, t_offset=50)])
    assert [w.container_id for w in ws] == ["a" * 12, "b" * 12]


def test_disjoint_windows_stay_separate():
    ws = parse_events([ev("start", "a" * 64, t_offset=0),
                       ev("die", "a" * 64, t_offset=10),
                       ev("start", "b" * 64, t_offset=100),
                       ev("die", "b" * 64, t_offset=130)])
    assert offsets(merge_windows(ws)) == [(0, 10), (100, 130)]


def test_overlapping_containers_merge_so_energy_is_counted_once():
    ws = parse_events([ev("start", "a" * 64, t_offset=0),
                       ev("start", "b" * 64, t_offset=10),
                       ev("die", "a" * 64, t_offset=30),
                       ev("die", "b" * 64, t_offset=50)])
    assert offsets(merge_windows(ws)) == [(0, 50)]


def test_unfinished_containers_are_excluded_from_the_union():
    ws = parse_events([ev("start", "a" * 64, t_offset=0),
                       ev("die", "a" * 64, t_offset=10),
                       ev("start", "b" * 64, t_offset=20)])
    assert offsets(merge_windows(ws)) == [(0, 10)]


def test_kill_and_stop_also_close_a_window():
    for closing in ("kill", "stop"):
        [w] = parse_events([ev("start", "a" * 64, t_offset=0),
                            ev(closing, "a" * 64, t_offset=7)])
        assert w.wall_time_s == 7.0, closing


def test_second_close_event_does_not_extend_the_window():
    """docker emits kill then die; the first close is the real end."""
    ws = parse_events([ev("start", "a" * 64, t_offset=0),
                       ev("kill", "a" * 64, t_offset=5),
                       ev("die", "a" * 64, t_offset=9)])
    assert len(ws) == 1 and ws[0].wall_time_s == 5.0


def test_seconds_only_timestamps_are_accepted():
    """Older daemons emit `time` without `timeNano`."""
    def plain(status, t):
        return json.dumps({"status": status, "id": "a" * 64, "time": BASE + t,
                           "from": "i", "Actor": {"ID": "a" * 64}})
    [w] = parse_events([plain("start", 0), plain("die", 12)])
    assert w.wall_time_s == 12.0
