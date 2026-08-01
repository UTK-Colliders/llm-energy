"""Parse Claude Code transcript JSONL into token usage.

Critical detail verified against real transcripts: a single API response
(message.id) appears as MULTIPLE assistant records — one per streamed content
block — each carrying the same usage object. Summing naively overcounts
tokens by 2-3x. We dedupe by message.id, keeping the record with the largest
output_tokens (the final/most complete write).

Sidechain records (isSidechain: true, i.e. subagents) are included in token
totals — subagent inference costs energy too — but counted separately as
sidechain_turns.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Iterable

from llm_energy.schemas import ModelUsage, SessionUsage

SYNTHETIC_MODELS = {"<synthetic>"}


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def parse_session(lines: Iterable[str], session_id: str = "") -> SessionUsage:
    usage = SessionUsage(session_ids=[session_id] if session_id else [])
    # message.id -> (record dict, is_sidechain)
    best_by_msg_id: dict[str, tuple[dict, bool]] = {}
    anon_records: list[tuple[dict, bool]] = []  # assistant records without an id
    timestamps: list[datetime] = []
    user_turns = 0
    skipped_synthetic = 0
    malformed = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(rec, dict):
            malformed += 1
            continue

        ts = _parse_ts(rec.get("timestamp", ""))
        if ts:
            timestamps.append(ts)
        if usage.cwd is None and isinstance(rec.get("cwd"), str):
            usage.cwd = rec["cwd"]

        rtype = rec.get("type")
        if rtype == "user":
            if not rec.get("isSidechain"):
                user_turns += 1
            continue
        if rtype != "assistant":
            continue

        msg = rec.get("message") or {}
        if not isinstance(msg, dict):
            continue
        model = msg.get("model")
        if model in SYNTHETIC_MODELS:
            skipped_synthetic += 1
            continue
        mu = msg.get("usage")
        if not isinstance(mu, dict):
            continue

        is_sidechain = bool(rec.get("isSidechain"))
        entry = ({"model": model or "unknown", "usage": mu}, is_sidechain)
        msg_id = msg.get("id")
        if msg_id:
            prev = best_by_msg_id.get(msg_id)
            if prev is None or (mu.get("output_tokens") or 0) > (
                    prev[0]["usage"].get("output_tokens") or 0):
                best_by_msg_id[msg_id] = entry
        else:
            anon_records.append(entry)

    per_model: dict[str, ModelUsage] = {}
    assistant_turns = 0
    sidechain_turns = 0
    for entry, is_sidechain in list(best_by_msg_id.values()) + anon_records:
        model = entry["model"]
        mu = entry["usage"]
        m = per_model.setdefault(model, ModelUsage(model=model))
        m.input_tokens += mu.get("input_tokens") or 0
        m.output_tokens += mu.get("output_tokens") or 0
        m.cache_creation_tokens += mu.get("cache_creation_input_tokens") or 0
        m.cache_read_tokens += mu.get("cache_read_input_tokens") or 0
        m.requests += 1
        if is_sidechain:
            sidechain_turns += 1
        else:
            assistant_turns += 1

    usage.per_model = sorted(per_model.values(), key=lambda m: m.model)
    usage.assistant_turns = assistant_turns
    usage.sidechain_turns = sidechain_turns
    usage.user_turns = user_turns
    if timestamps:
        start, end = min(timestamps), max(timestamps)
        usage.started_at = start.isoformat()
        usage.ended_at = end.isoformat()
        usage.wall_time_s = (end - start).total_seconds()
    if skipped_synthetic:
        usage.notes.append(f"skipped {skipped_synthetic} synthetic-model record(s)")
    if malformed:
        usage.notes.append(f"skipped {malformed} malformed line(s)")
    return usage


def merge_usages(usages: list[SessionUsage]) -> SessionUsage:
    """Combine several sessions into one coordination episode: tokens and
    wall times are summed; ids and notes concatenated."""
    if not usages:
        return SessionUsage()
    merged = SessionUsage()
    per_model: dict[str, ModelUsage] = {}
    for u in usages:
        merged.session_ids.extend(u.session_ids)
        merged.assistant_turns += u.assistant_turns
        merged.sidechain_turns += u.sidechain_turns
        merged.user_turns += u.user_turns
        merged.wall_time_s += u.wall_time_s
        merged.notes.extend(u.notes)
        if merged.cwd is None:
            merged.cwd = u.cwd
        for m in u.per_model:
            t = per_model.setdefault(m.model, ModelUsage(model=m.model))
            t.input_tokens += m.input_tokens
            t.output_tokens += m.output_tokens
            t.cache_creation_tokens += m.cache_creation_tokens
            t.cache_read_tokens += m.cache_read_tokens
            t.requests += m.requests
    merged.per_model = sorted(per_model.values(), key=lambda m: m.model)
    starts = [u.started_at for u in usages if u.started_at]
    ends = [u.ended_at for u in usages if u.ended_at]
    merged.started_at = min(starts) if starts else ""
    merged.ended_at = max(ends) if ends else ""
    return merged
