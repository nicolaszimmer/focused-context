"""The turn index: one SQLite database per Claude Code project.

The transcript stays the record. This is an index beside it, rebuilt from it
at will, and pruned on the same schedule Claude Code deletes transcripts.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .transcript import Transcript, parse

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    transcript_path TEXT,
    first_at TEXT,
    last_at TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_key TEXT NOT NULL,
    seq INTEGER NOT NULL,
    origin TEXT NOT NULL,
    parent_key TEXT,
    started_at TEXT,
    prompt TEXT NOT NULL,
    answer TEXT,
    answer_source TEXT,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    files TEXT NOT NULL DEFAULT '[]',
    commits TEXT NOT NULL DEFAULT '[]',
    pull_requests TEXT NOT NULL DEFAULT '[]',
    UNIQUE (session_id, turn_key)
);
CREATE INDEX IF NOT EXISTS turns_by_session ON turns (session_id, seq);
CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
    prompt, answer, content='turns', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
    INSERT INTO turns_fts (rowid, prompt, answer) VALUES (new.id, new.prompt, new.answer);
END;
CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
    INSERT INTO turns_fts (turns_fts, rowid, prompt, answer) VALUES ('delete', old.id, old.prompt, old.answer);
END;
CREATE TRIGGER IF NOT EXISTS turns_au AFTER UPDATE ON turns BEGIN
    INSERT INTO turns_fts (turns_fts, rowid, prompt, answer) VALUES ('delete', old.id, old.prompt, old.answer);
    INSERT INTO turns_fts (rowid, prompt, answer) VALUES (new.id, new.prompt, new.answer);
END;
"""

# A turn's answer from the Stop hook is the final text Claude Code reports.
# A later re-read of the transcript never replaces it.
UPSERT = """
INSERT INTO turns (session_id, turn_key, seq, origin, parent_key, started_at, prompt,
                   answer, answer_source, tool_calls, files, commits, pull_requests)
VALUES (:session_id, :turn_key, :seq, :origin, :parent_key, :started_at, :prompt,
        :answer, :answer_source, :tool_calls, :files, :commits, :pull_requests)
ON CONFLICT (session_id, turn_key) DO UPDATE SET
    seq = excluded.seq,
    origin = excluded.origin,
    parent_key = excluded.parent_key,
    started_at = excluded.started_at,
    prompt = excluded.prompt,
    answer = CASE WHEN turns.answer_source = 'stop-hook' THEN turns.answer ELSE excluded.answer END,
    answer_source = CASE WHEN turns.answer_source = 'stop-hook' THEN turns.answer_source ELSE excluded.answer_source END,
    tool_calls = excluded.tool_calls,
    files = excluded.files,
    commits = excluded.commits,
    pull_requests = excluded.pull_requests
WHERE turns.prompt IS NOT excluded.prompt
   OR turns.answer IS NOT excluded.answer
   OR turns.tool_calls IS NOT excluded.tool_calls
   OR turns.files IS NOT excluded.files
   OR turns.commits IS NOT excluded.commits
   OR turns.pull_requests IS NOT excluded.pull_requests
   OR turns.seq IS NOT excluded.seq
   OR turns.parent_key IS NOT excluded.parent_key
"""


def home() -> Path:
    return Path(os.environ.get("FOCUSED_HOME", "~/.claude/focused")).expanduser()


def slug_for_cwd(cwd: str) -> str:
    """Claude Code's name for a project's folder under ~/.claude/projects."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def db_for_transcript(transcript_path) -> Path:
    return home() / f"{Path(transcript_path).parent.name}.sqlite"


def db_for_cwd(cwd: str) -> Path:
    return home() / f"{slug_for_cwd(cwd)}.sqlite"


def retention_days() -> int:
    """FOCUSED_RETENTION_DAYS, else Claude Code's cleanupPeriodDays, else 30."""
    value = os.environ.get("FOCUSED_RETENTION_DAYS")
    if value is None:
        try:
            settings = json.loads((Path.home() / ".claude" / "settings.json").read_text())
            value = settings.get("cleanupPeriodDays")
        except (OSError, ValueError, AttributeError):
            value = None
    try:
        return int(value) if value is not None else 30
    except (TypeError, ValueError):
        return 30


def connect(path) -> sqlite3.Connection:
    """Open the index. The database and its -wal and -shm files are owner-only:
    prompts and answers can hold secrets."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    old = os.umask(0o077)  # SQLite then gives -wal and -shm the database's own 0600
    try:
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
    finally:
        os.umask(old)
    for suffix in ("", "-wal", "-shm"):  # also tightens files made before this rule
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            os.chmod(sidecar, 0o600)
    return conn


def index_transcript(conn: sqlite3.Connection, path, stop_message: Optional[str] = None,
                     now: Optional[datetime] = None) -> Transcript:
    """Upsert every turn of a transcript. Running it twice changes nothing."""
    parsed = parse(path)
    session_id = parsed.session_id or Path(path).stem
    with conn:
        for turn in parsed.turns:
            conn.execute(UPSERT, {
                "session_id": session_id,
                "turn_key": turn.key,
                "seq": turn.seq,
                "origin": turn.origin,
                "parent_key": turn.parent_key,
                "started_at": turn.started_at,
                "prompt": turn.prompt,
                "answer": turn.answer,
                "answer_source": "transcript" if turn.answer is not None else None,
                "tool_calls": turn.tool_calls,
                "files": json.dumps(turn.files),
                "commits": json.dumps(turn.commits),
                "pull_requests": json.dumps(turn.pull_requests),
            })
        if stop_message and parsed.turns:
            # The transcript may not hold the final message yet when Stop fires.
            conn.execute(
                "UPDATE turns SET answer = ?, answer_source = 'stop-hook' "
                "WHERE session_id = ? AND turn_key = ? AND answer IS NOT ?",
                (stop_message, session_id, parsed.turns[-1].key, stop_message))
        stamps = [t.started_at for t in parsed.turns if t.started_at]
        conn.execute(
            "INSERT INTO sessions (session_id, transcript_path, first_at, last_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (session_id) DO UPDATE SET transcript_path = excluded.transcript_path, "
            "first_at = excluded.first_at, last_at = excluded.last_at",
            (session_id, str(path), min(stamps, default=None), max(stamps, default=None)))
        prune(conn, now=now)
    return parsed


def prune(conn: sqlite3.Connection, now: Optional[datetime] = None) -> int:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days())
    stamp = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    removed = conn.execute("DELETE FROM turns WHERE started_at < ?", (stamp,)).rowcount
    conn.execute("DELETE FROM sessions WHERE session_id NOT IN (SELECT DISTINCT session_id FROM turns)")
    return removed


def latest_session(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute("SELECT session_id FROM sessions ORDER BY last_at DESC LIMIT 1").fetchone()
    return row["session_id"] if row else None


def session_turns(conn: sqlite3.Connection, session_id: str) -> list:
    return conn.execute("SELECT * FROM turns WHERE session_id = ? ORDER BY seq", (session_id,)).fetchall()


def search(conn: sqlite3.Connection, words: list, limit: int = 10) -> list:
    """Full-text search over prompts and answers. Words are matched as given, not as FTS syntax."""
    query = " ".join('"' + w.replace('"', '""') + '"' for w in words if w.strip())
    if not query:
        return []
    return conn.execute(
        "SELECT t.*, snippet(turns_fts, -1, '[', ']', ' … ', 12) AS hit "
        "FROM turns_fts JOIN turns t ON t.id = turns_fts.rowid "
        "WHERE turns_fts MATCH ? ORDER BY rank LIMIT ?", (query, limit)).fetchall()
