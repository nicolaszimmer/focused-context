import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate  # noqa: E402,F401  (before any test runs: none reaches a real service)

from focused import cli, store
from focused.transcript import parse, parse_entries

HUMAN = {"origin": {"kind": "human"}, "turnOrigin": "human"}
WAKE = {"origin": {"kind": "task-notification"}, "turnOrigin": "task_notification"}


def user(uuid, text, ts="2026-11-03T06:00:00.000Z", **extra):
    return {"type": "user", "uuid": uuid, "sessionId": "s1", "timestamp": ts, "message": {"content": text}, **extra}


def assistant(*blocks):
    return {"type": "assistant", "sessionId": "s1", "message": {"content": list(blocks)}}


def text(value):
    return {"type": "text", "text": value}


def tool(tool_id, name, **tool_input):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def result(tool_id, content):
    return {"type": "user", "sessionId": "s1", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": content}]}}


SESSION = [
    user("u1", "How do hooks work?\nSecond line", **HUMAN),
    assistant(text("Looking."), tool("t1", "Edit", file_path="README.md")),
    result("t1", "ok"),
    assistant(tool("t2", "Bash", command="git commit -m x"), ),
    result("t2", "[main abc1234] x\n 1 file changed"),
    assistant(tool("t3", "mcp__github__create_pull_request", title="x")),
    result("t3", '{"url":"https://github.com/o/r/pull/7"}'),
    assistant(text("Hooks only add context.")),
    user("w1", "<task-notification>CI green</task-notification>", ts="2026-11-03T06:05:00.000Z", **WAKE),
    assistant(text("No action needed.")),
    user("m1", "Stop hook feedback: untracked files", ts="2026-11-03T06:06:00.000Z", isMeta=True),
    assistant(text("Deleted the cache folder.")),
    user("u2", [{"type": "image", "source": {"data": "A" * 4000}}], ts="2026-11-03T06:10:00.000Z", **HUMAN),
    assistant(text("Let me check."), tool("t4", "Read", file_path="x")),
    {"type": "attachment", "uuid": "q1", "sessionId": "s1", "timestamp": "2026-11-03T06:11:00.000Z",
     "attachment": {"type": "queued_command", "prompt": "also this", "origin": {"kind": "human"}}},
    assistant(text("Both done.")),
    user("old", "a prompt with no origin", ts="2026-11-03T06:12:00.000Z"),
    {"type": "user", "uuid": "sc", "sessionId": "s1", "isSidechain": True, "message": {"content": "subagent prompt"}, **HUMAN},
]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.transcript = self.dir / "projects" / "-work-repo" / "s1.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.write(SESSION)
        self.env = mock.patch.dict(os.environ, {"FOCUSED_HOME": str(self.dir / "focused"),
                                                "FOCUSED_RETENTION_DAYS": "30",
                                                # not a repository: no test gets this one's
                                                "CLAUDE_PROJECT_DIR": str(self.dir)})
        self.env.start()
        self.now = datetime(2026, 11, 3, 7, 0, tzinfo=timezone.utc)
        self.connections = []

    def tearDown(self):
        for conn in self.connections:
            conn.close()
        self.env.stop()
        self.tmp.cleanup()

    def write(self, entries):
        self.transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    def index(self, **kwargs):
        conn = store.connect(store.db_for_transcript(self.transcript))
        self.connections.append(conn)
        store.index_transcript(conn, self.transcript, now=self.now, **kwargs)
        return conn


class ParseTest(Fixture):
    def test_turns_and_origins(self):
        turns = parse(self.transcript).turns
        self.assertEqual([t.origin for t in turns],
                         ["human", "task-notification", "meta", "human", "unknown"])

    def test_wake_up_reply_does_not_replace_the_answer(self):
        first = parse(self.transcript).turns[0]
        self.assertEqual(first.answer, "Hooks only add context.")
        self.assertEqual(first.narration, ["Looking."])

    def test_synthetic_turns_are_filed_under_the_question(self):
        turns = parse(self.transcript).turns
        self.assertEqual([t.parent_key for t in turns[1:3]], ["u1", "u1"])
        self.assertEqual(turns[1].answer, "No action needed.")

    def test_text_followed_by_a_tool_call_is_not_an_answer(self):
        from focused.transcript import parse_entries
        turns = parse_entries([user("x1", "q", **HUMAN), assistant(text("Looking."), tool("t9", "Read", file_path="x"))]).turns
        self.assertIsNone(turns[0].answer)

    def test_a_message_sent_while_working_joins_the_turn(self):
        turn = parse(self.transcript).turns[3]  # the image prompt, still working when "also this" came
        self.assertEqual((turn.prompt.strip(), turn.answer), ("(sent while Claude was working) also this", "Both done."))

    def test_a_message_sent_after_the_answer_starts_a_turn(self):
        from focused.transcript import parse_entries
        entries = [user("x1", "first", **HUMAN), assistant(text("Done.")),
                   {"type": "attachment", "uuid": "x2", "sessionId": "s1", "timestamp": "2026-11-03T06:20:00Z",
                    "attachment": {"type": "queued_command", "prompt": "next", "origin": {"kind": "human"}}},
                   assistant(text("Next done."))]
        queued = parse_entries(entries).turns[1]
        self.assertEqual((queued.origin, queued.prompt, queued.answer), ("human", "next", "Next done."))

    def test_facts_from_tool_calls(self):
        first = parse(self.transcript).turns[0]
        self.assertEqual(first.files, ["README.md"])
        self.assertEqual(first.commits, ["abc1234"])
        self.assertEqual(first.pull_requests, ["https://github.com/o/r/pull/7"])

    def test_commit_lines_git_prints(self):
        from focused.transcript import COMMIT_LINE
        lines = "\n".join(["[main abc1234] x", "[main (root-commit) bcd2345] x",
                           "[detached HEAD cde3456] x", "[feature/a-b def4567] x", "[not a commit] x"])
        self.assertEqual(COMMIT_LINE.findall(lines), ["abc1234", "bcd2345", "cde3456", "def4567"])

    def test_sidechain_skipped_and_image_not_counted(self):
        parsed = parse(self.transcript)
        self.assertNotIn("subagent prompt", [t.prompt for t in parsed.turns])
        self.assertLess(parsed.shares["human prompts"], 100)


class CommandTest(unittest.TestCase):
    def test_a_command_claude_never_answers_is_not_a_message(self):
        parsed = parse_entries([
            user("c1", "<command-name>/clear</command-name>\n<command-args></command-args>"),  # no origin
            user("o1", "<local-command-stdout>Cleared</local-command-stdout>"),
            user("u1", "Where were we?", ts="2026-11-03T06:01:00.000Z", **HUMAN),
            assistant(text("At the index.")),
            user("m1", "<command-name>/model</command-name>", **HUMAN),  # answered by Claude Code, not Claude
            user("w1", "<task-notification>CI green</task-notification>", **WAKE),
            assistant(text("Nothing to do.")),
            user("r1", "<command-name>/review</command-name>\n<command-args>7</command-args>", **HUMAN),
            assistant(text("Reviewed.")),  # a command Claude answers stays a message
            user("p1", "/Users/me/app.py fails on start", **HUMAN),  # a typed message, not yet answered
            user("q1", "Why does <command-name>/clear</command-name> stay in the index?", **HUMAN),  # quotes it
        ])
        self.assertEqual([(t.key, t.origin, t.parent_key) for t in parsed.turns], [
            ("c1", "command", None), ("o1", "command", None), ("u1", "human", None),
            ("m1", "command", "u1"), ("w1", "task-notification", "u1"), ("r1", "human", None),
            ("p1", "human", None), ("q1", "human", None)])
        # Claude Code writes the command's message tag first, as often as its name.
        self.assertEqual(parse_entries([user("c1", "<command-message>clear</command-message>\n"
                                                   "<command-name>/clear</command-name>")]).turns[0].origin, "command")


class StoreTest(Fixture):
    def count(self, conn, sql="SELECT count(*) FROM turns"):
        return conn.execute(sql).fetchone()[0]

    def test_indexing_twice_adds_no_rows(self):
        conn = self.index()
        self.index()
        self.assertEqual(self.count(conn), 5)
        self.assertEqual(self.count(conn, "SELECT count(*) FROM turns_fts WHERE turns_fts MATCH 'hooks'"), 1)

    def test_a_compaction_summary_is_never_a_persons_message(self):
        # Claude Code gives a compaction's summary the origin of the turn it compacted.
        summary = user("cs", "This session is being continued from a previous conversation.\n\n1. Primary Request",
                       ts="2026-11-03T06:30:00.000Z", isCompactSummary=True, isVisibleInTranscriptOnly=True, **HUMAN)
        self.write(SESSION + [summary, assistant(text("Picking up where we left off."))])
        conn = self.index()
        row = conn.execute("SELECT origin, parent_key FROM turns WHERE turn_key = 'cs'").fetchone()
        self.assertEqual(tuple(row), ("compact-summary", "old"))
        # An index that read it as a message (before 0.8.1) loses its parts on the next read.
        conn.execute("INSERT INTO parts (session_id, turn_key, n, text) VALUES ('s1', 'cs', 1, 'This session')")
        conn.execute("INSERT INTO pairs (session_id, turn_key, n, para_key, answers, partly, wording) "
                     "VALUES ('s1', 'cs', 1, 'cs:0.1', 0.9, 0, 'w')")
        conn.commit()
        self.index()
        self.assertEqual(self.count(conn, "SELECT count(*) FROM parts WHERE turn_key = 'cs'"), 0)
        self.assertEqual(self.count(conn, "SELECT count(*) FROM pairs WHERE turn_key = 'cs'"), 0)
        # Its reply is filed under the message it followed.
        self.assertEqual(conn.execute("SELECT turn_key FROM paras WHERE source_key = 'cs'").fetchone()[0], "old")

    def test_a_turn_read_differently_is_rewritten_even_when_nothing_else_changed(self):
        # A summary that opens a transcript has no message before it, so only its origin
        # changes when it is read again under 0.8.1's rule.
        self.write([user("cs", "This session is being continued from a previous conversation.",
                         isCompactSummary=True, **HUMAN), assistant(text("Picking up."))])
        conn = self.index()
        conn.execute("UPDATE turns SET origin = 'human' WHERE turn_key = 'cs'")  # as an older reading stored it
        conn.commit()
        self.index()
        self.assertEqual(conn.execute("SELECT origin FROM turns WHERE turn_key = 'cs'").fetchone()[0], "compact-summary")

    def test_stop_hook_answer_wins_over_a_lagging_transcript(self):
        conn = self.index(stop_message="The final words.")
        self.index()
        row = conn.execute("SELECT answer, answer_source FROM turns WHERE turn_key = 'old'").fetchone()
        self.assertEqual(tuple(row), ("The final words.", "stop-hook"))

    def test_a_late_stop_hook_leaves_the_turn_that_began_since_alone(self):
        conn = self.index(stop_message="Hooks only add context.")  # u1's answer; 'old' has begun since
        rows = {r[0]: tuple(r[1:]) for r in conn.execute("SELECT turn_key, answer, answer_source FROM turns")}
        self.assertEqual(rows["u1"], ("Hooks only add context.", "transcript"))
        self.assertEqual(rows["old"], (None, None))

    def test_a_stop_message_matching_an_earlier_narration_still_answers_the_latest_turn(self):
        conn = self.index(stop_message="Looking.")  # u1 said this before its tool calls, not as its answer
        rows = dict(conn.execute("SELECT turn_key, answer FROM turns").fetchall())
        self.assertEqual(rows["u1"], "Hooks only add context.")
        self.assertEqual(rows["old"], "Looking.")

    def test_reindexing_drops_turns_the_transcript_no_longer_splits_off(self):
        conn = self.index()
        conn.execute("INSERT INTO turns (session_id, turn_key, seq, origin, prompt) VALUES ('s1', 'q1', 9, 'human', 'also this')")
        conn.commit()
        self.index()
        self.assertEqual(self.count(conn, "SELECT count(*) FROM turns WHERE turn_key = 'q1'"), 0)
        self.assertEqual(self.count(conn), 5)

    def test_a_read_that_finds_no_turns_keeps_the_stored_ones(self):
        conn = self.index()
        kept = self.transcript.read_text()
        self.transcript.write_text("")
        self.index()
        self.assertEqual(self.count(conn), 5)
        self.transcript.write_text(kept)

    @unittest.skipUnless(hasattr(sqlite3.Connection, "setlimit"), "setlimit needs Python 3.11")
    def test_a_session_with_more_turns_than_sqlite_takes_variables_reindexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "long.jsonl"
            entries = [user(f"q{n}", f"question {n}", **HUMAN) for n in range(120)]
            transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
            with closing(store.connect(Path(tmp) / "x.sqlite")) as conn:
                conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 50)
                store.index_transcript(conn, transcript, now=self.now)
                store.index_transcript(conn, transcript, now=self.now)
                self.assertEqual(self.count(conn), 120)

    def test_a_prompt_with_no_text_shows_as_such(self):
        out = cli.format_turns([{"turn_key": "i1", "origin": "human", "parent_key": None, "started_at": None,
                                 "prompt": "", "answer": None, "files": "[]", "commits": "[]", "pull_requests": "[]"}])
        self.assertIn("You: (no text)", out)

    def test_search(self):
        conn = self.index()
        hits = store.search(conn, ["hooks"])
        self.assertEqual([h["turn_key"] for h in hits], ["u1"])
        self.assertEqual(store.search(conn, ['"unbalanced']), [])

    def test_retention(self):
        conn = self.index()
        self.now = datetime(2026, 12, 20, tzinfo=timezone.utc)
        self.index()
        self.assertEqual(self.count(conn), 0)
        self.assertEqual(self.count(conn, "SELECT count(*) FROM sessions"), 0)

    def test_database_and_its_wal_files_are_private_under_a_default_umask(self):
        old = os.umask(0o022)
        try:
            conn = self.index()  # left open, so -wal and -shm exist
        finally:
            os.umask(old)
        db = store.db_for_transcript(self.transcript)
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{db}{suffix}")
            self.assertTrue(path.exists(), path)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600, path)
        self.assertEqual(self.count(conn), 5)


class CliTest(Fixture):
    def setUp(self):
        super().setUp()
        # The CLI prunes by the real clock and the fixture's dates are fixed, so keep
        # them; Fixture.tearDown restores the environment.
        os.environ["FOCUSED_RETENTION_DAYS"] = "36500"

    def test_index_skips_a_bad_transcript_and_goes_on(self):
        bad = self.transcript.parent / "bad.jsonl"
        bad.write_bytes(b'{"type":"user","uuid":"b1","sessionId":"s2","origin":{"kind":"human"},'
                        b'"message":{"content":"caf\xc3"}}\n')
        missing = self.transcript.parent / "missing.jsonl"
        err = io.StringIO()
        with redirect_stderr(err):
            code, out = self.run_cli("index", str(bad), str(missing), str(self.transcript))
        self.assertEqual(code, 1)
        self.assertIn("bad.jsonl: 1 message,", out)  # the cut-off character no longer stops the read
        self.assertIn("s1.jsonl: 3 messages, 2 other turns", out)
        self.assertIn("missing.jsonl: skipped", err.getvalue())

    def run_cli(self, *args, stdin=None):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stdin", io.StringIO(stdin or "")):
            code = cli.main(list(args))
        return code, out.getvalue()

    def test_stop_hook_indexes_silently(self):
        payload = json.dumps({"transcript_path": str(self.transcript), "last_assistant_message": "Done."})
        code, out = self.run_cli("hook", "stop", stdin=payload)
        self.assertEqual((code, out), (0, ""))
        conn = sqlite3.connect(store.db_for_transcript(self.transcript))
        self.connections.append(conn)
        self.assertEqual(conn.execute("SELECT count(*) FROM turns").fetchone()[0], 5)

    def test_stop_hook_fails_open(self):
        code, out = self.run_cli("hook", "stop", stdin="not json")
        self.assertEqual((code, out), (0, ""))
        self.assertTrue((self.dir / "focused" / "errors.log").exists())

    def test_answers_lists_questions_with_events_folded(self):
        db = str(store.db_for_transcript(self.transcript))
        self.run_cli("--db", db, "index", str(self.transcript))
        code, out = self.run_cli("--db", db, "answers", "--events")
        self.assertEqual(code, 0)
        self.assertIn("A: Hooks only add context.", out)
        self.assertIn("+2 events: 1 task-notification, 1 meta", out)
        self.assertIn("-> No action needed.", out)
        self.assertIn("You: (sent while Claude was working) also this", out)  # the image prompt has no text
        self.assertIn("origin unknown", out)
        self.assertIn("How do hooks work? [...]", out)

    def test_answers_last_n(self):
        db = str(store.db_for_transcript(self.transcript))
        self.run_cli("--db", db, "index", str(self.transcript))
        _, out = self.run_cli("--db", db, "answers", "-n", "1")
        self.assertIn("## 3.", out)
        self.assertNotIn("Hooks only add context.", out)

    def test_find(self):
        db = str(store.db_for_transcript(self.transcript))
        self.run_cli("--db", db, "index", str(self.transcript))
        code, out = self.run_cli("--db", db, "find", "hooks")
        self.assertEqual(code, 0)
        self.assertIn("[hooks]", out.lower())


ROOT = Path(__file__).resolve().parent.parent


class HeadlessAndCommandTest(unittest.TestCase):
    """Shapes seen in a real `claude -p` session with the plugin loaded."""

    ENTRIES = [
        {"type": "user", "uuid": "p1", "sessionId": "s9", "turnOrigin": "sdk", "promptSource": "sdk",
         "timestamp": "2026-11-03T07:33:00Z", "message": {"content": "Reply with exactly: the sky is blue"}},
        assistant(text("the sky is blue")),
        {"type": "user", "uuid": "p2", "sessionId": "s9", "turnOrigin": "sdk", "timestamp": "2026-11-03T07:34:00Z",
         "message": {"content": "<command-message>focused:answers</command-message>\n"
                                "<command-name>/focused:answers</command-name>\n<command-args>--events</command-args>"}},
        {"type": "user", "uuid": "p3", "sessionId": "s9", "isMeta": True, "timestamp": "2026-11-03T07:34:00Z",
         "message": {"content": "Base directory for this skill: ...\n# session s9"}},
        assistant(text("```# session s9```")),
    ]

    def test_sdk_prompts_are_questions_and_a_command_keeps_its_reply(self):
        from focused.transcript import parse_entries
        turns = parse_entries(self.ENTRIES).turns
        self.assertEqual([(t.origin, t.prompt) for t in turns],
                         [("sdk", "Reply with exactly: the sky is blue"), ("sdk", "/focused:answers --events")])
        self.assertEqual([t.answer for t in turns], ["the sky is blue", "```# session s9```"])
        self.assertTrue(all(t.listed for t in turns))

    def test_answers_leaves_out_the_plugins_own_commands(self):
        from focused.cli import format_turns
        from focused.transcript import parse_entries
        rows = [{"turn_key": t.key, "origin": t.origin, "parent_key": t.parent_key, "started_at": t.started_at,
                 "prompt": t.prompt, "answer": t.answer, "files": "[]", "commits": "[]", "pull_requests": "[]"}
                for t in parse_entries(self.ENTRIES).turns]
        rows.append(dict(rows[1], turn_key="p4", prompt="/focused-answers -n 2", answer="copy"))  # a repository's copy
        rows.append(dict(rows[1], turn_key="p6", prompt="/focused-review", answer="someone else's command"))
        out = format_turns(rows)
        self.assertIn("You: Reply with exactly: the sky is blue", out)
        self.assertNotIn("/focused:answers", out)
        self.assertNotIn("/focused-answers", out)
        self.assertIn("You: /focused-review", out)  # only focused's own names are left out

    def test_the_list_and_the_search_agree_on_what_is_focuseds_own(self):
        from focused.transcript import is_own_command
        self.assertTrue(is_own_command("/focused-find sky"))
        self.assertTrue(is_own_command("/focused-answers"))
        self.assertTrue(is_own_command("/focused-recall the webhook"))  # it answers with copies of other sessions
        self.assertTrue(is_own_command("/focused-context --full"))
        self.assertTrue(is_own_command("/focused-find\tsky"))
        self.assertTrue(is_own_command("/focused-answers\n\n(sent while Claude was working) also this"))
        self.assertFalse(is_own_command("/focused-answersx"))
        self.assertFalse(is_own_command(" /focused-find sky"))
        self.assertFalse(is_own_command("/focused-review"))
        self.assertFalse(is_own_command("/FOCUSED-FIND sky"))  # names are case-sensitive, in the search too

    def test_find_skips_a_repositorys_copies_of_the_commands_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "s9.jsonl"
            copy = dict(self.ENTRIES[2], uuid="p5", message={"content": "<command-name>/focused-find</command-name>\n"
                                                                       "<command-args>sky</command-args>"})
            other = dict(self.ENTRIES[2], uuid="p7", timestamp="2026-11-03T07:35:00Z",
                         message={"content": "<command-name>/focused-review</command-name>"})
            shout = dict(self.ENTRIES[2], uuid="p8", timestamp="2026-11-03T07:36:00Z",
                         message={"content": "<command-name>/FOCUSED-FIND</command-name><command-args>sky</command-args>"})
            entries = self.ENTRIES[:2] + [copy, assistant(text("- the sky is blue")),
                                          other, assistant(text("sky check done")),
                                          shout, assistant(text("Unknown command, sky"))]
            transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
            with closing(store.connect(Path(tmp) / "x.sqlite")) as conn:
                store.index_transcript(conn, transcript, now=datetime(2026, 11, 3, tzinfo=timezone.utc))
                # The list keeps /FOCUSED-FIND (names are case-sensitive), so the search does too.
                self.assertEqual(sorted(h["turn_key"] for h in store.search(conn, ["sky"])), ["p1", "p7", "p8"])

    def test_find_skips_the_plugins_own_command_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "s9.jsonl"
            entries = self.ENTRIES[:-1] + [assistant(text("You: Reply ... A: the sky is blue"))]
            transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
            with closing(store.connect(Path(tmp) / "x.sqlite")) as conn:
                store.index_transcript(conn, transcript, now=datetime(2026, 11, 3, tzinfo=timezone.utc))
                self.assertEqual([h["turn_key"] for h in store.search(conn, ["sky"])], ["p1"])


class PluginTest(unittest.TestCase):
    def test_manifests_and_hooks_point_at_files_that_exist(self):
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
        self.assertEqual(market["plugins"][0]["name"], plugin["name"])
        self.assertEqual(set(hooks), {"Stop", "SessionEnd", "SessionStart", "UserPromptSubmit", "PostToolUse"})
        self.assertEqual(hooks["SessionStart"][0]["matcher"], "clear|compact")
        prompt = hooks["UserPromptSubmit"][0]["hooks"][0]
        self.assertTrue(prompt["command"].endswith(" hook prompt"))
        self.assertNotIn("async", prompt)  # only a hook in the foreground can stop a message
        for event in hooks.values():
            for hook in event[0]["hooks"]:
                self.assertIn("${CLAUDE_PLUGIN_ROOT}/scripts/focused.py", hook["command"])
        self.assertTrue((ROOT / "scripts" / "focused.py").exists())
        for skill in ("answers", "find", "context"):
            body = (ROOT / "skills" / skill / "SKILL.md").read_text()
            self.assertIn("disable-model-invocation: true", body)
            self.assertIn('--in-session "${CLAUDE_SESSION_ID}"', body)
            # What the user types never reaches a shell unquoted: no injected (!) command.
            self.assertNotIn("!`", body)
            self.assertNotIn("```!", body)

    def test_launcher_runs_the_hook_without_installing(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "projects" / "-p" / "s1.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text("\n".join(json.dumps(e) for e in SESSION) + "\n")
            env = dict(os.environ, FOCUSED_HOME=str(Path(tmp) / "home"), FOCUSED_RETENTION_DAYS="36500")
            done = subprocess.run([sys.executable, str(ROOT / "scripts" / "focused.py"), "hook", "session-end"],
                                  input=json.dumps({"transcript_path": str(transcript)}), text=True,
                                  capture_output=True, env=env, cwd=tmp)
            self.assertEqual((done.returncode, done.stdout), (0, ""))
            with closing(sqlite3.connect(Path(tmp) / "home" / "-p.sqlite")) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM turns").fetchone()[0], 5)


class InSessionTest(Fixture):
    """What the skills run: focused --in-session <id> answers|find."""

    def setUp(self):
        super().setUp()
        os.environ["FOCUSED_RETENTION_DAYS"] = "36500"
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.dir)  # transcripts live under <dir>/projects

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(args))
        return code, out.getvalue()

    def test_indexes_the_session_first_and_lists_it(self):
        code, out = self.run_cli("--in-session", "s1", "answers")
        self.assertEqual(code, 0)
        self.assertIn("A: Hooks only add context.", out)
        self.assertTrue(store.db_for_transcript(self.transcript).exists())

    def test_find_searches_that_sessions_project(self):
        code, out = self.run_cli("--in-session", "s1", "find", "hooks")
        self.assertEqual(code, 0)
        self.assertIn("[hooks]", out.lower())

    def test_words_with_quotes_and_shell_characters_are_search_terms(self):
        code, out = self.run_cli("--in-session", "s1", "find", "hooks", "don't", "$(touch pwned)", "`id`")
        self.assertEqual((code, out.strip()), (0, "No match."))
        self.assertFalse(Path("pwned").exists())

    def test_bad_skill_options_are_reported_not_fatal(self):
        code, out = self.run_cli("--in-session", "s1", "answers", "--bogus")
        self.assertEqual(code, 0)
        self.assertIn("unrecognized arguments: --bogus", out)

    def test_an_index_that_cannot_be_opened_is_reported_not_fatal(self):
        with mock.patch.object(store, "connect", side_effect=sqlite3.OperationalError("unable to open database file")):
            code, out = self.run_cli("--in-session", "s1", "answers")
        self.assertEqual(code, 0)
        self.assertIn("unable to open database file", out)

    def test_a_failed_refresh_falls_back_to_the_existing_index(self):
        self.run_cli("--in-session", "s1", "answers")
        with mock.patch.object(store, "index_transcript", side_effect=sqlite3.OperationalError("database is locked")):
            code, out = self.run_cli("--in-session", "s1", "answers")
        self.assertEqual(code, 0)
        self.assertIn("could not refresh the index (database is locked)", out)
        self.assertIn("A: Hooks only add context.", out)

    def test_problems_go_to_stdout_with_exit_0_so_a_skill_is_not_aborted(self):
        self.transcript.unlink()
        code, out = self.run_cli("--in-session", "nope", "answers")
        self.assertEqual(code, 0)
        self.assertIn("no other session of this project is indexed", out)  # nothing said here, nothing else to offer
        code, out = self.run_cli("--in-session", "nope", "find")
        self.assertEqual(code, 0)
        self.assertIn("Name the words", out)


if __name__ == "__main__":
    unittest.main()
