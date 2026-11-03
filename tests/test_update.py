"""Keeping a cloud install at a branch (focused/update.py, `install --follow`), against a
local git repository standing in for GitHub, holding this checkout's own files."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from focused import install, update

ROOT = Path(__file__).resolve().parent.parent


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.upstream = self.dir / "upstream"
        self.upstream.mkdir()
        files = git("ls-files", "--cached", "--others", "--exclude-standard", cwd=ROOT).splitlines()
        for name in files:
            if (ROOT / name).is_file():
                (self.upstream / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, self.upstream / name)
        git("init", "-q", "-b", "main", cwd=self.upstream)
        git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=self.upstream)
        self.first = self.commit("first")
        self.config = self.dir / "claude"
        self.env = mock.patch.dict(os.environ, {
            "FOCUSED_REPOSITORY": self.upstream.as_uri(), "FOCUSED_CACHE": str(self.dir / "cache"),
            "CLAUDE_CONFIG_DIR": str(self.config), "FOCUSED_HOME": str(self.dir / "home"),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def commit(self, message):
        git("add", "-A", cwd=self.upstream)
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", message,
            cwd=self.upstream)
        return git("rev-parse", "HEAD", cwd=self.upstream)

    def installed(self, follow="main", jev=True):
        """As the setup script does: fetch main's commit, and install from it."""
        root = update.fetch(update.latest("main"))
        install.install(root, jev=jev, follow=follow)
        return root

    def settings(self):
        return json.loads((self.config / "settings.json").read_text())

    def commands(self):
        return [h["command"] for groups in self.settings()["hooks"].values() for g in groups for h in g["hooks"]]

    def test_the_install_follows_a_branch_with_a_hook_at_startup_and_resume(self):
        root = self.installed()
        [group] = [g for g in self.settings()["hooks"]["SessionStart"] if g["matcher"] == "startup|resume"]
        [hook] = group["hooks"]
        self.assertTrue(hook["async"])
        self.assertEqual(hook["command"], f'python3 "{root}/scripts/focused.py" update --hook --follow main --jev  '
                                          f'{install.MARK}')
        install.install(root, jev=False)  # without following, no such hook
        self.assertFalse(any("update" in c for c in self.commands()))
        for bad in ("main; rm -rf ~", "-x", "a b", ""):
            with self.assertRaises(ValueError, msg=bad):
                install.install(root, follow=bad)

    def test_nothing_happens_while_the_branch_hasnt_moved(self):
        root = self.installed()
        self.assertEqual(update.update(root, "main", jev=True), f"focused is at main ({self.first[:8]})")
        self.assertEqual(sorted(p.name for p in (self.dir / "cache").iterdir()), [self.first])

    def test_a_moved_branch_is_fetched_and_its_own_installer_takes_over(self):
        root = self.installed()
        (self.upstream / "NEWS").write_text("a newer focused\n")
        second = self.commit("second")
        message = update.update(root, "main", jev=True)
        self.assertEqual(message, f"focused updated to main ({second[:8]}), from {self.first[:8]}")
        new = self.dir / "cache" / second
        self.assertTrue((new / "NEWS").exists())
        commands = self.commands()
        self.assertTrue(commands and all(str(new) in c for c in commands if install.MARK in c), commands)
        self.assertTrue(any("FOCUSED_JEV=1" in c for c in commands))  # Jev stays on
        self.assertIn(str(new), (self.config / "skills" / "focused-context" / "SKILL.md").read_text())
        self.assertEqual(update.update(new, "main", jev=True), f"focused is at main ({second[:8]})")

    def test_the_hook_fails_open_and_prints_nothing(self):
        root = self.installed()
        os.environ["FOCUSED_REPOSITORY"] = (self.dir / "nowhere").as_uri()
        hook = next(c for c in self.commands() if "update --hook" in c)
        done = subprocess.run(["sh", "-c", hook], input="{}", text=True, capture_output=True, cwd=self.dir)
        self.assertEqual((done.returncode, done.stdout), (0, ""))
        self.assertIn("update: git ls-remote", (self.dir / "home" / "errors.log").read_text())
        done = subprocess.run([sys.executable, str(root / "scripts" / "focused.py"), "update", "--follow", "main"],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)  # by hand, it says so
        self.assertIn("focused: git ls-remote", done.stderr)

    def test_the_hook_run_as_claude_code_would_updates_the_install(self):
        root = self.installed()
        second = self.commit("second")
        hook = next(c for c in self.commands() if "update --hook" in c)
        done = subprocess.run(["sh", "-c", hook], input="{}", text=True, capture_output=True, cwd=self.dir)
        self.assertEqual((done.returncode, done.stdout), (0, ""))
        self.assertIn(str(self.dir / "cache" / second), " ".join(self.commands()))
        self.assertNotIn(str(root) + "/", " ".join(self.commands()))

    def test_a_branch_that_doesnt_exist_or_a_name_that_isnt_plain_is_an_error(self):
        with self.assertRaises(update.UpdateError):
            update.latest("no-such-branch")
        with self.assertRaises(update.UpdateError):
            update.latest("main --upload-pack=touch")


if __name__ == "__main__":
    unittest.main()
