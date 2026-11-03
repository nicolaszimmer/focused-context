"""focused: index Claude Code turns, list each question with its answer, search them.

    focused hook stop                 # as a Stop hook: reads the hook's JSON on stdin
    focused index <transcript.jsonl>  # index one transcript
    focused index --all               # index every transcript under ~/.claude/projects
    focused answers [-n N] [--session ID] [--events] [--full]
    focused find <words>
"""

from __future__ import annotations

import argparse
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


def format_turns(rows, full: bool = False, events: bool = False, number_from: int = 1) -> str:
    """Each listed turn with its answer; synthetic turns folded under it."""
    listed = [r for r in rows if r["origin"] in ("human", "unknown")]
    children: dict = {}
    for row in rows:
        if row["parent_key"]:
            children.setdefault(row["parent_key"], []).append(row)
    out = []
    for i, row in enumerate(listed, number_from):
        mark = "" if row["origin"] == "human" else " · origin unknown"
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


def _db(args, transcript=None) -> Path:
    if args.db:
        return Path(args.db)
    if transcript:
        return store.db_for_transcript(transcript)
    return store.db_for_cwd(os.getcwd())


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
        paths += sorted(str(p) for p in (Path.home() / ".claude" / "projects").glob("*/*.jsonl"))
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
    session = args.session or store.latest_session(conn)
    if not session:
        print("No indexed sessions for this project. Run `focused index --all`, or enable the Stop hook.", file=sys.stderr)
        return 1
    rows = store.session_turns(conn, session)
    listed = [r for r in rows if r["origin"] in ("human", "unknown")]
    keep = {r["turn_key"] for r in listed[-args.n:]} if args.n else None
    if keep is not None:
        rows = [r for r in rows if r["turn_key"] in keep or r["parent_key"] in keep]
    first = len(listed) - len(keep) + 1 if keep is not None else 1
    print(f"# session {session}\n")
    print(format_turns(rows, full=args.full, events=args.events, number_from=max(first, 1)))
    return 0


def cmd_find(args) -> int:
    with closing(store.connect(_db(args))) as conn:
        rows = store.search(conn, args.words, limit=args.limit)
    if not rows:
        print("No match.")
        return 1
    for row in rows:
        when = (row["started_at"] or "")[:16].replace("T", " ")
        print(f"- {when} · session {row['session_id'][:8]} · {row['origin']}: {row['hit']}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="focused", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", help="the index database (default: one per project under ~/.claude/focused)")
    sub = parser.add_subparsers(dest="command", required=True)

    hook = sub.add_parser("hook", help="run as a Claude Code hook")
    hook.add_argument("event", choices=["stop"])
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
    find.add_argument("words", nargs="+")
    find.add_argument("--limit", type=int, default=10)
    find.set_defaults(run=cmd_find)

    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
