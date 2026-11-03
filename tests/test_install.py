"""Installing focused for a cloud environment (focused/install.py, scripts/install-cloud.sh)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_focused import SESSION  # noqa: E402

from focused import install  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.settings = self.dir / "claude" / "settings.json"
        self.skills = self.dir / "claude" / "skills"
        self.settings.parent.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def run_install(self, **kwargs):
        return install.install(ROOT, settings_path=self.settings, skills_dir=self.skills, **kwargs)

    def loaded(self):
        return json.loads(self.settings.read_text())

    def commands(self, event):
        return [h["command"] for g in self.loaded()["hooks"].get(event, []) for h in g["hooks"]]

    def test_installs_hooks_skills_and_the_mark_beside_existing_settings(self):
        theirs = {"type": "command", "command": "~/.claude/stop-hook-git-check.sh"}
        self.settings.write_text(json.dumps({"theme": "dark", "enabledPlugins": {},
                                             "hooks": {"Stop": [{"hooks": [theirs]}]}}))
        self.run_install()
        settings = self.loaded()
        self.assertEqual(settings["theme"], "dark")
        self.assertIn(theirs, settings["hooks"]["Stop"][0]["hooks"])
        for event, word in (("Stop", "stop"), ("SessionEnd", "session-end"),
                            ("SessionStart", "session-start"), ("UserPromptSubmit", "prompt")):
            ours = [c for c in self.commands(event) if install.MARK in c]
            self.assertEqual(len(ours), 1, event)
            self.assertIn(f'python3 "{ROOT}/scripts/focused.py" hook {word}', ours[0])
        self.assertEqual([g.get("matcher") for g in settings["hooks"]["SessionStart"]], ["clear|compact"])
        stop = [h for g in settings["hooks"]["Stop"] for h in g["hooks"] if install.MARK in h["command"]][0]
        self.assertTrue(stop["async"])
        self.assertEqual(settings["env"]["FOCUSED_INSTALL"], "environment")
        for name in install.SKILLS:
            text = (self.skills / f"focused-{name}" / "SKILL.md").read_text()
            self.assertTrue(text.startswith(f"---\nname: focused-{name}\n"))
            self.assertIn(f'python3 "{ROOT}/scripts/focused.py" --in-session', text)
            self.assertNotIn("CLAUDE_PLUGIN_ROOT", text)

    def test_non_ascii_settings_survive_whatever_the_locale(self):
        self.settings.write_text('{"companyAnnouncements": ["Grüße — 日本"]}', encoding="utf-8")
        done = subprocess.run([sys.executable, str(ROOT / "scripts" / "focused.py"), "install"], capture_output=True,
                              text=True, env=dict(os.environ, CLAUDE_CONFIG_DIR=str(self.settings.parent),
                                                  LC_ALL="en_US.ISO-8859-1", LANG="en_US.ISO-8859-1",
                                                  PYTHONUTF8="0", PYTHONCOERCECLOCALE="0"))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8"))["companyAnnouncements"], ["Grüße — 日本"])

    def test_running_again_replaces_rather_than_adds(self):
        self.run_install()
        self.run_install(jev=True)
        stops = [c for c in self.commands("Stop") if install.MARK in c]
        self.assertEqual(len(stops), 1)
        self.assertTrue(stops[0].startswith("FOCUSED_JEV=1 "))
        self.assertFalse(any(c.startswith("FOCUSED_JEV=1") for c in self.commands("SessionEnd")))
        other = ROOT / "tests"  # not a checkout: refused, and nothing is changed
        with self.assertRaises(ValueError):
            install.install(other, settings_path=self.settings, skills_dir=self.skills)
        self.assertEqual(len([c for c in self.commands("Stop") if install.MARK in c]), 1)

    def test_uninstall_removes_only_what_it_installed(self):
        theirs = {"type": "command", "command": "echo theirs"}
        self.settings.write_text(json.dumps({"env": {"KEEP": "1"}, "hooks": {"Stop": [{"hooks": [theirs]}]}}))
        (self.skills / "mine").mkdir(parents=True)
        (self.skills / "mine" / "SKILL.md").write_text("---\nname: mine\n---\n")
        self.run_install()
        install.uninstall(settings_path=self.settings, skills_dir=self.skills)
        self.assertEqual(self.loaded(), {"env": {"KEEP": "1"}, "hooks": {"Stop": [{"hooks": [theirs]}]}})
        self.assertTrue((self.skills / "mine" / "SKILL.md").exists())
        self.assertFalse((self.skills / "focused-context").exists())

    def test_broken_settings_and_foreign_skills_are_left_alone(self):
        for broken in ("{ not json", '{"hooks": ["not", "an", "object"]}', '{"env": "x"}', '{"hooks": {"Stop": {}}}'):
            self.settings.write_text(broken)
            with self.assertRaises(ValueError):
                self.run_install()
            self.assertEqual(self.settings.read_text(), broken)
        self.settings.write_text("{}")
        (self.skills / "focused-find").mkdir(parents=True)
        (self.skills / "focused-find" / "SKILL.md").write_text("someone else's")
        with self.assertRaises(ValueError):
            self.run_install()
        self.assertEqual((self.skills / "focused-find" / "SKILL.md").read_text(), "someone else's")
        # Refused before anything was written: no hooks, no mark, no other commands.
        self.assertEqual(self.settings.read_text(), "{}")
        self.assertFalse((self.skills / "focused-answers").exists())

    def test_a_checkout_path_the_shell_would_read_is_refused(self):
        for name in ("a$HOME", "a`id`", 'a"b', "a\\b", "a b"):
            odd = self.dir / name
            (odd / "scripts").mkdir(parents=True)
            (odd / "scripts" / "focused.py").write_text("")
            with self.assertRaises(ValueError, msg=name):
                install.install(odd, settings_path=self.settings, skills_dir=self.skills)
        self.assertFalse(self.settings.exists())

    def test_the_installed_hooks_run_as_claude_code_would_run_them(self):
        """Each installed command, run through sh with a hook payload: the Stop hook
        indexes, and SessionStart after /clear prints the cleared session's summary."""
        subprocess.run(["sh", str(ROOT / "scripts" / "install-cloud.sh")], check=True, capture_output=True,
                       env=dict(os.environ, CLAUDE_CONFIG_DIR=str(self.settings.parent)))
        transcript = self.dir / "projects" / "-p" / "s1.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("\n".join(json.dumps(e) for e in SESSION) + "\n")
        env = dict(os.environ, FOCUSED_HOME=str(self.dir / "home"), FOCUSED_RETENTION_DAYS="36500")

        def run(event, payload):
            [command] = [c for c in self.commands(event) if install.MARK in c]
            return subprocess.run(["sh", "-c", command], input=json.dumps(payload), text=True,
                                  capture_output=True, env=env, cwd=self.dir, check=True)

        self.assertEqual(run("Stop", {"session_id": "s1", "transcript_path": str(transcript)}).stdout, "")
        self.assertTrue((self.dir / "home" / "-p.sqlite").exists())
        run("SessionEnd", {"session_id": "s1", "transcript_path": str(transcript), "reason": "clear"})
        out = run("SessionStart", {"session_id": "s2", "source": "clear",
                                   "transcript_path": str(transcript.parent / "s2.jsonl")}).stdout
        self.assertIn("# Where we left off", out)
        self.assertEqual(run("UserPromptSubmit", {"session_id": "s1", "transcript_path": str(transcript),
                                                  "prompt": "Next."}).stdout, "")


if __name__ == "__main__":
    unittest.main()
