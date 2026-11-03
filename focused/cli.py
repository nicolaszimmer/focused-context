"""focused: index Claude Code turns, list each question with its answer, search them.

    focused hook stop|session-end|session-start|prompt   # as a hook: reads the hook's JSON on stdin
    focused index <transcript.jsonl>  # index one transcript
    focused index --all [--jev]       # index every transcript under ~/.claude/projects
    focused answers [-n N] [--session ID] [--events] [--full]
    focused find <words>
    focused context [--session ID] [--full]   # the summary loaded after /clear and compaction
    focused install [--jev] [--uninstall]     # hooks and commands for a cloud environment

With FOCUSED_JEV=1 and an OpenRouter key, the Stop hook also asks Jev what each part
of a message does and which paragraphs answer it (focused/jev.py); `answers` then lists
the parts with their answers. Without it, each turn shows its final text, as before.

With FOCUSED_REMOTE_URL and FOCUSED_REMOTE_TOKEN, the hooks and `index` also push each
session to the hosted index (focused/remote.py); `answers --session` and `context` read
a session from it when this machine has none, and `find` searches it too.

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
import time
import traceback
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import idle, install, jev, remote, store, summary
from .transcript import LISTED, is_own_command


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
    """A turn a person may have started, other than focused's own commands."""
    return row["origin"] in LISTED and not is_own_command(row["prompt"])


def _jev_lines(view: dict, full: bool) -> list:
    out = []
    for part in view["parts"]:
        who = "You, while Claude worked" if part["queued"] else "You"
        text = part["text"].strip() if full else preview(part["text"], 300)
        out.append(f"{who} [{', '.join(part['tags'])}]: {text}")
        answers = view["answers"].get(part["n"], [])
        for answer in answers:
            where = "" if answer["place"] == "reply to the message" else \
                f" (after a wake-up, {(answer['started_at'] or '')[11:16]})"
            body = answer["text"].strip() if full else preview(answer["text"], 400)
            out.append(f"  → {body}{where}".replace("\n", "\n    "))
        if not answers and any(tag in jev.NEEDS_ANSWER for tag in part["tags"]):
            out.append("  → (no answer found)")
    return out


def format_turns(rows, full: bool = False, events: bool = False, number_from: int = 1, views=None) -> str:
    """Each listed turn with its answer; synthetic turns folded under it. A turn Jev has
    read shows each part of the message with the paragraphs that answer it."""
    views = views or {}
    listed = [r for r in rows if is_question(r)]
    children: dict = {}
    for row in rows:
        if row["parent_key"]:
            children.setdefault(row["parent_key"], []).append(row)
    out = []
    for i, row in enumerate(listed, number_from):
        mark = {"human": "", "sdk": " · via SDK"}.get(row["origin"], " · origin unknown")
        out.append(f"## {i}. {(row['started_at'] or '')[:16].replace('T', ' ')}{mark}")
        view = views.get(row["turn_key"])
        if view and view["complete"]:
            out.extend(_jev_lines(view, full))
        else:
            out.append("You: " + (row["prompt"].strip() or "(no text)" if full else preview(row["prompt"], 200, first_line=True)))
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
        if _holds(db, session):
            return db
    return store.db_for_cwd(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def _holds(db: Path, session: str) -> bool:
    try:
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
            return conn.execute("SELECT 1 FROM turns WHERE session_id = ? LIMIT 1", (session,)).fetchone() is not None
    except sqlite3.Error:
        return False


def _with_session(args, db: Path, session: Optional[str]) -> Path:
    """The index that holds `session`: this project's, another one on this machine, or
    the one its rows are read into from the hosted index (its own project's, or --db).
    Without any, this project's, which then says it has no such session."""
    if not session:
        return db
    if _holds(db, session):
        return db
    if not args.db:
        for other in sorted(store.home().glob("*.sqlite")):
            if other != db and _holds(other, session):
                return other
    try:
        if remote.settings() is None:
            return db
        data = remote.fetch(session)
        if data is None:
            return db
        target = db if args.db else store.home() / f"{data['project']}.sqlite"
        with closing(store.connect(target)) as conn:
            remote.save(conn, session, data)
        return target
    except (remote.RemoteError, sqlite3.Error) as error:
        print(f"focused: could not read session {session} from the hosted index ({error}).\n")
        return db


def _fail(args, message: str, code: int) -> int:
    if args.in_session:
        print(message)
        return 0
    print(message, file=sys.stderr)
    return code


def _log(what: str, detail: str = "") -> None:
    """A line in ~/.claude/focused/errors.log; inside an except block, with the traceback."""
    try:
        log = store.home() / "errors.log"
        log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        trace = traceback.format_exc() if sys.exc_info()[0] else ""
        with open(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(timezone.utc).isoformat()} {what}{(': ' + detail) if detail else ''}\n{trace}\n")
    except OSError:
        pass


class _SessionLock:
    """One Jev run per session at a time: a second async Stop hook skips, and the next
    run picks up whatever it would have asked. Where there is no fcntl, no lock."""

    def __init__(self, session: str):
        self.path = store.home() / f"jev-{session}.lock"
        self.handle = None

    def __enter__(self) -> bool:
        try:
            import fcntl
        except ImportError:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.handle = open(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600), "w")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def __exit__(self, *exc) -> None:
        if self.handle:
            self.handle.close()


CLEARED = "cleared.json"
FULL_PRINTED = 25_000  # longest --full summary printed; the Bash tool returns 30,000 by default


def _note_cleared(db: Path, session: str) -> None:
    """The session /clear just ended, for the new session's SessionStart hook."""
    home = store.home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    note = {"db": str(db), "session": session, "at": time.time()}
    with open(os.open(home / CLEARED, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
        json.dump(note, handle)


def _cleared(db: Path) -> Optional[str]:
    try:
        note = json.loads((store.home() / CLEARED).read_text())
    except (OSError, ValueError):
        return None
    fresh = time.time() - float(note.get("at", 0)) < 300
    return note.get("session") if fresh and note.get("db") == str(db) else None


def _previous_session(conn, db: Path, current: Optional[str]) -> Optional[str]:
    """The session /clear just ended (its SessionEnd note), else the project's most
    recent other session, re-read so its last turns are in. When this machine has no
    other session, as in a new cloud container, the project's latest in the hosted index."""
    target = _cleared(db) or next((row[0] for row in conn.execute(
        "SELECT session_id FROM sessions WHERE session_id IS NOT ? ORDER BY last_at DESC LIMIT 1",
        (current,))), None) or _hosted_previous(conn, db, current)
    row = conn.execute("SELECT transcript_path FROM sessions WHERE session_id = ?", (target,)).fetchone()
    if row and row[0]:
        _reindex(conn, row[0])  # its last turns, if the Stop hook hadn't got to them
    return target


def _hosted_previous(conn, db: Path, current: Optional[str]) -> Optional[str]:
    """The project's most recent other session in the hosted index, read into this one."""
    try:
        if remote.settings() is None:
            return None
        listed = remote.sessions(db.stem, limit=5, timeout=5.0)
        target = next((row.get("session_id") for row in listed if row.get("session_id") != current), None)
        data = remote.fetch(target, timeout=5.0) if target else None
        if data is None:
            return None
        remote.save(conn, target, data)
        return target
    except (remote.RemoteError, sqlite3.Error) as error:
        failure = str(error)
    _log("hosted index", f"the previous session: {failure}")
    return None


def _push(conn, session: str, transcript, timeout: float) -> Optional[str]:
    """Push what changed in a session to the hosted index, if one is set up. Returns
    what went wrong, also written to the log, or None."""
    try:
        if remote.settings() is None:
            return None
        done = remote.push(conn, session, Path(transcript).parent.name, timeout=timeout)
        for key in done["skipped"]:
            _log("push", f"session {session}: turn {key} is larger than a push can carry")
        return None
    except (remote.RemoteError, sqlite3.Error) as error:
        failure = f"session {session}: {error}"
    _log("push", failure)
    return failure


def _reindex(conn, path) -> None:
    """Re-read a transcript before summarizing it. If that fails (the index locked past
    its timeout, the file gone), the summary is built from the index as it is."""
    if not os.path.exists(path):
        return
    try:
        store.index_transcript(conn, path)
    except (OSError, ValueError, sqlite3.Error):
        _log("reindex before the summary", str(path))


def _summary_file(session: str) -> Path:
    """Where a session's full summary is kept: directly in summaries/, named after the
    session. A session id that isn't a plain file name is refused, so no summary is
    written, or pointed at, anywhere else."""
    name = f"{session}.md"
    if not session or Path(name).name != name or name.startswith("."):
        raise ValueError(f"session id {session!r} is not a plain name")
    return store.home() / "summaries" / name


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
        handle.write(text)


def cmd_hook_session_start(args) -> int:
    """After /clear or compaction, print where the session left off; Claude Code adds
    it to the new context. Only reads the index: SessionStart must be quick. Fails
    open and prints nothing when there is nothing to say."""
    try:
        payload = json.load(sys.stdin)
        source, current, path = payload.get("source"), payload.get("session_id"), payload.get("transcript_path")
        if source not in ("clear", "compact") or not path:
            return 0
        db = _db(args, path)
        if source == "clear" and not db.exists():  # nothing was ever indexed for this project
            return 0
        with closing(store.connect(db)) as conn:
            if source == "compact":
                _reindex(conn, path)
                target = current
            else:
                target = _previous_session(conn, db, current)
            if not target:
                return 0
            full_path = _summary_file(target)
            short, full = summary.build(conn, target, str(full_path))
        if short:
            _write_private(full_path, full)
            sys.stdout.write(short)
    except Exception:  # noqa: BLE001 - a hook must never fail the session
        _log("hook session-start")
    return 0


def cmd_hook_prompt(args) -> int:
    """UserPromptSubmit: after an hour without a reply, stop one typed message, so the
    person can /clear before the session is sent again without the cache
    (focused/idle.py). Prints nothing otherwise, as plain stdout would be added to
    Claude's context. Fails open: an error lets the message through."""
    try:
        reason = idle.check(json.load(sys.stdin))
        if reason:
            sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))
    except Exception:  # noqa: BLE001 - a hook must never fail the session
        _log("hook prompt")
    return 0


def cmd_hook(args) -> int:
    if args.event == "session-start":
        return cmd_hook_session_start(args)
    if args.event == "prompt":
        return cmd_hook_prompt(args)
    return cmd_hook_stop(args)


def cmd_hook_stop(args) -> int:
    """Fails open: never blocks Claude, never prints to stdout. With Jev on, the Stop
    hook (async) also asks Jev about what it hasn't read yet; SessionEnd doesn't, as its
    time is short."""
    try:
        payload = json.load(sys.stdin)
        path = payload.get("transcript_path")
        if args.event == "session-end" and payload.get("reason") == "clear" and path and payload.get("session_id"):
            _note_cleared(_db(args, path), payload["session_id"])
        if path and os.path.exists(path):
            stop_message = payload.get("last_assistant_message")
            with closing(store.connect(_db(args, path))) as conn:
                parsed = store.index_transcript(conn, path, stop_message=stop_message)
                session = parsed.session_id or Path(path).stem
                if args.event == "stop" and jev.enabled():
                    wanted = (stop_message or "").strip()
                    if wanted and not any(wanted == reply.strip() for t in parsed.turns for reply in t.replies):
                        time.sleep(2)  # the transcript may not hold the final message yet
                        parsed = store.index_transcript(conn, path, stop_message=stop_message)
                    with _SessionLock(session) as mine:
                        if mine:
                            try:
                                done = store.ask_jev(conn, session)
                                for error in done["errors"]:
                                    _log("jev", error)
                            except Exception:  # noqa: BLE001 - what Jev didn't read is shown as before
                                _log("jev")
                # SessionEnd's time is short; what it can't push, the next push sends.
                _push(conn, session, path, timeout=60.0 if args.event == "stop" else 8.0)
    except Exception:  # noqa: BLE001 - a hook must never fail the turn
        _log("hook stop")
    return 0


def cmd_index(args) -> int:
    paths = list(args.paths)
    if args.all:
        paths += sorted(str(p) for p in projects_dir().glob("*/*.jsonl"))
    if not paths:
        print("Nothing to index: name a transcript, or pass --all.", file=sys.stderr)
        return 2
    if args.jev and not jev.api_key():
        print("--jev needs an OpenRouter key: FOCUSED_OPENROUTER_API_KEY or OPENROUTER_API_KEY.", file=sys.stderr)
        return 2
    try:
        hosted = remote.settings() is not None
    except remote.RemoteError as error:
        print(f"focused: {error}", file=sys.stderr)
        return 2
    failed = 0
    for path in paths:  # one bad transcript must not stop the rest
        asked = ""
        try:
            with closing(store.connect(_db(args, path))) as conn:
                parsed = store.index_transcript(conn, path)
                session = parsed.session_id or Path(path).stem
                if args.jev:
                    done = store.ask_jev(conn, session, budget=float("inf"))
                    asked = f", {done['requests']} Jev requests"
                    for error in done["errors"]:
                        print(f"{path}: Jev failed on {error}", file=sys.stderr)
                    failed += bool(done["errors"])
                if hosted:
                    problem = _push(conn, session, path, timeout=300.0)
                    if problem:
                        print(f"{path}: not pushed ({problem})", file=sys.stderr)
                        failed += 1
                    asked += ", pushed" if not problem else ""
        except (OSError, ValueError, sqlite3.Error, jev.JevError) as error:
            print(f"{path}: skipped ({error})", file=sys.stderr)
            failed += 1
            continue
        listed = sum(1 for t in parsed.turns if t.listed)
        others = len(parsed.turns) - listed
        print(f"{path}: {listed} message{'' if listed == 1 else 's'}, {others} other turn{'' if others == 1 else 's'}{asked}")
    return 1 if failed else 0


def cmd_answers(args) -> int:
    with closing(store.connect(_with_session(args, _db(args), args.session))) as conn:
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
    print(format_turns(rows, full=args.full, events=args.events, number_from=max(first, 1),
                       views=store.turn_views(conn, session)))
    return 0


def cmd_context(args) -> int:
    db = _with_session(args, _db(args), args.session)
    with closing(store.connect(db)) as conn:
        session = args.session or args.in_session or store.latest_session(conn)
        if not session:
            return _fail(args, "No indexed sessions for this project.", 1)
        try:
            full_path = _summary_file(session)
            short, full = summary.build(conn, session, str(full_path))
            if not short and args.in_session and not args.session:
                # Right after /clear the session holds only this command: show the one
                # cleared, as the SessionStart hook did.
                previous = _previous_session(conn, db, args.in_session)
                if previous:
                    session = previous
                    full_path = _summary_file(session)
                    short, full = summary.build(conn, session, str(full_path))
        except ValueError as error:
            return _fail(args, f"focused: {error}.", 2)
    if not short:
        return _fail(args, f"Session {session} has no messages indexed.", 1)
    _write_private(full_path, full)
    if args.full and len(full) > FULL_PRINTED:
        # The Bash tool returns at most 30,000 characters by default (BASH_MAX_OUTPUT_LENGTH),
        # so a longer summary is read from its file instead (skills/context/SKILL.md).
        print(f"The full summary is {len(full):,} characters, more than the Bash tool returns at once. "
              f"It is in this file: {full_path}")
        return 0
    print(full if args.full else short)
    return 0


def cmd_install(args) -> int:
    """For a cloud environment's setup script (scripts/install-cloud.sh): install the hooks
    and commands for every session of the environment, or remove them."""
    try:
        if args.uninstall:
            done = install.uninstall()
        else:
            done = install.install(Path(args.root) if args.root else None, jev=args.jev)
    except (OSError, ValueError) as error:
        print(f"focused: {error}", file=sys.stderr)
        return 1
    for line in done or ["nothing to remove"]:
        print(f"focused: {line}")
    return 0


def cmd_find(args) -> int:
    if not args.words:
        return _fail(args, "Name the words to search for, as in: find webhook retry", 2)
    db = _db(args)
    with closing(store.connect(db)) as conn:
        rows = store.search(conn, args.words, limit=args.limit)
        paras = store.search_paras(conn, args.words, limit=args.limit)
        here = {row[0] for row in conn.execute("SELECT session_id FROM sessions")}
    hosted, problem = _hosted_find(args, db.stem, here)
    if not rows and not paras and not hosted:
        if problem:
            print(problem)
        print("No match.")
        return 0 if args.in_session else 1
    for row in rows:
        when = (row["started_at"] or "")[:16].replace("T", " ")
        print(f"- {when} · session {row['session_id'][:8]} · {row['origin']}: {row['hit']}")
    if paras:
        print("\nIn Claude's replies, wake-ups included:" if rows else "In Claude's replies, wake-ups included:")
        for row in paras:
            when = (row["started_at"] or "")[:16].replace("T", " ")
            print(f"- {when} · session {row['session_id'][:8]} · after: {preview(row['prompt'], 60, first_line=True)}\n  {row['hit']}")
    if hosted:
        print(("\n" if rows or paras else "") + "In the hosted index, from sessions not on this machine:")
        print("\n".join(hosted))
    if problem:
        print(("\n" if rows or paras or hosted else "") + problem)
    return 0


def _hosted_find(args, project: str, here: set) -> tuple:
    """Lines for the hosted index's matches from sessions this index doesn't hold, each
    with the full session id, as `answers --session` and `context --session` take it;
    and what went wrong, if anything."""
    try:
        if remote.settings() is None:
            return [], None
        found = remote.search(args.words, project, limit=args.limit)
    except remote.RemoteError as error:
        return [], f"focused: could not search the hosted index ({error})."
    lines = []
    for row in found["turns"]:
        if row.get("session_id") in here or is_own_command(row.get("prompt")):
            continue
        when = (row.get("started_at") or "")[:16].replace("T", " ")
        lines.append(f"- {when} · session {row.get('session_id')} · {row.get('origin')}: {row.get('hit')}")
    for row in found["paras"]:
        if row.get("session_id") in here:
            continue
        when = (row.get("started_at") or "")[:16].replace("T", " ")
        lines.append(f"- {when} · session {row.get('session_id')} · after: "
                     f"{preview(row.get('prompt'), 60, first_line=True)}\n  {row.get('hit')}")
    return lines, None


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
    # so a turn whose async run was cut off by the exit is still indexed. SessionStart loads
    # the summary after /clear and compaction; UserPromptSubmit ("prompt") stops one message
    # after an hour without a reply.
    hook.add_argument("event", choices=["stop", "session-end", "session-start", "prompt"])
    hook.set_defaults(run=cmd_hook)

    index = sub.add_parser("index", help="index transcripts")
    index.add_argument("paths", nargs="*")
    index.add_argument("--all", action="store_true", help="every transcript under ~/.claude/projects")
    index.add_argument("--jev", action="store_true", help="also ask Jev about every turn it hasn't read")
    index.set_defaults(run=cmd_index)

    answers = sub.add_parser("answers", help="each question with its answer")
    answers.add_argument("-n", type=int, default=0, help="only the last N questions")
    answers.add_argument("--session", help="a session id (default: the latest in this project)")
    answers.add_argument("--events", action="store_true", help="list the folded wake-ups and their replies")
    answers.add_argument("--full", action="store_true", help="print complete prompts and answers")
    answers.set_defaults(run=cmd_answers)

    context = sub.add_parser("context", help="the summary loaded after /clear and compaction")
    context.add_argument("--session", help="a session id (default: the latest in this project)")
    context.add_argument("--full", action="store_true", help="every message, not only what fits the hook's limit")
    context.set_defaults(run=cmd_context)

    setup = sub.add_parser("install", help="install the hooks and commands for a cloud environment")
    setup.add_argument("--root", help="the focused-context checkout the hooks run (default: this one)")
    setup.add_argument("--jev", action="store_true", help="ask Jev in the Stop hook (needs an OpenRouter key)")
    setup.add_argument("--uninstall", action="store_true", help="remove what install put in place")
    setup.set_defaults(run=cmd_install)

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
