import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from focused import cli, store
from focused.transcript import parse

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
                                                "FOCUSED_RETENTION_DAYS": "30"})
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
                         ["human", "task-notification", "meta", "human", "human", "unknown"])

    def test_wake_up_reply_does_not_replace_the_answer(self):
        first = parse(self.transcript).turns[0]
        self.assertEqual(first.answer, "Hooks only add context.")
        self.assertEqual(first.narration, ["Looking."])

    def test_synthetic_turns_are_filed_under_the_question(self):
        turns = parse(self.transcript).turns
        self.assertEqual([t.parent_key for t in turns[1:3]], ["u1", "u1"])
        self.assertEqual(turns[1].answer, "No action needed.")

    def test_text_followed_by_a_tool_call_is_not_an_answer(self):
        self.assertIsNone(parse(self.transcript).turns[3].answer)

    def test_queued_message_is_a_question(self):
        queued = parse(self.transcript).turns[4]
        self.assertEqual((queued.prompt, queued.answer), ("also this", "Both done."))

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


class StoreTest(Fixture):
    def count(self, conn, sql="SELECT count(*) FROM turns"):
        return conn.execute(sql).fetchone()[0]

    def test_indexing_twice_adds_no_rows(self):
        conn = self.index()
        self.index()
        self.assertEqual(self.count(conn), 6)
        self.assertEqual(self.count(conn, "SELECT count(*) FROM turns_fts WHERE turns_fts MATCH 'hooks'"), 1)

    def test_stop_hook_answer_wins_over_a_lagging_transcript(self):
        conn = self.index(stop_message="The final words.")
        self.index()
        row = conn.execute("SELECT answer, answer_source FROM turns WHERE turn_key = 'old'").fetchone()
        self.assertEqual(tuple(row), ("The final words.", "stop-hook"))

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
        self.assertEqual(self.count(conn), 6)


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
        self.assertIn("bad.jsonl: 1 questions", out)  # the cut-off character no longer stops the read
        self.assertIn("s1.jsonl: 4 questions", out)
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
        self.assertEqual(conn.execute("SELECT count(*) FROM turns").fetchone()[0], 6)

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
        self.assertIn("Q: (no text)", out)
        self.assertIn("origin unknown", out)
        self.assertIn("How do hooks work? [...]", out)

    def test_answers_last_n(self):
        db = str(store.db_for_transcript(self.transcript))
        self.run_cli("--db", db, "index", str(self.transcript))
        _, out = self.run_cli("--db", db, "answers", "-n", "1")
        self.assertIn("## 4.", out)
        self.assertNotIn("Hooks only add context.", out)

    def test_find(self):
        db = str(store.db_for_transcript(self.transcript))
        self.run_cli("--db", db, "index", str(self.transcript))
        code, out = self.run_cli("--db", db, "find", "hooks")
        self.assertEqual(code, 0)
        self.assertIn("[hooks]", out.lower())


if __name__ == "__main__":
    unittest.main()
