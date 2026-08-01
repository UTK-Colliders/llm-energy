from pathlib import Path

import pytest

from llm_energy.session.parse import merge_usages, parse_session

FIXTURES = Path(__file__).parent / "fixtures"


def load_lines(name: str):
    return (FIXTURES / name).read_text().splitlines()


def test_basic_session():
    u = parse_session(load_lines("session_basic.jsonl"), session_id="abc")
    assert u.session_ids == ["abc"]
    assert u.cwd == "/Users/prof/llm-energy"
    assert u.user_turns == 2
    assert u.assistant_turns == 2
    assert u.sidechain_turns == 0
    assert u.wall_time_s == pytest.approx(120.0)  # 15:00:00 -> 15:02:00

    assert len(u.per_model) == 1
    m = u.per_model[0]
    assert m.model == "claude-fable-5"
    assert m.input_tokens == 110
    assert m.output_tokens == 250
    assert m.cache_creation_tokens == 1000
    assert m.cache_read_tokens == 5000
    assert m.requests == 2


def test_duplicate_message_ids_not_double_counted():
    u = parse_session(load_lines("session_multi_model_dupes.jsonl"))
    fable = next(m for m in u.per_model if m.model == "claude-fable-5")
    # msg_a appears 3 times with growing output_tokens; only the largest counts
    assert fable.output_tokens == 496 + 120
    assert fable.input_tokens == 2 + 4
    assert fable.cache_creation_tokens == 38074
    assert fable.cache_read_tokens == 40000
    assert fable.requests == 2


def test_sidechain_and_synthetic_and_malformed():
    u = parse_session(load_lines("session_multi_model_dupes.jsonl"))
    haiku = next(m for m in u.per_model if m.model.startswith("claude-haiku"))
    assert haiku.input_tokens == 500        # sidechain tokens ARE counted
    assert u.sidechain_turns == 1
    assert u.assistant_turns == 2           # msg_a + msg_b, not sidechain/synthetic
    assert any("synthetic" in n for n in u.notes)
    assert any("malformed" in n for n in u.notes)


def test_merge_usages():
    a = parse_session(load_lines("session_basic.jsonl"), session_id="a")
    b = parse_session(load_lines("session_multi_model_dupes.jsonl"), session_id="b")
    m = merge_usages([a, b])
    assert m.session_ids == ["a", "b"]
    assert m.user_turns == a.user_turns + b.user_turns
    assert m.wall_time_s == pytest.approx(a.wall_time_s + b.wall_time_s)
    fable = next(x for x in m.per_model if x.model == "claude-fable-5")
    assert fable.output_tokens == 250 + 616
    assert m.started_at == a.started_at     # earliest across sessions
    assert m.ended_at == b.ended_at


def test_empty_input():
    u = parse_session([])
    assert u.per_model == []
    assert u.wall_time_s == 0.0
