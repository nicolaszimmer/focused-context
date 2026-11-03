"""The repository graph (focused/repos.py): the repositories each session worked in, kept in
the index, pushed with it, and followed by recall and by the choice of a session."""

import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_summary  # noqa: E402  (not its classes: their tests would run here again)
from test_focused import HUMAN, Fixture, assistant, text, tool, user  # noqa: E402
from test_remote import RemoteFixture  # noqa: E402

from focused import cli, recall, remote, repos, store, transcript  # noqa: E402

FC, WORKER, LAB, OTHER = ("github.com/o/focused-context", "github.com/o/focused-context-worker",
                          "github.com/o/focused-context-lab", "github.com/o/unrelated")


def git_repo(path: Path, origin=None) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path


class NameTest(unittest.TestCase):
    def test_a_repository_is_named_by_its_origin_in_any_of_gits_forms(self):
        for url in ("https://github.com/o/focused-context", "https://github.com/o/focused-context.git\n",
                    "https://user:secret@GitHub.com:443/o/focused-context.git", "ssh://git@github.com/o/focused-context.git",
                    "git@github.com:o/focused-context.git", "github.com:o/focused-context"):
            self.assertEqual(repos.name_for_url(url), FC, url)
        self.assertEqual(repos.name_for_url("https://gitlab.com/group/sub/name.git"), "gitlab.com/group/sub/name")
        self.assertEqual(repos.name_for_url("git@github.com:O/Focused-Context.git"), FC)  # one name, whatever the case
        for url in ("", "/srv/git/name.git", "./name", "file:///srv/git/name.git", "https://github.com/",
                    "C:/repos/tool", "c:\\repos\\tool", "https://[::1/o/name",
                    "https://github.com/o/na me", "https://github.com/" + "x/" * 120):
            self.assertIsNone(repos.name_for_url(url), url)
        self.assertEqual(repos.short(FC), "focused-context")


class ExtractTest(RemoteFixture):
    def setUp(self):
        super().setUp()
        self.home = self.dir / "home"
        self.fc = git_repo(self.home / "focused-context", "https://github.com/o/focused-context.git")
        self.worker_repo = git_repo(self.home / "focused-context-worker", "git@github.com:o/focused-context-worker.git")
        git_repo(self.home / "focused-context-lab", "https://github.com/o/focused-context-lab")  # attached, untouched
        git_repo(self.home / "scratch")  # no origin: no name
        (self.home / "notes").mkdir()
        (self.fc / "focused").mkdir()

    def session(self):
        def at(entry, cwd):
            return entry | {"cwd": str(cwd)}
        return [
            at(user("u1", "Deploy the Worker and fix the hook.", **HUMAN), self.home),
            at(assistant(tool("t1", "Read", file_path=str(self.worker_repo / "src" / "index.ts"))), self.home),
            at(assistant(tool("t2", "Grep", pattern="x", path="focused")), self.fc),  # relative to the cwd
            at(assistant(tool("t3", "Edit", file_path=str(self.fc / "focused" / "gone.py"))), self.fc),  # no longer there
            at(assistant(text("Done.")), self.fc),
            at(assistant(tool("t4", "Read", file_path=str(self.home / "scratch" / "a.txt"))), self.home),
        ]

    def test_a_session_links_to_the_repositories_it_worked_in_by_its_entries(self):
        parsed = transcript.parse_entries(self.session())
        self.assertEqual(parsed.start, str(self.home))
        self.assertEqual(repos.of_session(parsed.places, parsed.start), [
            (FC, 5),  # three entries in it, a path relative to it, a file in it
            (WORKER, 1),
            (LAB, 0),  # attached, not worked in
        ])
        self.assertEqual(repos.attached(str(self.fc / "focused")), [FC])  # inside a repository: that one
        self.assertEqual(repos.attached(str(self.home)), [FC, LAB, WORKER])  # sorted
        self.assertEqual(repos.attached(str(self.home / "notes")), [])
        self.assertEqual(repos.attached(str(self.dir / "missing")), [])

    def test_the_index_keeps_them_and_a_later_read_without_the_folders_lowers_no_count(self):
        self.write(self.session())
        self.index()
        with closing(store.connect(store.db_for_transcript(self.transcript))) as conn:
            self.assertEqual(store.session_repos(conn, "s1"), [
                {"repo": FC, "entries": 5}, {"repo": WORKER, "entries": 1}, {"repo": LAB, "entries": 0}])
            self.assertEqual([r["repo"] for r in store.session_repos(conn, "s1", worked=True)], [FC, WORKER])
            shutil.rmtree(self.home)  # a new container: the transcript is read again, the folders are gone
            store.index_transcript(conn, self.transcript)
            self.assertEqual(len(store.session_repos(conn, "s1")), 3)
            self.assertEqual(store.session_repos(conn, "s1")[0], {"repo": FC, "entries": 5})

    def test_git_runs_before_the_index_takes_its_write_lock(self):
        self.write(self.session())
        seen = []
        conn = store.connect(store.db_for_transcript(self.transcript))
        self.addCleanup(conn.close)
        real = repos.of_session

        def looking(places, start):
            seen.append(conn.in_transaction)
            return real(places, start)
        with mock.patch.object(store.repos, "of_session", looking):
            store.index_transcript(conn, self.transcript)
        self.assertEqual(seen, [False])

    def test_a_push_names_them_and_a_session_read_back_brings_them(self):
        self.write(self.session())
        payload = json.dumps({"session_id": "s1", "transcript_path": str(self.transcript)})
        with redirect_stdout(io.StringIO()), mock.patch("sys.stdin", io.StringIO(payload)):
            cli.main(["hook", "stop"])
        self.assertEqual(self.worker.sessions["s1"]["repos"], [
            {"repo": FC, "entries": 5}, {"repo": WORKER, "entries": 1}, {"repo": LAB, "entries": 0}])
        os.environ["FOCUSED_HOME"] = str(self.dir / "new-container")
        target = self.dir / "back.sqlite"
        with closing(store.connect(target)) as conn:
            remote.save(conn, "s1", remote.fetch("s1"))
            self.assertEqual(store.session_repos(conn, "s1")[1], {"repo": WORKER, "entries": 1})
        self.worker.sessions["s1"]["repos"] = [{"repo": "not a repository", "entries": 1}]
        with self.assertRaises(remote.RemoteError):
            remote.fetch("s1")

    def test_an_index_whose_folders_are_gone_doesnt_wipe_what_the_hosted_index_holds(self):
        self.worker.sessions["s1"] = {"turns": [], "parts": [], "paras": [], "pairs": [],
                                      "repos": [{"repo": FC, "entries": 9}]}
        shutil.rmtree(self.home)
        self.write(self.session())
        self.index()
        with closing(store.connect(store.db_for_transcript(self.transcript))) as conn:
            remote.push(conn, "s1", "-work-repo")
        self.assertEqual(self.worker.sessions["s1"]["repos"], [{"repo": FC, "entries": 9}])


class RecallByRepositoryTest(RemoteFixture):
    def setUp(self):
        super().setUp()
        self.fc = git_repo(self.dir / "work" / "focused-context", "https://github.com/o/focused-context")
        self.worker.recall_items = [
            {"session_id": "near1", "title": "The Worker", "started_at": "2026-11-02T10:00:00Z", "part": "Is it deployed?",
             "answers": ["Not yet."], "similarity": 0.7, "via": ["meaning"], "repos": [WORKER], "relation": "near"},
            {"session_id": "same1", "title": "Recall", "started_at": "2026-11-01T10:00:00Z", "part": "Where is recall?",
             "answers": [], "similarity": 0.65, "via": ["meaning"], "repos": [FC, WORKER], "relation": "same"},
            {"session_id": "none1", "title": "Else", "started_at": "2026-11-01T10:00:00Z", "part": "Unrelated?",
             "answers": [], "similarity": 0.9, "via": ["meaning"], "relation": "sideways"},
        ]

    def test_the_first_message_sends_the_repositories_the_session_starts_with(self):
        fresh = self.transcript.parent / "s2.jsonl"
        fresh.write_text("")
        payload = {"session_id": "s2", "transcript_path": str(fresh), "prompt": "Where does recall live now?",
                   "cwd": str(self.dir / "work")}
        with redirect_stdout(io.StringIO()) as out, mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            cli.main(["hook", "prompt"])
        sent = self.worker.recalls[-1]
        self.assertEqual((sent["repos"], sent["min_similarity"]), ([FC], recall.MIN_SIMILARITY))
        context = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertIn('- “The Worker” (near1), 2026-11-02, related repository (focused-context-worker): "Is it deployed?"',
                      context)
        self.assertIn('- “Recall” (same1), 2026-11-01, same repository (focused-context): "Where is recall?"', context)
        self.assertIn('- “Else” (none1), 2026-11-01: "Unrelated?"', context)  # a relation it doesn't know is none

    def test_on_demand_it_sends_the_repositories_worked_in_and_a_lower_floor(self):
        session = self.transcript.parent / "s2.jsonl"
        session.write_text(json.dumps(user("u1", "Fix it.", **HUMAN) | {"sessionId": "s2", "cwd": str(self.fc)}) + "\n")
        with redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(cli, "projects_dir", return_value=self.transcript.parent.parent), \
                mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.dir / "work")}):
            cli.main(["--in-session", "s2", "recall", "--", "recall"])
        sent = self.worker.recalls[-1]
        self.assertEqual((sent["repos"], sent["min_similarity"]), ([FC], recall.ON_DEMAND_MIN))
        self.assertIn("## “Recall” (same1), 2026-11-01 10:00 (by meaning, 0.65; same repository (focused-context))",
                      out.getvalue())


class ChooseByRepositoryTest(Fixture):
    """In a new session, the sessions of its own repositories are offered first, then those
    of repositories worked on together with them, then the rest."""
    session = test_summary.ChooseTest.session
    context = test_summary.ChooseTest.context

    def setUp(self):
        super().setUp()
        os.environ["FOCUSED_RETENTION_DAYS"] = "36500"
        self.project = self.transcript.parent
        self.transcript.unlink()
        self.work = self.dir / "work"
        for name in ("focused-context", "focused-context-worker", "unrelated"):
            git_repo(self.work / name, f"https://github.com/o/{name}")

    def in_repos(self, sid, minute, *names):
        self.session(sid, minute, [f"Work in {', '.join(names) or 'nothing'}."], title=f"Session {sid}")
        with closing(store.connect(store.db_for_transcript(self.project / f"{sid}.jsonl"))) as conn, conn:
            conn.executemany("INSERT INTO session_repos VALUES (?, ?, ?)",
                             [(sid, f"github.com/o/{name}", 5) for name in names])

    def test_the_same_repositories_first_then_related_ones_then_the_latest(self):
        self.in_repos("same-old", 0, "focused-context")
        self.in_repos("both", 5, "focused-context", "focused-context-worker")  # relates the two
        self.in_repos("near", 10, "focused-context-worker")
        self.in_repos("other", 40, "unrelated")
        self.session("cur", 50, ["<command-name>/focused-context</command-name>"])
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.work / "focused-context")}):
            code, out = self.context()
        lines = out.splitlines()
        self.assertIn("those of this session's repositories first", lines[0])
        self.assertEqual([line.split(" | ")[0] for line in lines[1:]], ["1. both", "2. same-old", "3. near"])
        self.assertEqual(lines[1].split(" | ")[4], "focused-context, focused-context-worker")
        # Without a repository, the latest, as before.
        code, out = self.context()
        self.assertEqual([line.split(" | ")[0] for line in out.splitlines()[1:]], ["1. other", "2. near", "3. both"])


class HostedChoiceTest(RemoteFixture):
    def test_the_hosted_index_adds_other_projects_sessions_only_when_related(self):
        listed = {"turns": [{"turn_key": "a", "seq": 0, "origin": "human", "prompt": "Hello there."}],
                  "parts": [], "paras": [], "pairs": [], "first_at": None}
        self.worker.sessions.update({
            "elsewhere-same": dict(listed, project="-other", last_at="2026-11-03T05:00:00Z", title="Same elsewhere",
                                   repos=[{"repo": FC, "entries": 3}]),
            "elsewhere-none": dict(listed, project="-other", last_at="2026-11-03T06:00:00Z", title="Unrelated elsewhere",
                                   repos=[{"repo": OTHER, "entries": 3}]),
            "here": dict(listed, project="-work-repo", last_at="2026-11-03T04:00:00Z", title="Here", repos=[]),
        })
        with closing(store.connect(store.db_for_transcript(self.transcript))) as conn:
            rows, problem = cli._sessions(conn, store.db_for_transcript(self.transcript), mine=[FC])
        self.assertIsNone(problem)
        self.assertEqual([(r["session_id"], r["relation"]) for r in rows], [("elsewhere-same", "same"), ("here", None)])


class GraphTest(unittest.TestCase):
    def test_related_repositories_are_those_worked_on_together(self):
        sessions = [[FC, WORKER], [WORKER, LAB], [OTHER]]
        self.assertEqual(repos.near([FC], sessions), {WORKER})  # one step, not two
        self.assertEqual(repos.relation([WORKER], {FC}, {WORKER}), "near")
        self.assertEqual(repos.relation([FC, OTHER], {FC}, {WORKER}), "same")
        self.assertIsNone(repos.relation([OTHER], {FC}, {WORKER}))
        self.assertEqual(repos.describe("same", [WORKER, FC], [FC]), "same repository (focused-context)")
        self.assertEqual(repos.describe(None, [FC], [FC]), "")


if __name__ == "__main__":
    unittest.main()
