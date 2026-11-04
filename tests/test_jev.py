"""Jev at indexing: parts, paragraphs, what is asked, and how it is shown.

Jev itself is replaced by a fake that answers by rule, so these tests check
focused's side: what it asks, what it keeps, and what it shows.
"""

import io
import json
import os
import sys
import time
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import isolate  # noqa: E402,F401  (before any test runs: none reaches a real service)

from test_focused import SESSION, Fixture, assistant, text, user  # noqa: E402

from focused import cli, jev, store  # noqa: E402
from focused.parts import message_parts, paragraphs, place  # noqa: E402


class FakeJev:
    """Part 1 asks and expects an answer; part 2 states a fact. A paragraph answers a
    part when it contains that part's first word, which keeps the rule visible."""

    def __init__(self):
        self.requests = []

    def __call__(self, state, questions, deadline=None):
        self.requests.append((state, questions))
        answers = {}
        for name, question in questions.items():
            if question["type"] == "noul":
                kind, part = name.rsplit("_", 1)
                yes = (kind == "question") if part == "p1" else (kind == "information")
                answers[name] = {"type": "noul", "noul": 0.9 if yes else 0.1}
            else:
                pid = name.split("_", 1)[1]
                paragraph = next(p for p in state["paragraphs"] if p["id"] == pid)
                hit = state["part"].split()[0].lower().strip("?.") in paragraph["text"].lower()
                answers[name] = {"type": "choice", "choice": "answers" if hit else "not",
                                 "probabilities": {"answers": 0.8 if hit else 0.05, "partly": 0.1, "not": 0.1 if hit else 0.85}}
        return answers


class PartsTest(unittest.TestCase):
    def test_message_parts(self):
        prompt = ("Now two things: 1. You fill it. Can this run before any turn? A hook?\n"
                  "keep it\n\n(sent while Claude was working) ❯ /plugin install x\n  ⎿  not found")
        self.assertEqual(message_parts(prompt), [
            ("Now two things: 1. You fill it.", False),
            ("Can this run before any turn? A hook?", False),
            ("keep it", False),
            ("❯ /plugin install x\n  ⎿  not found", True),
        ])

    def test_paragraphs(self):
        reply = "Intro line\nstill intro\n\n- one\n  - sub\n- two\n\n```\ncode\n\nmore\n```\nAfter."
        self.assertEqual(paragraphs(reply), ["Intro line\nstill intro", "- one\n  - sub", "- two",
                                             "```\ncode\n\nmore\n```", "After."])

    def test_place(self):
        self.assertEqual(place("anything", True), "reply to the message")
        self.assertEqual(place("<task-notification><summary>CI green</summary></task-notification>", False),
                         "after a wake-up (CI green)")
        self.assertEqual(place("Stop hook feedback:\nmore", False), "after a wake-up (Stop hook feedback:)")


class WordingTest(unittest.TestCase):
    def test_the_measured_wording(self):
        [(state, questions)] = jev.kinds_requests(["keep it and merge it"])
        self.assertEqual(sorted(questions), ["decision_p1", "information_p1", "instruction_p1", "question_p1"])
        self.assertEqual(questions["decision_p1"]["instructions"],
                         "Does the part with id p1 in state.parts settle a choice that was open, such as one "
                         "Claude asked the person about? (See state.policy.)")
        self.assertEqual(jev.tags({"question": 0.4, "decision": 0.6, "instruction": 0.7}), ["decision", "instruction"])
        self.assertEqual(jev.tags({"question": 0.2}), ["other"])

    def test_a_long_turn_is_read_in_windows_and_asked_in_batches(self):
        paras = [(f"k{i}", "reply to the message", "x" * 100) for i in range(90)]
        with mock.patch.object(jev, "MAX_CHARS", 5000), mock.patch.object(jev, "MAX_QUESTIONS", 10):
            requests = jev.answer_requests(["a part"], 1, paras, {key for key, _, _ in paras})
        asked = [key for _, _, names in requests for key in names.values()]
        self.assertEqual(sorted(asked), sorted(key for key, _, _ in paras))
        self.assertTrue(all(len(q) <= 10 for _, q, _ in requests))
        self.assertTrue(all(len(json.dumps(state)) <= 5000 + 1000 for state, _, _ in requests))

    def test_an_incomplete_answer_is_an_error(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"answers": {"question_p1": {"type": "noul"}}}).encode()
        [(state, questions)] = jev.kinds_requests(["a"])
        with mock.patch.dict(os.environ, {"FOCUSED_OPENROUTER_API_KEY": "k"}), \
                mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(jev.JevError):
                jev.http_ask(state, {"question_p1": questions["question_p1"]})

    def test_every_template_is_in_the_wording_digest(self):
        for template in (jev.KIND_QUESTION, jev.KIND_TRUE, jev.KIND_FALSE, jev.ANSWER_QUESTION, jev.ANSWER_OPTIONS):
            self.assertIn(template, jev.WORDED)

    def test_kinds_are_asked_in_batches_and_a_long_message_is_shortened(self):
        parts = [f"part number {n}." for n in range(1, 13)]
        requests = jev.kinds_requests(parts)
        self.assertEqual([len(q) for _, q in requests], [40, 8])
        self.assertEqual(len(requests[1][0]["parts"]), 12)  # all in view while they fit
        with mock.patch.object(jev, "MAX_MESSAGE", 20):
            [(state, _)] = jev.kinds_requests(["x" * 30])
            self.assertEqual(state["message"], "x" * 20 + jev.MESSAGE_CUT)

    def test_no_request_starts_after_the_deadline(self):
        [(state, questions)] = jev.kinds_requests(["a"])
        with mock.patch.dict(os.environ, {"FOCUSED_OPENROUTER_API_KEY": "k"}), \
                mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(jev.JevTimeout):
                jev.http_ask(state, questions, deadline=time.monotonic() - 1)
        urlopen.assert_not_called()

    def test_off_unless_switched_on_with_a_key(self):
        with mock.patch.dict(os.environ, {"FOCUSED_JEV": "", "OPENROUTER_API_KEY": "k"}):
            self.assertFalse(jev.enabled())
        with mock.patch.dict(os.environ, {"FOCUSED_JEV": "1"}, clear=True):
            self.assertFalse(jev.enabled())
        with mock.patch.dict(os.environ, {"FOCUSED_JEV": "1", "OPENROUTER_API_KEY": "k"}):
            self.assertTrue(jev.enabled())


class JevIndexTest(Fixture):
    def count(self, conn, table):
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_parts_and_paragraphs_are_indexed(self):
        conn = self.index()
        parts = conn.execute("SELECT turn_key, n, text, queued FROM parts ORDER BY turn_key, n").fetchall()
        self.assertIn(("u1", 1, "How do hooks work?", 0), [tuple(p) for p in parts])
        self.assertIn(("u2", 1, "also this", 1), [tuple(p) for p in parts])  # the image prompt has no text
        places = [tuple(r) for r in conn.execute("SELECT text, place FROM paras WHERE turn_key = 'u1' ORDER BY seq")]
        self.assertEqual(places, [
            ("Looking.", "reply to the message"),
            ("Hooks only add context.", "reply to the message"),
            ("No action needed.", "after a wake-up (<task-notification>CI green</task-notification>)"),
            ("Deleted the cache folder.", "after a wake-up (Stop hook feedback: untracked files)"),
        ])
        self.index()
        self.assertEqual(self.count(conn, "paras"), len(places) + 2)  # u2: two texts

    def test_jev_is_asked_once_and_its_answers_shown(self):
        conn = self.index()
        fake = FakeJev()
        done = store.ask_jev(conn, "s1", ask=fake)
        self.assertTrue(done["complete"])
        first = len(fake.requests)
        self.assertGreater(first, 0)
        self.assertEqual(store.ask_jev(conn, "s1", ask=fake)["requests"], 0)

        views = store.turn_views(conn, "s1")
        u1 = views["u1"]
        self.assertTrue(u1["complete"])
        self.assertEqual([p["tags"] for p in u1["parts"]], [["question"], ["information"]])
        self.assertEqual([a["text"] for a in u1["answers"][1]], [])  # "how" is in no paragraph
        out = cli.format_turns(store.session_turns(conn, "s1"), views=views)
        self.assertIn("You [question]: How do hooks work?\n  → (no answer found)", out)
        self.assertIn("You [information]: Second line", out)
        self.assertNotIn("A: Hooks only add context.", out)

    def test_a_wake_up_paragraph_can_be_the_answer(self):
        self.write([user("u1", "Action status?", origin={"kind": "human"}),
                    assistant(text("Looking.")),
                    user("w1", "<task-notification><summary>CI green</summary></task-notification>",
                         ts="2026-11-03T06:05:00.000Z", origin={"kind": "task-notification"}),
                    assistant(text("Action passed on the second run."))])
        conn = self.index()
        store.ask_jev(conn, "s1", ask=FakeJev())
        out = cli.format_turns(store.session_turns(conn, "s1"), views=store.turn_views(conn, "s1"))
        self.assertIn("You [question]: Action status?\n  → Action passed on the second run. (after a wake-up, 06:05)", out)

    def test_a_turn_jev_has_not_read_shows_as_before(self):
        conn = self.index()
        out = cli.format_turns(store.session_turns(conn, "s1"), views=store.turn_views(conn, "s1"))
        self.assertIn("You: How do hooks work?", out)
        self.assertIn("A: Hooks only add context.", out)

    def test_a_spent_budget_stops_and_the_next_run_goes_on(self):
        conn = self.index()
        ticks = iter(range(0, 1000, 10))
        done = store.ask_jev(conn, "s1", ask=FakeJev(), budget=15, clock=lambda: next(ticks))
        self.assertFalse(done["complete"])
        rest = store.ask_jev(conn, "s1", ask=FakeJev())
        self.assertTrue(rest["complete"])
        self.assertGreater(rest["requests"], 0)

    def test_a_turn_jev_fails_on_does_not_hold_up_the_others(self):
        conn = self.index()
        fake = FakeJev()

        def flaky(state, questions, deadline=None):
            if "a prompt with no origin" in state.get("message", ""):  # the newest turn, read first
                raise jev.JevError("HTTP 400: too large")
            return fake(state, questions)

        done = store.ask_jev(conn, "s1", ask=flaky)
        self.assertFalse(done["complete"])
        self.assertEqual(len(done["errors"]), 1)
        self.assertIn("HTTP 400", done["errors"][0])
        views = store.turn_views(conn, "s1")
        self.assertTrue(views["u2"]["complete"])
        self.assertTrue(views["u1"]["complete"])
        self.assertIsNone(conn.execute("SELECT kinds FROM parts WHERE turn_key = 'old'").fetchone()[0])

    def test_running_out_of_time_mid_request_ends_the_run(self):
        conn = self.index()

        def slow(state, questions, deadline=None):
            raise jev.JevTimeout("out of time")

        done = store.ask_jev(conn, "s1", ask=slow)
        self.assertEqual((done["complete"], done["errors"]), (False, []))

    def test_paragraphs_keep_their_order_when_one_is_added_before_others(self):
        self.index()
        grown = list(SESSION)
        at = next(i for i, e in enumerate(grown) if e.get("uuid") == "w1")
        grown.insert(at, assistant(text("Also this.")))
        self.write(grown)
        conn = self.index()
        order = [r[0] for r in conn.execute("SELECT text FROM paras WHERE turn_key = 'u1' ORDER BY seq")]
        self.assertEqual(order, ["Looking.", "Hooks only add context.", "Also this.", "No action needed.",
                                 "Deleted the cache folder."])

    def test_new_wording_is_asked_again(self):
        conn = self.index()
        store.ask_jev(conn, "s1", ask=FakeJev())
        with mock.patch.object(jev, "WORDING", "reworded"):
            self.assertFalse(store.turn_views(conn, "s1")["u1"]["complete"])
            self.assertGreater(store.ask_jev(conn, "s1", ask=FakeJev())["requests"], 0)

    def test_a_message_that_grows_is_asked_about_again(self):
        conn = self.index()
        store.ask_jev(conn, "s1", ask=FakeJev())
        before = conn.execute("SELECT count(*) FROM pairs WHERE turn_key = 'u1' AND n = 1").fetchone()[0]
        grown = [dict(e) for e in SESSION]
        grown[0] = user("u1", "How do hooks work?\nSecond line\nThird line", origin={"kind": "human"}, turnOrigin="human")
        self.write(grown)
        self.index()
        self.assertFalse(store.turn_views(conn, "s1")["u1"]["complete"])
        self.assertEqual(conn.execute("SELECT count(*) FROM pairs WHERE turn_key = 'u1' AND n = 1").fetchone()[0], before)
        store.ask_jev(conn, "s1", ask=FakeJev())
        self.assertEqual(len(store.turn_views(conn, "s1")["u1"]["parts"]), 3)

    def test_retention_takes_parts_paragraphs_and_answers_too(self):
        conn = self.index()
        store.ask_jev(conn, "s1", ask=FakeJev())
        from datetime import datetime, timezone
        self.now = datetime(2026, 12, 20, tzinfo=timezone.utc)
        self.index()
        self.assertEqual([self.count(conn, t) for t in ("parts", "paras", "pairs")], [0, 0, 0])

    def test_find_searches_paragraphs_wake_ups_included(self):
        conn = self.index()
        hits = store.search_paras(conn, ["action"])
        self.assertEqual([(h["turn_key"], h["hit"]) for h in hits], [("u1", "No [action] needed.")])


class HookTest(Fixture):
    def run_hook(self, env):
        payload = json.dumps({"transcript_path": str(self.transcript), "last_assistant_message": "Both done."})
        with mock.patch.dict(os.environ, env), mock.patch("sys.stdin", io.StringIO(payload)), \
                mock.patch.object(cli.time, "sleep"), redirect_stdout(io.StringIO()) as out:
            code = cli.main(["hook", "stop"])
        return code, out.getvalue()

    def test_the_hook_asks_jev_only_when_switched_on(self):
        with mock.patch.object(store, "ask_jev") as ask:
            self.run_hook({"FOCUSED_JEV": "", "OPENROUTER_API_KEY": "k"})
            ask.assert_not_called()
            self.run_hook({"FOCUSED_JEV": "1", "OPENROUTER_API_KEY": "k"})
            ask.assert_called_once()

    def test_a_jev_failure_is_logged_and_the_index_kept(self):
        with mock.patch.object(store, "ask_jev", side_effect=jev.JevError("HTTP 503")):
            code, out = self.run_hook({"FOCUSED_JEV": "1", "OPENROUTER_API_KEY": "k"})
        self.assertEqual((code, out), (0, ""))
        log = (Path(os.environ["FOCUSED_HOME"]) / "errors.log").read_text()
        self.assertIn("jev", log)
        self.assertIn("HTTP 503", log)
        with closing(store.connect(store.db_for_transcript(self.transcript))) as conn:
            self.assertGreater(conn.execute("SELECT count(*) FROM parts").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
