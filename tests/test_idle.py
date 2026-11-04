"""The stop after an hour without a reply (focused/idle.py) and its hook."""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import isolate  # noqa: E402,F401  (before any test runs: none reaches a real service)

from test_focused import Fixture, assistant, text, tool, user  # noqa: E402

from focused import cli, idle  # noqa: E402

HUMAN = {"origin": {"kind": "human"}, "turnOrigin": "human"}
REPLIED = "2026-11-03T10:00:00.000Z"


def reply(value, ts=REPLIED, tokens=150_000, **extra):
    entry = assistant(text(value))
    entry["timestamp"] = ts
    entry["message"]["usage"] = {"input_tokens": 2, "cache_creation_input_tokens": 1_000,
                                 "cache_read_input_tokens": tokens - 1_200, "output_tokens": 198}
    entry.update(extra)
    return entry


def later(minutes: float) -> float:
    return (datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)).timestamp()


class IdleTest(Fixture):
    def setUp(self):
        super().setUp()
        self.write([user("u1", "Build it.", ts="2026-11-03T09:00:00.000Z", **HUMAN), reply("Built.")])

    def payload(self, prompt="And now the docs, please."):
        return {"session_id": "s1", "transcript_path": str(self.transcript), "prompt": prompt,
                "hook_event_name": "UserPromptSubmit"}

    def test_a_typed_message_after_an_hour_is_stopped_once(self):
        reason = idle.check(self.payload(), now=later(72))
        self.assertIn("no reply for 1 h 12 min", reason)
        self.assertIn("150,000 tokens", reason)
        self.assertIn("Run /clear", reason)
        self.assertIn("send again", reason)
        self.assertIsNone(idle.check(self.payload(), now=later(73)))  # sent again: it goes through
        self.assertIsNone(idle.check(self.payload("Something else."), now=later(80)))

    def test_a_new_idle_spell_stops_again(self):
        self.assertIsNotNone(idle.check(self.payload(), now=later(72)))
        self.write([user("u1", "Build it.", **HUMAN), reply("Built."),
                    user("u2", "And now the docs, please.", **HUMAN), reply("Done.", ts="2026-11-03T11:13:00.000Z")])
        self.assertIsNone(idle.check(self.payload(), now=later(73 + 30)))
        self.assertIn("no reply for 59 min", idle.check(self.payload(), now=later(73 + 59)))

    def test_under_59_minutes_or_a_small_session_goes_through(self):
        self.assertIsNone(idle.check(self.payload(), now=later(58.9)))
        self.write([user("u1", "Build it.", **HUMAN), reply("Built.", tokens=99_000)])
        self.assertIsNone(idle.check(self.payload(), now=later(300)))

    def test_turns_claude_code_starts_itself_go_through(self):
        for prompt in ("<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>",
                       '  <wake reason="external-event"><event source="github">',
                       "<cross-session-message from=\"x\">hello</cross-session-message>"):
            self.assertIsNone(idle.check(self.payload(prompt), now=later(90)), prompt)

    def test_a_scheduled_prompt_goes_through(self):
        scheduling = assistant(tool("t1", "CronCreate", cron="0 * * * *", prompt="Check the deploy."))
        self.write([user("u1", "Build it.", **HUMAN), scheduling, reply("Built.")])
        self.assertIsNone(idle.check(self.payload("Check the deploy."), now=later(90)))
        self.assertIsNotNone(idle.check(self.payload("Check the deploy, please."), now=later(90)))

    def test_a_loop_with_a_placeholder_prompt_is_never_stopped(self):
        waking = assistant(tool("t1", "ScheduleWakeup", delaySeconds=3600, prompt="<<autonomous-loop-dynamic>>"))
        self.write([user("u1", "Build it.", **HUMAN), waking, reply("Built.")])
        self.assertIsNone(idle.check(self.payload("Keep going with the backlog."), now=later(90)))

    def test_the_last_reply_skips_side_chains_and_synthetic_messages(self):
        self.write([user("u1", "Build it.", **HUMAN), reply("Built."),
                    reply("A subagent.", ts="2026-11-03T11:00:00.000Z", isSidechain=True),
                    reply("API Error", ts="2026-11-03T11:00:00.000Z", tokens=0)])
        self.assertEqual(idle.last_reply(self.transcript), (REPLIED, 150_000))

    def test_the_last_reply_is_found_far_from_the_end(self):
        filler = [user(f"f{n}", "x" * 1_000, **HUMAN) for n in range(3_000)]  # 3 MB without a reply
        self.write([user("u1", "Build it.", **HUMAN), reply("Built.")] + filler)
        self.assertEqual(idle.last_reply(self.transcript), (REPLIED, 150_000))

    def test_switched_off_or_tuned_in_the_environment(self):
        with mock.patch.dict(os.environ, {"FOCUSED_IDLE_MINUTES": "0"}):
            self.assertIsNone(idle.check(self.payload(), now=later(300)))
        with mock.patch.dict(os.environ, {"FOCUSED_IDLE_MINUTES": "5", "FOCUSED_IDLE_TOKENS": "200000"}):
            self.assertIsNone(idle.check(self.payload(), now=later(300)))
        with mock.patch.dict(os.environ, {"FOCUSED_IDLE_MINUTES": "5"}):
            self.assertIn("no reply for 6 min", idle.check(self.payload(), now=later(6)))

    def test_nothing_to_go_on_goes_through(self):
        self.assertIsNone(idle.check({"prompt": "Hi", "session_id": "s1", "transcript_path": str(self.dir / "none.jsonl")}))
        self.assertIsNone(idle.check({"prompt": "Hi", "session_id": "../x", "transcript_path": str(self.transcript)},
                                     now=later(90)))
        self.write([user("u1", "Build it.", **HUMAN)])
        self.assertIsNone(idle.check(self.payload(), now=later(90)))

    def run_hook(self, payload: str):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stdin", io.StringIO(payload)):
            code = cli.main(["hook", "prompt"])
        return code, out.getvalue()

    def test_the_hook_blocks_with_json_and_is_silent_otherwise(self):
        with mock.patch("time.time", return_value=later(72)):
            code, out = self.run_hook(json.dumps(self.payload()))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["decision"], "block")
            self.assertIn("Run /clear", json.loads(out)["reason"])
            self.assertEqual(self.run_hook(json.dumps(self.payload())), (0, ""))

    def test_the_hook_fails_open(self):
        self.assertEqual(self.run_hook("not json"), (0, ""))
        self.assertIn("hook prompt", (self.dir / "focused" / "errors.log").read_text())


if __name__ == "__main__":
    unittest.main()
