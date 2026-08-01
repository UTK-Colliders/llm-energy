"""Locate Claude Code session transcripts.

Transcripts live at ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl where
the encoding replaces '/' and '.' with '-'. Because the encoding rule has
drifted across Claude Code versions, we also support scanning all project
dirs and reading the real cwd from the first records of each file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class SessionFileInfo:
    path: Path
    session_id: str
    mtime: datetime
    size_bytes: int
    cwd: str | None = None
    started_at: str | None = None


def projects_root() -> Path:
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(cfg) if cfg else Path.home() / ".claude"
    return base / "projects"


def current_session_id(env: dict[str, str] | None = None) -> str | None:
    """The Claude Code session that spawned this process, if any.

    Claude Code exports CLAUDE_CODE_SESSION_ID into every subprocess it runs,
    and the value is the transcript filename stem. So a command the agent
    invokes can identify the conversation driving it — no mtime guessing.
    """
    e = os.environ if env is None else env
    sid = (e.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    return sid or None


def encode_cwd(cwd: Path) -> str:
    return str(cwd).replace("/", "-").replace(".", "-")


def _peek_metadata(path: Path, max_lines: int = 25) -> tuple[str | None, str | None]:
    """Read (cwd, first timestamp) from the first records of a transcript."""
    cwd = None
    ts = None
    try:
        with path.open() as fh:
            for _ in range(max_lines):
                line = fh.readline()
                if not line:
                    break
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cwd is None and isinstance(rec.get("cwd"), str):
                    cwd = rec["cwd"]
                if ts is None and isinstance(rec.get("timestamp"), str):
                    ts = rec["timestamp"]
                if cwd and ts:
                    break
    except OSError:
        pass
    return cwd, ts


def find_sessions(cwd: Path | None = None,
                  since: datetime | None = None,
                  all_projects: bool = False,
                  root: Path | None = None) -> list[SessionFileInfo]:
    """List transcript files, newest first.

    With cwd and not all_projects: look in the encoded dir for that cwd, but
    fall back to a full scan filtered by the cwd recorded inside each file.
    """
    root = root or projects_root()
    if not root.exists():
        return []

    if cwd is not None and not all_projects:
        encoded_dir = root / encode_cwd(cwd)
        dirs = [encoded_dir] if encoded_dir.is_dir() else list(root.iterdir())
    else:
        dirs = list(root.iterdir())

    infos: list[SessionFileInfo] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for f in d.glob("*.jsonl"):
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if since and mtime < since:
                continue
            file_cwd, started = _peek_metadata(f)
            if cwd is not None and file_cwd is not None and Path(file_cwd) != cwd:
                continue
            infos.append(SessionFileInfo(
                path=f, session_id=f.stem, mtime=mtime,
                size_bytes=f.stat().st_size, cwd=file_cwd, started_at=started))
    infos.sort(key=lambda i: i.mtime, reverse=True)
    return infos


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def find_sessions_started_in_window(t_start: datetime, t_end: datetime,
                                    cwd: Path | None = None,
                                    root: Path | None = None) -> list[SessionFileInfo]:
    """Sessions whose transcript's first record falls inside [t_start, t_end].

    Attributes a wrapped `claude` run to the conversation(s) it created: the
    wrapper cannot know the child's session id up front (Claude Code mints it
    at startup), but a session that *began* during the measured window belongs
    to that window. Returned oldest first.
    """
    found = []
    for info in find_sessions(cwd=cwd, since=t_start, all_projects=cwd is None,
                              root=root):
        started = _parse_iso(info.started_at)
        if started is not None and t_start <= started <= t_end:
            found.append(info)
    found.sort(key=lambda i: i.started_at or "")
    return found


def find_session_by_id(session_id: str, root: Path | None = None) -> SessionFileInfo | None:
    """Find a session by full or prefix id across all project dirs."""
    root = root or projects_root()
    if not root.exists():
        return None
    matches = [f for f in root.glob("*/*.jsonl") if f.stem.startswith(session_id)]
    if not matches:
        return None
    f = max(matches, key=lambda p: p.stat().st_mtime)
    cwd, started = _peek_metadata(f)
    return SessionFileInfo(
        path=f, session_id=f.stem,
        mtime=datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc),
        size_bytes=f.stat().st_size, cwd=cwd, started_at=started)
