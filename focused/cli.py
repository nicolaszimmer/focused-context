"""focused: index Claude Code turns, list each question with its answer, search them.

    focused hook stop|session-end     # as a hook: reads the hook's JSON on stdin
    focused index <transcript.jsonl>  # index one transcript
    focused index --all               # index every transcript under ~/.claude/projects
    focused answers [-n N] [--session ID] [--events] [--full]
    focused find <words>

From a skill, pass --in-session <id>: focused reindexes that session's transcript, uses
its project's index, and reports every problem on stdout with exit code 0, so the skill
shows it instead of failing.
"""


from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import traceback
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import store
from .transcript import LISTED


class SkillMessage(Exception):
    """An argument error to report on stdout instead of exiting non-zero."""


class Parser(argparse.ArgumentParser):
    skill_mode = False

    def error(self, message):
        if Parser.skill_mode:
            raise SkillMessage(f"{self.prog}: {message}")
        super().error(message)


def preview(text, limit: int, first_line: bool = False) -> str:
    text = (text or "").strip()
    if not text:
        return "(no text)"
    short = text.splitlines()[0] if first_line else text
    short = short[:limit]
    return short if short == text else short + " [...]"


def _list(value: str) -> list:
    try:
        return json.loads(value or "[]")
    except ValueError:
        return []


def is_question(row) -> bool:
    """A turn a person may have started, other than the plugin's own /focused: commands."""
    return row["origin"] in LISTED and not (row["prompt"] or "").startswith("/focused:")


def format_turns(rows, full: bool = False, events: bool = False, number_from: int = 1) -> str:
    """Each listed turn with its answer; synthetic turns folded under it."""
    listed = [r for r in rows if is_question(r)]
    children: dict = {}
    for row in rows:
        if row["parent_key"]:
            children.setdefault(row["parent_key"], []).append(row)
    out = []
    for i, row in enumerate(listed, number_from):
        mark = {"human": "", "sdk": " · via SDK"}.get(row["origin"], " · origin unknown")
        out.append(f"## {i}. {(row['started_at'] or '')[:16].replace('T', ' ')}{mark}")
        out.append("Q: " + (row["prompt"].strip() or "(no text)" if full else preview(row["prompt"], 200, first_line=True)))
        answer = row["answer"]
        if answer is None:
            out.append("A: (no answer yet)")
        else:
            out.append("A: " + (answer.strip() if full else preview(answer, 600)))
        facts = []
        for label, key in (("files", "files"), ("commits", "commits"), ("PRs", "pull_requests")):
            values = _list(row[key])
            if values:
                facts.append(f"{label}: " + ", ".join(values))
        if facts:
            out.append("   " + " · ".join(facts))
        folded = children.get(row["turn_key"], [])
        if folded:
            kinds = Counter(r["origin"] for r in folded)
            out.append(f"   +{len(folded)} events: " + ", ".join(f"{n} {k}" for k, n in kinds.most_common()))
            if events:
                for event in folded:
                    reply = preview(event["answer"], 160) if event["answer"] else "(no reply)"
                    out.append(f"   - {event['origin']}: {preview(event['prompt'], 80, first_line=True)} -> {reply}")
        out.append("")
    return "\n".join(out)


def projects_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser() / "projects"


def _db(args, transcript=None) -> Path:
    if args.db:
        return Path(args.db)
    if transcript:
        return store.db_for_transcript(transcript)
    if args.in_session:
        return _db_for_session(args.in_session)
    return store.db_for_cwd(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def _db_for_session(session: str) -> Path:
    """The index of the project this session belongs to, refreshed from its transcript."""
    for transcript in projects_dir().glob(f"*/{glob.escape(session)}.jsonl"):
        db = store.db_for_transcript(transcript)
        try:
            with closing(store.connect(db)) as conn:
                store.index_transcript(conn, transcript)
        except (OSError, ValueError, sqlite3.Error) as error:  # e.g. locked by the Stop hook
            print(f"focused: could not refresh the index ({error}); answering from the existing one.\n")
        return db
    for db in sorted(store.home().glob("*.sqlite")):  # the transcript is gone, the index may not be
        try:
            with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
                if conn.execute("SELECT 1 FROM sessions WHERE session_id = ?", (session,)).fetchone():
                    return db
        except sqlite3.Error:
            continue
    return store.db_for_cwd(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def _fail(args, message: str, code: int) -> int:
    if args.in_session:
        print(message)
        return 0
    print(message, file=sys.stderr)
    return code


def cmd_hook_stop(args) -> int:
    """Fails open: never blocks Claude, never prints to stdout."""
    try:
        payload = json.load(sys.stdin)
        path = payload.get("transcript_path")
        if path and os.path.exists(path):
            conn = store.connect(_db(args, path))
            store.index_transcript(conn, path, stop_message=payload.get("last_assistant_message"))
            conn.close()
    except Exception:  # noqa: BLE001 - a hook must never fail the turn
        try:
            log = store.home() / "errors.log"
            log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with open(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a", encoding="utf-8") as handle:
                handle.write(f"{datetime.now(timezone.utc).isoformat()} hook stop\n{traceback.format_exc()}\n")
        except OSError:
            pass
    return 0


def cmd_index(args) -> int:
    paths = list(args.paths)
    if args.all:
        paths += sorted(str(p) for p in projects_dir().glob("*/*.jsonl"))
    if not paths:
        print("Nothing to index: name a transcript, or pass --all.", file=sys.stderr)
        return 2
    failed = 0
    for path in paths:  # one bad transcript must not stop the rest
        try:
            with closing(store.connect(_db(args, path))) as conn:
                parsed = store.index_transcript(conn, path)
        except (OSError, ValueError, sqlite3.Error) as error:
            print(f"{path}: skipped ({error})", file=sys.stderr)
            failed += 1
            continue
        listed = sum(1 for t in parsed.turns if t.listed)
        print(f"{path}: {listed} questions, {len(parsed.turns) - listed} other turns")
    return 1 if failed else 0


def cmd_answers(args) -> int:
    with closing(store.connect(_db(args))) as conn:
        return _answers(args, conn)


def _answers(args, conn) -> int:
    session = args.session or args.in_session or store.latest_session(conn)
    if not session:
        return _fail(args, "No indexed sessions for this project. Run `focused index --all`, or enable the Stop hook.", 1)
    rows = store.session_turns(conn, session)
    if not rows:
        return _fail(args, f"No indexed sessions match {session}. Run `focused index --all`, or enable the Stop hook.", 1)
    listed = [r for r in rows if is_question(r)]
    keep = {r["turn_key"] for r in listed[-args.n:]} if args.n else None
    if keep is not None:
        rows = [r for r in rows if r["turn_key"] in keep or r["parent_key"] in keep]
    first = len(listed) - len(keep) + 1 if keep is not None else 1
    print(f"# session {session}\n")
    print(format_turns(rows, full=args.full, events=args.events, number_from=max(first, 1)))
    return 0


def cmd_find(args) -> int:
    if not args.words:
        return _fail(args, "Name the words to search for, as in: find webhook retry", 2)
    with closing(store.connect(_db(args))) as conn:
        rows = store.search(conn, args.words, limit=args.limit)
    if not rows:
        print("No match.")
        return 0 if args.in_session else 1
    for row in rows:
        when = (row["started_at"] or "")[:16].replace("T", " ")
        print(f"- {when} · session {row['session_id'][:8]} · {row['origin']}: {row['hit']}")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    Parser.skill_mode = "--in-session" in argv
    parser = Parser(prog="focused", description=__doc__,
                    formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", help="the index database (default: one per project under ~/.claude/focused)")
    parser.add_argument("--in-session", metavar="ID",
                        help="use the index of this session's project, refreshed first (for skills)")
    sub = parser.add_subparsers(dest="command", required=True)

    hook = sub.add_parser("hook", help="run as a Claude Code hook")
    # Stop runs async after each turn; SessionEnd runs in the foreground on exit or /clear,
    # so a turn whose async run was cut off by the exit is still indexed.
    hook.add_argument("event", choices=["stop", "session-end"])
    hook.set_defaults(run=cmd_hook_stop)

    index = sub.add_parser("index", help="index transcripts")
    index.add_argument("paths", nargs="*")
    index.add_argument("--all", action="store_true", help="every transcript under ~/.claude/projects")
    index.set_defaults(run=cmd_index)

    answers = sub.add_parser("answers", help="each question with its answer")
    answers.add_argument("-n", type=int, default=0, help="only the last N questions")
    answers.add_argument("--session", help="a session id (default: the latest in this project)")
    answers.add_argument("--events", action="store_true", help="list the folded wake-ups and their replies")
    answers.add_argument("--full", action="store_true", help="print complete prompts and answers")
    answers.set_defaults(run=cmd_answers)

    find = sub.add_parser("find", help="search prompts and answers")
    find.add_argument("words", nargs="*")
    find.add_argument("--limit", type=int, default=10)
    find.set_defaults(run=cmd_find)

    try:
        args = parser.parse_args(argv)
    except SkillMessage as message:
        print(message)
        return 0
    if not args.in_session:
        return args.run(args)
    try:
        return args.run(args)
    except (OSError, ValueError, sqlite3.Error) as error:  # e.g. the index can't be opened at all
        print(f"focused: {error}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
