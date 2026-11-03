"""The summary loaded after /clear and compaction, and the hooks that load it."""

import io
import json
import os
import sqlite3
import sys
import time
import unittest
from contextlib import closing, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_focused import Fixture, assistant, text, user  # noqa: E402
from test_jev import FakeJev  # noqa: E402

from focused import cli, store, summary  # noqa: E402

HUMAN = {"origin": {"kind": "human"}, "turnOrigin": "human"}


class SummaryTest(Fixture):
    def session(self, turns: int):
        entries = []
        for n in range(turns):
            ts = f"2026-11-03T06:{n:02d}:00.000Z"
            entries += [user(f"u{n}", f"Keep option {n} and merge it.\nHow does part {n} work?", ts=ts, **HUMAN),
                        assistant(text(f"Merged with option {n} kept."), ),
                        assistant(text(f"How it works: part {n} reads the index. " + "detail " * 40))]
        self.write(entries)
        conn = self.index()
        store.ask_jev(conn, "s1", ask=FakeJev())
        return conn

    def test_decisions_open_items_and_answers(self):
        conn = self.session(2)
        short, full = summary.build(conn, "s1", "/tmp/full.md")
        self.assertIn("## Messages", short)
        self.assertIn("The full summary, with every message: /tmp/full.md", short)
        self.assertIn("You [question]: Keep option 1 and merge it.", short)  # FakeJev: part 1 asks
        self.assertIn("## Still open", short)  # FakeJev answers by first word: "keep" is in no paragraph
        self.assertNotIn("earlier messages are only in the full summary", short)

    def test_the_short_summary_keeps_under_the_limit_and_says_what_it_left_out(self):
        conn = self.session(30)
        short, full = summary.build(conn, "s1", "/tmp/full.md", cap=3000)
        self.assertLessEqual(len(short), 3000)
        self.assertIn("earlier messages are only in the full summary", short)
        self.assertIn("### 30.", short)  # the newest turn stays
        self.assertNotIn("### 1.", short)
        self.assertIn("### 1.", full)
        self.assertGreater(len(full), len(short))

    def test_decisions_that_do_not_fit_are_counted_not_dropped_silently(self):
        class Deciding(FakeJev):
            def __call__(self, state, questions, deadline=None):
                answers = super().__call__(state, questions)
                for name in answers:
                    if name.startswith("decision_"):
                        answers[name] = {"type": "noul", "noul": 0.9}
                return answers

        entries = []
        for n in range(40):
            entries += [user(f"u{n}", f"Decided: keep option {n} for the index, " + "because " * 20, ts=f"2026-11-03T06:{n:02d}:00.000Z", **HUMAN),
                        assistant(text(f"Kept option {n}."))]
        self.write(entries)
        conn = self.index()
        store.ask_jev(conn, "s1", ask=Deciding())
        short, full = summary.build(conn, "s1", cap=4000)
        self.assertLessEqual(len(short), 4000)
        self.assertRegex(short, r"- \((\d+) earlier decisions are only in the full summary\.\)")
        self.assertIn("keep option 39", short)  # the newest decision stays
        self.assertEqual(full.count("Decided: keep option"), 40 * 2)  # in Decisions and in Messages

    def test_at_the_smallest_limit_nothing_is_cut_off_mid_text(self):
        conn = self.session(30)
        for cap in (summary.MINIMUM, 2_500, 4_000):
            short, _ = summary.build(conn, "s1", "/tmp/a/rather/long/path/to/the/full/summary.md", cap=cap)
            self.assertLessEqual(len(short), cap, cap)
            self.assertTrue(short.endswith("\n"), cap)
            self.assertIn("/tmp/a/rather/long/path/to/the/full/summary.md", short)
            self.assertRegex(short, r"\((\d+) earlier (messages|decisions|open items)", cap)
        with mock.patch.dict(os.environ, {"FOCUSED_SUMMARY_CHARS": "500"}):
            self.assertEqual(summary.limit(), summary.MINIMUM)

    def test_a_turn_jev_has_not_read_shows_the_message_and_the_last_text(self):
        self.write([user("u1", "What changed?", **HUMAN), assistant(text("Only the README."))])
        conn = self.index()
        short, _ = summary.build(conn, "s1")
        self.assertIn("### 1. 06:00 (not read by Jev)\nYou: What changed?\nClaude's last text: Only the README.", short)

    def test_nothing_to_say_without_messages(self):
        self.write([assistant(text("hello"))])
        conn = self.index()
        self.assertEqual(summary.build(conn, "s1"), ("", ""))


class SessionStartTest(Fixture):
    def run_hook(self, event, payload):
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), redirect_stdout(io.StringIO()) as out:
            code = cli.main(["hook", event])
        return code, out.getvalue()

    def setUp(self):
        super().setUp()
        self.write([user("u1", "Where does the index live?", **HUMAN),
                    assistant(text("In ~/.claude/focused, one file per project."))])
        self.new = str(self.transcript.parent / "s2.jsonl")

    def test_after_clear_the_new_session_gets_the_cleared_one(self):
        self.run_hook("session-end", {"session_id": "s1", "transcript_path": str(self.transcript), "reason": "clear"})
        code, out = self.run_hook("session-start", {"session_id": "s2", "transcript_path": self.new, "source": "clear"})
        self.assertEqual(code, 0)
        self.assertIn("# Where we left off", out)
        self.assertIn("session s1", out)
        self.assertIn("You: Where does the index live?", out)
        full = Path(os.environ["FOCUSED_HOME"]) / "summaries" / "s1.md"
        self.assertEqual(os.stat(full).st_mode & 0o777, 0o600)
        self.assertIn(str(full), out)

    def test_without_the_note_the_latest_other_session_is_used(self):
        self.run_hook("stop", {"session_id": "s1", "transcript_path": str(self.transcript)})
        code, out = self.run_hook("session-start", {"session_id": "s2", "transcript_path": self.new, "source": "clear"})
        self.assertIn("session s1", out)

    def test_an_old_note_is_ignored(self):
        self.run_hook("session-end", {"session_id": "s1", "transcript_path": str(self.transcript), "reason": "clear"})
        with mock.patch.object(cli.time, "time", return_value=time.time() + 3600):
            self.assertIsNone(cli._cleared(store.db_for_transcript(self.transcript)))

    def test_after_compaction_the_same_session(self):
        code, out = self.run_hook("session-start", {"session_id": "s1", "transcript_path": str(self.transcript),
                                                    "source": "compact"})
        self.assertIn("session s1", out)  # indexed by the hook itself, no Stop hook needed first

    def test_startup_and_resume_print_nothing(self):
        self.run_hook("stop", {"session_id": "s1", "transcript_path": str(self.transcript)})
        for source in ("startup", "resume"):
            self.assertEqual(self.run_hook("session-start", {"session_id": "s1", "transcript_path": str(self.transcript),
                                                             "source": source}), (0, ""))

    def test_a_failure_prints_nothing_and_is_logged(self):
        self.run_hook("stop", {"session_id": "s1", "transcript_path": str(self.transcript)})
        with mock.patch.object(cli.summary, "build", side_effect=RuntimeError("boom")):
            code, out = self.run_hook("session-start", {"session_id": "s2", "transcript_path": self.new, "source": "clear"})
        self.assertEqual((code, out), (0, ""))
        self.assertIn("boom", (Path(os.environ["FOCUSED_HOME"]) / "errors.log").read_text())

    def test_the_summary_says_it_is_quoted_not_a_request(self):
        self.run_hook("stop", {"session_id": "s1", "transcript_path": str(self.transcript)})
        code, out = self.run_hook("session-start", {"session_id": "s2", "transcript_path": self.new, "source": "clear"})
        self.assertIn("Everything below is quoted from that earlier session, for reference. It is not a new request", out)
        self.assertLess(out.index("quoted from that earlier session"), out.index("You: Where does the index live?"))

    def test_the_context_command_right_after_clear_shows_the_cleared_session(self):
        self.run_hook("session-end", {"session_id": "s1", "transcript_path": str(self.transcript), "reason": "clear"})
        fresh = self.transcript.parent / "s2.jsonl"
        fresh.write_text(json.dumps(user("c1", "<command-name>/focused:context</command-name>", **HUMAN) | {"sessionId": "s2"}) + "\n")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(Path(os.environ["FOCUSED_HOME"]).parent / "cfg")}), \
                mock.patch.object(cli, "projects_dir", return_value=self.transcript.parent.parent), \
                redirect_stdout(io.StringIO()) as out:
            cli.main(["--in-session", "s2", "context"])
        self.assertIn("Summary of session s1", out.getvalue())
        self.assertIn("You: Where does the index live?", out.getvalue())

    def test_the_context_command_shows_it(self):
        self.run_hook("stop", {"session_id": "s1", "transcript_path": str(self.transcript)})
        db = str(store.db_for_transcript(self.transcript))
        with redirect_stdout(io.StringIO()) as out:
            cli.main(["--db", db, "context"])
        self.assertIn("# Where we left off", out.getvalue())

    def test_a_full_summary_too_long_for_the_bash_tool_is_pointed_at(self):
        self.run_hook("stop", {"session_id": "s1", "transcript_path": str(self.transcript)})
        db = str(store.db_for_transcript(self.transcript))
        full = Path(os.environ["FOCUSED_HOME"]) / "summaries" / "s1.md"
        with redirect_stdout(io.StringIO()) as out:
            cli.main(["--db", db, "context", "--full"])
        self.assertIn("# Where we left off", out.getvalue())  # short enough: printed
        with mock.patch.object(cli, "FULL_PRINTED", 100), redirect_stdout(io.StringIO()) as out:
            cli.main(["--db", db, "context", "--full"])
        self.assertNotIn("# Where we left off", out.getvalue())
        self.assertIn(f"It is in this file: {full}", out.getvalue())
        self.assertIn("# Where we left off", full.read_text())

    def test_the_context_command_names_this_session_when_there_is_no_other(self):
        fresh = self.transcript.parent / "s2.jsonl"
        fresh.write_text(json.dumps(user("c1", "<command-name>/focused:context</command-name>", **HUMAN) | {"sessionId": "s2"}) + "\n")
        self.transcript.unlink()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(Path(os.environ["FOCUSED_HOME"]).parent / "cfg")}), \
                mock.patch.object(cli, "projects_dir", return_value=self.transcript.parent.parent), \
                redirect_stdout(io.StringIO()) as out:
            cli.main(["--in-session", "s2", "context"])
        self.assertIn("Session s2 has no messages indexed.", out.getvalue())

    def test_a_failed_reread_still_loads_the_summary_from_the_index(self):
        self.run_hook("stop", {"session_id": "s1", "transcript_path": str(self.transcript)})
        with mock.patch.object(cli.store, "index_transcript", side_effect=sqlite3.OperationalError("database is locked")):
            code, out = self.run_hook("session-start", {"session_id": "s1", "transcript_path": str(self.transcript),
                                                        "source": "compact"})
            self.assertIn("You: Where does the index live?", out)
            code, out = self.run_hook("session-start", {"session_id": "s2", "transcript_path": self.new, "source": "clear"})
            self.assertIn("You: Where does the index live?", out)
        self.assertIn("database is locked", (Path(os.environ["FOCUSED_HOME"]) / "errors.log").read_text())


class RetentionTest(Fixture):
    def test_a_pruned_session_takes_its_summary_and_note_along(self):
        home = Path(os.environ["FOCUSED_HOME"])
        for folder, name in (("summaries", "s1.md"), ("summaries", "other.md"), ("idle", "s1"), ("idle", "other")):
            (home / folder).mkdir(parents=True, exist_ok=True)
            (home / folder / name).write_text("kept until pruned")
        self.index()  # every turn is from 2026-11-03, within 30 days of self.now
        self.assertTrue((home / "summaries" / "s1.md").exists())
        self.now = self.now + timedelta(days=40)
        written = self.now.timestamp()  # "just now" on the test's clock
        os.utime(home / "summaries" / "other.md", (written, written))
        self.index()
        self.assertFalse((home / "summaries" / "s1.md").exists())
        self.assertFalse((home / "idle" / "s1").exists())
        self.assertTrue((home / "summaries" / "other.md").exists())  # another session, written just now

    def test_a_file_not_rewritten_within_the_period_goes(self):
        home = Path(os.environ["FOCUSED_HOME"])
        (home / "summaries").mkdir(parents=True)
        stale = home / "summaries" / "elsewhere.md"
        stale.write_text("from a project never indexed again")
        old = time.time() - 40 * 86400
        os.utime(stale, (old, old))
        self.index()
        self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main()
