"""Steward sessions (focused/steward.py): the hook after a pull request is opened, and the command."""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: E402,F401  (before any test runs: none reaches a real service)

from focused import cli, install, steward  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
URL = "https://github.com/nicolaszimmer/focused-context/pull/14"


def opened(head="claude/feature", url=URL, owner="nicolaszimmer", repo="focused-context",
           tool="mcp__github__create_pull_request"):
    """A PostToolUse payload as Claude Code sends it after the GitHub tool opens a pull request."""
    return {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": tool,
            "tool_input": {"owner": owner, "repo": repo, "head": head, "base": "main", "title": "x"},
            "tool_response": [{"type": "text", "text": json.dumps({"id": "1", "url": url})}]}


def hook(payload, **env):
    with mock.patch.dict(os.environ, {"CLAUDE_CODE_REMOTE": "true", **env}), \
            mock.patch("sys.stdin", io.StringIO(json.dumps(payload) if not isinstance(payload, str) else payload)), \
            redirect_stdout(io.StringIO()) as out:
        code = cli.main(["hook", "pr-opened"])
    return code, out.getvalue()


class StewardTest(unittest.TestCase):
    def test_a_pull_request_opened_in_a_cloud_session_is_handed_to_a_steward(self):
        code, out = hook(opened())
        self.assertEqual(code, 0)
        context = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(context["hookEventName"], "PostToolUse")
        text = context["additionalContext"]
        self.assertIn("don't call subscribe_pr_activity", text)
        arguments = json.loads(text[text.index("{"):])
        self.assertEqual(arguments["model"], "claude-sonnet-5-5")
        self.assertEqual((arguments["source_url"], arguments["source_revision"], arguments["outcome_branch"]),
                         ("https://github.com/nicolaszimmer/focused-context", "claude/feature", "claude/feature"))
        self.assertEqual(arguments["title"], "Steward: nicolaszimmer/focused-context#14")
        self.assertEqual(arguments["tags"], ["focused-steward"])
        brief = arguments["prompt"]
        self.assertIn("subscribe_pr_activity for nicolaszimmer/focused-context #14", brief)
        self.assertIn("Push to `claude/feature` only", brief)
        self.assertIn("Never merge", brief)
        self.assertIn("or else its tests", brief)  # a repository without a CLAUDE.md
        self.assertIn("Waiting on you:", brief)
        self.assertNotIn("permission_mode", arguments)  # the steward inherits this session's, never "plan"

    def test_nothing_is_handed_over_outside_the_cloud_or_switched_off(self):
        for env in ({"CLAUDE_CODE_REMOTE": ""}, {"FOCUSED_STEWARD": "0"}):
            self.assertEqual(hook(opened(), **env), (0, ""))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_REMOTE": "true", "FOCUSED_STEWARD_MODEL": "claude-haiku-5-5"}):
            self.assertEqual(steward.from_hook(opened())["model"], "claude-haiku-5-5")

    def test_only_a_pull_request_the_steward_can_push_to(self):
        for payload in (opened(head="someone:feature"),  # a fork's branch
                        opened(head="../main"), opened(head="-x"),
                        opened(url="https://example.com/x"),  # no pull request in the result
                        opened(owner="other"),  # the result names another repository
                        opened(tool="mcp__github__create_branch")):
            self.assertIsNone(steward.from_hook(payload), payload)
            self.assertEqual(hook(payload), (0, ""))

    def test_the_hook_fails_open(self):
        self.assertEqual(hook("{ not json"), (0, ""))
        self.assertEqual(hook({"tool_name": "mcp__github__create_pull_request", "tool_input": "x"}), (0, ""))

    def test_the_command_gives_the_same_arguments_for_a_pull_request_already_open(self):
        with redirect_stdout(io.StringIO()) as out:
            code = cli.main(["steward", URL, "--head", "claude/feature"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertEqual(json.loads(text[text.index("{"):]), steward.from_hook(opened()))
        for argv, says in ((["steward", URL], "--head"), (["steward", "not a url", "--head", "x"], "isn't a pull request"),
                           (["steward", URL, "--head", "a:b"], "can push to")):
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["--in-session", "s1", *argv]), 0)  # a skill shows the problem
            self.assertIn(says, out.getvalue())

    def test_installed_for_cloud_environments_and_the_plugin(self):
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]["PostToolUse"]
        self.assertEqual(hooks[0]["matcher"], "mcp__github__create_pull_request")
        self.assertTrue(hooks[0]["hooks"][0]["command"].endswith(" hook pr-opened"))
        installed = install._hooks(ROOT, jev=False)["PostToolUse"]
        self.assertEqual(installed[0]["matcher"], "mcp__github__create_pull_request")
        self.assertIn("hook pr-opened", installed[0]["hooks"][0]["command"])
        self.assertIn("steward", install.SKILLS)
        skill = (ROOT / "skills" / "steward" / "SKILL.md").read_text()
        self.assertIn("disable-model-invocation: true", skill)
        self.assertIn('--in-session "${CLAUDE_SESSION_ID}" steward', skill)


if __name__ == "__main__":
    unittest.main()
