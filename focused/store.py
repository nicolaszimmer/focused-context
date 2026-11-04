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

from . import jev, repos
from .parts import message_parts, paragraphs, place
from .transcript import LISTED, Transcript, is_own_command, parse

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    transcript_path TEXT,
    first_at TEXT,
    last_at TEXT,
    title TEXT
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

-- Each part of a person's message (parts.message_parts), with Jev's kinds once asked.
CREATE TABLE IF NOT EXISTS parts (
    session_id TEXT NOT NULL,
    turn_key TEXT NOT NULL,
    n INTEGER NOT NULL,
    text TEXT NOT NULL,
    queued INTEGER NOT NULL DEFAULT 0,
    kinds TEXT,
    wording TEXT,
    PRIMARY KEY (session_id, turn_key, n)
);
-- Each paragraph Claude wrote in a person's turn or in a wake-up filed under it.
CREATE TABLE IF NOT EXISTS paras (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    para_key TEXT NOT NULL,
    turn_key TEXT NOT NULL,
    source_key TEXT NOT NULL,
    seq INTEGER NOT NULL,
    place TEXT NOT NULL,
    started_at TEXT,
    text TEXT NOT NULL,
    UNIQUE (session_id, para_key)
);
CREATE INDEX IF NOT EXISTS paras_by_turn ON paras (session_id, turn_key, seq);
-- Jev's answer to "does this paragraph answer this part?".
CREATE TABLE IF NOT EXISTS pairs (
    session_id TEXT NOT NULL,
    turn_key TEXT NOT NULL,
    n INTEGER NOT NULL,
    para_key TEXT NOT NULL,
    answers REAL NOT NULL,
    partly REAL NOT NULL,
    wording TEXT NOT NULL,
    PRIMARY KEY (session_id, turn_key, n, para_key)
);
CREATE VIRTUAL TABLE IF NOT EXISTS paras_fts USING fts5(text, content='paras', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS paras_ai AFTER INSERT ON paras BEGIN
    INSERT INTO paras_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS paras_ad AFTER DELETE ON paras BEGIN
    INSERT INTO paras_fts (paras_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS paras_au AFTER UPDATE ON paras BEGIN
    INSERT INTO paras_fts (paras_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO paras_fts (rowid, text) VALUES (new.id, new.text);
END;
-- The repositories a session worked in (focused/repos.py), by its transcript entries in
-- each; one it started with but didn't work in has 0.
CREATE TABLE IF NOT EXISTS session_repos (
    session_id TEXT NOT NULL,
    repo TEXT NOT NULL,
    entries INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, repo)
);
-- What the hosted index holds (focused/remote.py): each turn's digest as last pushed.
CREATE TABLE IF NOT EXISTS pushed (
    session_id TEXT NOT NULL,
    turn_key TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (session_id, turn_key)
);
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
   OR turns.origin IS NOT excluded.origin
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
        if "title" not in {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}:
            conn.execute("ALTER TABLE sessions ADD COLUMN title TEXT")  # an index made before 0.6.0
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
    # Before the transaction: finding the repositories runs git, which mustn't hold the
    # index's write lock.
    worked_in = repos.of_session(parsed.places, parsed.start)
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
        if parsed.turns:
            # Rows for turns this read no longer finds (a rule changed how turns split) go.
            # A read that finds no turns at all deletes nothing: it is more likely a
            # transcript that could not be read than a session with no questions.
            kept = {t.key for t in parsed.turns}
            stored = [row[0] for row in conn.execute(
                "SELECT turn_key FROM turns WHERE session_id = ?", (session_id,))]
            conn.executemany("DELETE FROM turns WHERE session_id = ? AND turn_key = ?",
                             [(session_id, key) for key in stored if key not in kept])
        wanted = (stop_message or "").strip()
        if wanted and parsed.turns and not any((t.answer or "").strip() == wanted for t in parsed.turns):
            # The transcript may not hold the final message yet when Stop fires: then the
            # message is the latest turn's answer. If a turn's answer already is the message,
            # the transcript has caught up, or the hook (it runs async) ran after the next
            # turn began. Either way there is nothing to add. When an earlier turn merely
            # has the same answer, this writes nothing either, and the next read fills the
            # latest turn in: a missing answer is the cheap mistake, a wrong one would stick.
            conn.execute(
                "UPDATE turns SET answer = ?, answer_source = 'stop-hook' "
                "WHERE session_id = ? AND turn_key = ? AND answer IS NOT ?",
                (stop_message, session_id, parsed.turns[-1].key, stop_message))
        if parsed.turns:
            _index_parts(conn, session_id, parsed)
        stamps = [t.started_at for t in parsed.turns if t.started_at]
        conn.execute(
            "INSERT INTO sessions (session_id, transcript_path, first_at, last_at, title) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (session_id) DO UPDATE SET transcript_path = excluded.transcript_path, "
            "first_at = excluded.first_at, last_at = excluded.last_at, "
            "title = COALESCE(excluded.title, sessions.title)",
            (session_id, str(path), min(stamps, default=None), max(stamps, default=None), parsed.title))
        # A count only grows: a later read in a container where a repository's folder is
        # gone finds fewer of its entries, not fewer entries.
        conn.executemany(
            "INSERT INTO session_repos (session_id, repo, entries) VALUES (?, ?, ?) "
            "ON CONFLICT (session_id, repo) DO UPDATE SET entries = MAX(session_repos.entries, excluded.entries)",
            [(session_id, repo, entries) for repo, entries in worked_in])
        prune(conn, now=now)
    return parsed


def _index_parts(conn: sqlite3.Connection, session_id: str, parsed: Transcript) -> None:
    """Parts of each person's message, and the paragraphs of its turn and its wake-ups.
    A part or paragraph whose text changed loses Jev's answers about it; a new part
    means the message changed, so the turn's kinds are asked again."""
    under: dict = {}
    for turn in parsed.turns:
        if turn.parent_key:
            under.setdefault(turn.parent_key, []).append(turn)
    # A turn that isn't a person's message (a rule changed how it is read, as for a
    # compaction's summary in 0.8.1) keeps no parts, answers or paragraphs of its own: its
    # paragraphs are filed under the message it follows.
    unlisted = [(session_id, t.key) for t in parsed.turns if t.origin not in LISTED or is_own_command(t.prompt)]
    for table in ("pairs", "parts", "paras"):
        conn.executemany(f"DELETE FROM {table} WHERE session_id = ? AND turn_key = ?", unlisted)
    for turn in parsed.turns:
        if turn.origin not in LISTED or is_own_command(turn.prompt):
            continue
        parts = message_parts(turn.prompt)
        stored = {row[0]: row[1] for row in conn.execute(
            "SELECT n, text FROM parts WHERE session_id = ? AND turn_key = ?", (session_id, turn.key))}
        if [stored.get(n) for n in range(1, len(parts) + 1)] != [text for text, _ in parts] or len(stored) != len(parts):
            changed = [n for n, (text, _) in enumerate(parts, 1) if stored.get(n) != text]
            changed += [n for n in stored if n > len(parts)]
            conn.executemany("DELETE FROM pairs WHERE session_id = ? AND turn_key = ? AND n = ?",
                             [(session_id, turn.key, n) for n in changed])
            conn.execute("DELETE FROM parts WHERE session_id = ? AND turn_key = ?", (session_id, turn.key))
            conn.executemany("INSERT INTO parts (session_id, turn_key, n, text, queued) VALUES (?, ?, ?, ?, ?)",
                             [(session_id, turn.key, n, text, int(queued)) for n, (text, queued) in enumerate(parts, 1)])
        seen, seq = set(), 0
        for source in [turn] + under.get(turn.key, []):
            where = place(source.prompt, source is turn)
            for i, reply in enumerate(source.replies):
                for m, text in enumerate(paragraphs(reply), 1):
                    seq += 1
                    key = f"{source.key}:{i}.{m}"
                    seen.add(key)
                    row = conn.execute("SELECT text, seq, place FROM paras WHERE session_id = ? AND para_key = ?",
                                       (session_id, key)).fetchone()
                    if row is None:
                        conn.execute(
                            "INSERT INTO paras (session_id, para_key, turn_key, source_key, seq, place, started_at, text) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (session_id, key, turn.key, source.key, seq, where, source.started_at, text))
                    elif tuple(row) != (text, seq, where):
                        if row[0] != text:  # a different paragraph now: Jev's answers about it no longer hold
                            conn.execute("DELETE FROM pairs WHERE session_id = ? AND para_key = ?", (session_id, key))
                        conn.execute("UPDATE paras SET text = ?, seq = ?, place = ? WHERE session_id = ? AND para_key = ?",
                                     (text, seq, where, session_id, key))
        stale = [row[0] for row in conn.execute(
            "SELECT para_key FROM paras WHERE session_id = ? AND turn_key = ?", (session_id, turn.key)) if row[0] not in seen]
        conn.executemany("DELETE FROM paras WHERE session_id = ? AND para_key = ?", [(session_id, k) for k in stale])
        conn.executemany("DELETE FROM pairs WHERE session_id = ? AND para_key = ?", [(session_id, k) for k in stale])
    _drop_orphans(conn)


def _drop_orphans(conn: sqlite3.Connection) -> None:
    """Parts, paragraphs and Jev's answers go with their turn."""
    for table in ("parts", "paras", "pairs"):
        conn.execute(f"DELETE FROM {table} WHERE NOT EXISTS (SELECT 1 FROM turns t WHERE t.session_id = "
                     f"{table}.session_id AND t.turn_key = {table}.turn_key)")


def prune(conn: sqlite3.Connection, now: Optional[datetime] = None) -> int:
    """Delete turns older than the retention period, with what was built from them: their
    parts, paragraphs and Jev's answers, and the saved summary of a session left with no
    turns (summaries/<session>.md, and its note in idle/). A summary or note not rewritten
    within the period goes too, even if its project's index is never opened again."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days())
    stamp = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    # Retention is each side's own: a turn deleted for its age isn't named gone in the
    # next push (remote.py), so the hosted index keeps it for as long as it keeps turns.
    conn.execute("DELETE FROM pushed WHERE EXISTS (SELECT 1 FROM turns t WHERE t.session_id = pushed.session_id "
                 "AND t.turn_key = pushed.turn_key AND t.started_at < ?)", (stamp,))
    removed = conn.execute("DELETE FROM turns WHERE started_at < ?", (stamp,)).rowcount
    gone = [row[0] for row in conn.execute(
        "SELECT session_id FROM sessions WHERE session_id NOT IN (SELECT DISTINCT session_id FROM turns)")]
    conn.execute("DELETE FROM sessions WHERE session_id NOT IN (SELECT DISTINCT session_id FROM turns)")
    conn.execute("DELETE FROM session_repos WHERE session_id NOT IN (SELECT session_id FROM sessions)")
    if removed:
        _drop_orphans(conn)
    _prune_files(gone, cutoff.timestamp())
    return removed


SAVED = (("summaries", ".md"), ("idle", ""), ("cleared", ""))  # files kept per session beside the index


def _prune_files(sessions: list, cutoff: float) -> None:
    for folder, suffix in SAVED:
        directory = home() / folder
        doomed = {directory / f"{session}{suffix}" for session in sessions
                  if session and Path(f"{session}{suffix}").name == f"{session}{suffix}"}
        try:
            doomed.update(directory.iterdir() if directory.is_dir() else ())
        except OSError:
            pass
        for path in doomed:
            try:
                if path.stem in sessions or path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass


def ask_jev(conn: sqlite3.Connection, session_id: str, ask: jev.Ask = jev.http_ask,
            budget: float = 90.0, clock=None) -> dict:
    """Ask Jev what it hasn't been asked about this session yet, latest turn first, until
    `budget` seconds are spent; the next run goes on from there. Every request gets the
    deadline, so a slow one can't run past it. A turn Jev fails on is noted in
    "errors" and skipped, so it can't hold up the others. Answers are kept per wording,
    so a reworded question is asked again rather than reused."""
    import time
    clock = clock or time.monotonic
    started, deadline = clock(), time.monotonic() + budget
    done = {"requests": 0, "kinds": 0, "pairs": 0, "complete": True, "errors": []}

    def call(state, questions):
        if clock() - started > budget:
            raise jev.JevTimeout("out of time")
        done["requests"] += 1
        return ask(state, questions, deadline=deadline)

    turns = [row[0] for row in conn.execute(
        "SELECT p.turn_key FROM parts p JOIN turns t ON t.session_id = p.session_id AND t.turn_key = p.turn_key "
        "WHERE p.session_id = ? GROUP BY p.turn_key ORDER BY MAX(t.seq) DESC", (session_id,))]
    for turn_key in turns:
        try:
            _ask_turn(conn, session_id, turn_key, call, done)
        except jev.JevTimeout:
            done["complete"] = False
            return done
        except jev.JevError as error:
            done["complete"] = False
            done["errors"].append(f"turn {turn_key}: {error}")
    return done


def _ask_turn(conn: sqlite3.Connection, session_id: str, turn_key: str, call, done: dict) -> None:
    rows = conn.execute("SELECT n, text, kinds, wording FROM parts WHERE session_id = ? AND turn_key = ? ORDER BY n",
                        (session_id, turn_key)).fetchall()
    texts = [row[1] for row in rows]
    if any(row[2] is None or row[3] != jev.WORDING for row in rows):
        answers: dict = {}
        for state, questions in jev.kinds_requests(texts):
            answers.update(call(state, questions))
        kinds = jev.read_kinds(answers, len(texts))
        with conn:
            conn.executemany("UPDATE parts SET kinds = ?, wording = ? WHERE session_id = ? AND turn_key = ? AND n = ?",
                             [(json.dumps(k), jev.WORDING, session_id, turn_key, n) for n, k in enumerate(kinds, 1)])
        done["kinds"] += len(texts)
    paras = [(row[0], row[1], row[2]) for row in conn.execute(
        "SELECT para_key, place, text FROM paras WHERE session_id = ? AND turn_key = ? ORDER BY seq",
        (session_id, turn_key))]
    if not paras:
        return
    for n in range(1, len(texts) + 1):
        asked = {row[0] for row in conn.execute(
            "SELECT para_key FROM pairs WHERE session_id = ? AND turn_key = ? AND n = ? AND wording = ?",
            (session_id, turn_key, n, jev.WORDING))}
        wanted = {key for key, _, _ in paras if key not in asked}
        for state, questions, names in jev.answer_requests(texts, n, paras, wanted) if wanted else []:
            answers = call(state, questions)
            with conn:
                conn.executemany(
                    "INSERT INTO pairs (session_id, turn_key, n, para_key, answers, partly, wording) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (session_id, turn_key, n, para_key) DO UPDATE SET "
                    "answers = excluded.answers, partly = excluded.partly, wording = excluded.wording",
                    [(session_id, turn_key, n, key, float(answers[name]["probabilities"]["answers"]),
                      float(answers[name]["probabilities"]["partly"]), jev.WORDING) for name, key in names.items()])
            done["pairs"] += len(names)


def turn_views(conn: sqlite3.Connection, session_id: str) -> dict:
    """turn key -> {"parts": [{n, text, queued, tags}], "answers": {n: [paragraph rows]}, "complete": bool}.
    Complete when Jev has given every part its kinds and judged every paragraph of the
    reply itself; paragraphs of later wake-ups count once they are judged."""
    views: dict = {}
    for row in conn.execute("SELECT turn_key, n, text, queued, kinds, wording FROM parts WHERE session_id = ? ORDER BY turn_key, n",
                            (session_id,)):
        view = views.setdefault(row[0], {"parts": [], "answers": {}, "complete": True})
        current = row[5] == jev.WORDING and row[4] is not None
        view["complete"] &= current
        view["parts"].append({"n": row[1], "text": row[2], "queued": bool(row[3]),
                              "tags": jev.tags(json.loads(row[4])) if current else []})
    for turn_key, view in views.items():
        count = len(view["parts"])
        paras = conn.execute("SELECT para_key, place, started_at, text, seq FROM paras WHERE session_id = ? AND turn_key = ? ORDER BY seq",
                             (session_id, turn_key)).fetchall()
        pairs = {}
        for row in conn.execute("SELECT n, para_key, answers FROM pairs WHERE session_id = ? AND turn_key = ? AND wording = ?",
                                (session_id, turn_key, jev.WORDING)):
            pairs[(row[0], row[1])] = row[2]
        reply_keys = [p[0] for p in paras if p[1] == "reply to the message"]
        if not reply_keys or any((n, key) not in pairs for n in range(1, count + 1) for key in reply_keys):
            view["complete"] = False
        for n in range(1, count + 1):
            view["answers"][n] = [{"place": p[1], "started_at": p[2], "text": p[3], "p": pairs[(n, p[0])]}
                                  for p in paras if pairs.get((n, p[0]), 0.0) >= jev.CUT]
    return views


def name(session_id: str, title: Optional[str]) -> str:
    """How focused names a session to a person: by its title, as Claude Code names it, with
    the start of its id, which also names it."""
    return f"“{title}” ({session_id[:8]})" if title else f"session {session_id[:8]}"


def session_name(conn: sqlite3.Connection, session_id: str) -> str:
    row = conn.execute("SELECT title FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    return name(session_id, row[0] if row else None)


def session_repos(conn: sqlite3.Connection, session_id: str, worked: bool = False) -> list:
    """A session's repositories as [{"repo", "entries"}], most used first; with `worked`, only
    those it worked in."""
    return [{"repo": row[0], "entries": row[1]} for row in conn.execute(
        "SELECT repo, entries FROM session_repos WHERE session_id = ? AND entries >= ? ORDER BY entries DESC, repo",
        (session_id, 1 if worked else 0))]


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
    # focused's own commands answer with copies of other answers. The search asks the same
    # function the list does, so the two can't disagree on which turns those are.
    conn.create_function("is_own_command", 1, is_own_command, deterministic=True)
    return conn.execute(
        "SELECT t.*, s.title, snippet(turns_fts, -1, '[', ']', ' … ', 12) AS hit "
        "FROM turns_fts JOIN turns t ON t.id = turns_fts.rowid "
        "LEFT JOIN sessions s ON s.session_id = t.session_id "
        "WHERE turns_fts MATCH ? AND NOT is_own_command(t.prompt) "
        "ORDER BY rank LIMIT ?",
        (query, limit)).fetchall()


def search_paras(conn: sqlite3.Connection, words: list, limit: int = 10) -> list:
    """Full-text search over the paragraphs Claude wrote, wake-ups included, with the
    message each one is filed under."""
    query = " ".join('"' + w.replace('"', '""') + '"' for w in words if w.strip())
    if not query:
        return []
    return conn.execute(
        "SELECT p.session_id, p.turn_key, p.started_at, t.prompt, s.title, "
        "snippet(paras_fts, -1, '[', ']', ' … ', 12) AS hit "
        "FROM paras_fts JOIN paras p ON p.id = paras_fts.rowid "
        "JOIN turns t ON t.session_id = p.session_id AND t.turn_key = p.turn_key "
        "LEFT JOIN sessions s ON s.session_id = p.session_id "
        "WHERE paras_fts MATCH ? ORDER BY rank LIMIT ?",
        (query, limit)).fetchall()
