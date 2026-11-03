"""The hosted index's client (focused/remote.py), against a stand-in for the Worker's API
(focused-context-worker) that keeps rows the way the Worker does: a push replaces the
rows of the turns it carries and deletes those it names gone."""

import io
import json
import os
import sqlite3
import sys
import threading
import unittest
import urllib.parse
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_focused import HUMAN, SESSION, Fixture, assistant, text, user  # noqa: E402

from focused import cli, remote, store  # noqa: E402

TOKEN = "test-token"
TABLES = ("turns", "parts", "paras", "pairs")


class FakeWorker:
    """The Worker's API over dicts, on 127.0.0.1."""

    def __init__(self):
        self.sessions: dict = {}
        self.requests: list = []
        self.fail: list = []  # statuses to answer the next requests with
        self.redirect = None  # a URL to send every request on to
        worker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.handle_any("GET")

            def do_PUT(self):
                self.handle_any("PUT")

            def handle_any(self, method):
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                worker.requests.append((method, self.path, len(body), self.headers.get("authorization")))
                if worker.redirect:
                    self.send_response(302)
                    self.send_header("location", worker.redirect + self.path)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                if self.headers.get("authorization") != f"Bearer {TOKEN}":
                    return self.reply(401, {"error": "unauthorized"})
                if worker.fail:
                    return self.reply(worker.fail.pop(0), {"error": "failing on purpose"})
                url = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(url.query))
                if url.path.startswith("/v1/sessions/"):
                    session = urllib.parse.unquote(url.path.rsplit("/", 1)[1])
                    if method == "PUT":
                        return self.reply(200, worker.put(session, json.loads(body)))
                    found = worker.sessions.get(session)
                    return self.reply(200, {"session_id": session, **found} if found else None) if found else \
                        self.reply(404, {"error": "no such session"})
                if url.path == "/v1/sessions":
                    listed = [{"session_id": k, "project": v["project"], "last_at": v["last_at"]}
                              for k, v in worker.sessions.items() if query.get("project") in (None, v["project"])]
                    listed.sort(key=lambda s: s["last_at"] or "", reverse=True)
                    return self.reply(200, {"sessions": listed[:int(query.get("limit", 20))]})
                if url.path == "/v1/search":
                    return self.reply(200, worker.search(query["q"].split(), query.get("project")))
                return self.reply(404, {"error": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def put(self, session, body):
        held = self.sessions.setdefault(session, {table: [] for table in TABLES})
        replaced = {t["turn_key"] for t in body.get("turns", [])} | set(body.get("gone", []))
        for table in TABLES:
            held[table] = [row for row in held[table] if row["turn_key"] not in replaced]
        moved = {p["para_key"] for p in body.get("paras", [])}
        held["paras"] = [p for p in held["paras"] if p["para_key"] not in moved]
        for table in TABLES:
            held[table] += body.get(table, [])
        held.update(project=body["project"], first_at=body["first_at"], last_at=body["last_at"])
        if not held["turns"]:
            del self.sessions[session]
        return {"turns": len(body.get("turns", [])), "gone": len(body.get("gone", []))}

    def search(self, words, project):
        def matches(*texts):
            return all(any(w.lower() in (t or "").lower() for t in texts) for w in words)
        out = {"turns": [], "paras": []}
        for session, held in self.sessions.items():
            if project not in (None, held["project"]):
                continue
            prompts = {t["turn_key"]: t["prompt"] for t in held["turns"]}
            out["turns"] += [{"session_id": session, "turn_key": t["turn_key"], "origin": t["origin"],
                              "started_at": t["started_at"], "prompt": t["prompt"], "hit": t["prompt"]}
                             for t in held["turns"] if matches(t["prompt"], t["answer"])]
            out["paras"] += [{"session_id": session, "turn_key": p["turn_key"], "started_at": p["started_at"],
                              "prompt": prompts.get(p["turn_key"]), "hit": p["text"]}
                             for p in held["paras"] if matches(p["text"])]
        return out

    def rows(self, session):
        held = self.sessions[session]
        return {table: sorted(held[table], key=lambda r: json.dumps(r, sort_keys=True)) for table in TABLES}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def local_rows(conn, session):
    groups = remote._groups(conn, session)
    return {table: sorted((row for g in groups.values() for row in g[table]), key=lambda r: json.dumps(r, sort_keys=True))
            for table in TABLES}


class RemoteFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.worker = FakeWorker()
        self.addCleanup(self.worker.close)
        os.environ.update({"FOCUSED_REMOTE_URL": self.worker.url, "FOCUSED_REMOTE_TOKEN": TOKEN,
                           "FOCUSED_RETENTION_DAYS": "36500"})

    def pushes(self):
        return [r for r in self.worker.requests if r[0] == "PUT"]


class SettingsTest(unittest.TestCase):
    def test_on_only_with_both_and_never_in_the_clear_to_another_machine(self):
        with mock.patch.dict(os.environ, {"FOCUSED_REMOTE_URL": "", "FOCUSED_REMOTE_TOKEN": "t"}):
            self.assertIsNone(remote.settings())
        for url, expected in (("https://focused.example.workers.dev/", "https://focused.example.workers.dev"),
                              ("http://127.0.0.1:8787", "http://127.0.0.1:8787"),
                              ("http://localhost:8787", "http://localhost:8787"),
                              ("http://[::1]:8787", "http://[::1]:8787")):
            with mock.patch.dict(os.environ, {"FOCUSED_REMOTE_URL": url, "FOCUSED_REMOTE_TOKEN": "t"}):
                self.assertEqual(remote.settings(), (expected, "t"))
        for url in ("http://focused.example.workers.dev", "http://localhost.example.com", "ftp://x", "focused.dev"):
            with mock.patch.dict(os.environ, {"FOCUSED_REMOTE_URL": url, "FOCUSED_REMOTE_TOKEN": "t"}), \
                    self.assertRaises(remote.RemoteError, msg=url):
                remote.settings()


class PushTest(RemoteFixture):
    def test_a_push_sends_what_changed_and_nothing_twice(self):
        conn = self.index()
        done = remote.push(conn, "s1", "-work-repo")
        self.assertEqual((done["turns"], done["requests"], done["gone"]), (5, 1, 0))
        self.assertEqual(self.worker.rows("s1"), local_rows(conn, "s1"))
        self.assertEqual(self.worker.sessions["s1"]["project"], "-work-repo")
        self.assertEqual(remote.push(conn, "s1", "-work-repo")["requests"], 0)
        self.write(SESSION + [user("u9", "And the retries?", ts="2026-11-03T06:20:00.000Z", **HUMAN),
                              assistant(text("They back off."))])
        store.index_transcript(conn, self.transcript, now=self.now)
        done = remote.push(conn, "s1", "-work-repo")
        self.assertEqual(done["turns"], 1)
        self.assertEqual(self.worker.rows("s1"), local_rows(conn, "s1"))

    def test_a_turn_gone_here_is_named_gone(self):
        conn = self.index()
        remote.push(conn, "s1", "-work-repo")
        self.write(SESSION[:-2])  # without the last message
        store.index_transcript(conn, self.transcript, now=self.now)
        done = remote.push(conn, "s1", "-work-repo")
        self.assertEqual((done["gone"], done["turns"]), (1, 0))
        self.assertEqual(self.worker.rows("s1"), local_rows(conn, "s1"))
        self.assertEqual(remote.push(conn, "s1", "-work-repo")["requests"], 0)

    def test_a_turn_deleted_for_its_age_stays_in_the_hosted_index(self):
        conn = self.index()
        remote.push(conn, "s1", "-work-repo")
        os.environ["FOCUSED_RETENTION_DAYS"] = "30"
        store.index_transcript(conn, self.transcript, now=self.now + timedelta(days=60))
        self.assertEqual(conn.execute("SELECT count(*) FROM turns").fetchone()[0], 0)
        self.assertEqual(remote.push(conn, "s1", "-work-repo")["requests"], 0)
        self.assertEqual(len(self.worker.rows("s1")["turns"]), 5)
        self.assertEqual(conn.execute("SELECT count(*) FROM pushed").fetchone()[0], 0)

    def test_a_push_goes_in_pieces_and_one_cut_short_goes_on_from_there(self):
        conn = self.index()
        original, calls = self.worker.put, []

        def put(session, body):  # the fourth request fails
            calls.append(session)
            if len(calls) == 3:
                self.worker.fail.append(503)
            return original(session, body)
        self.worker.put = put
        with mock.patch.object(remote, "PIECE", 1):  # every turn a piece of its own
            with self.assertRaises(remote.RemoteError) as caught:
                remote.push(conn, "s1", "-work-repo")
            self.assertEqual(caught.exception.status, 503)
            self.assertEqual(conn.execute("SELECT count(*) FROM pushed").fetchone()[0], 3)
            done = remote.push(conn, "s1", "-work-repo")
        self.assertEqual((done["turns"], done["requests"]), (2, 2))
        self.assertEqual(self.worker.rows("s1"), local_rows(conn, "s1"))

    def test_a_turn_too_large_to_push_is_skipped_until_it_changes(self):
        conn = self.index()
        sizes = {key: len(remote._encoded(group)) for key, group in remote._groups(conn, "s1").items()}
        largest = max(sizes, key=sizes.get)
        with mock.patch.object(remote, "LARGEST_TURN", sizes[largest] - 1):
            done = remote.push(conn, "s1", "-work-repo")
            self.assertEqual((done["skipped"], done["turns"]), ([largest], 4))
            self.assertEqual(remote.push(conn, "s1", "-work-repo")["requests"], 0)
        self.assertNotIn(largest, [t["turn_key"] for t in self.worker.rows("s1")["turns"]])

    def test_a_redirect_is_not_followed_so_the_token_goes_nowhere_else(self):
        conn = self.index()
        elsewhere = FakeWorker()
        self.addCleanup(elsewhere.close)
        self.worker.redirect = elsewhere.url  # plain http, on another port: another origin
        for attempt in (lambda: remote.fetch("s1"), lambda: remote.sessions("-work-repo"),
                        lambda: remote.search(["hooks"], None), lambda: remote.push(conn, "s1", "-work-repo")):
            with self.assertRaises(remote.RemoteError) as caught:
                attempt()
            self.assertEqual(caught.exception.status, 302)
        self.assertIn("isn't followed", str(caught.exception))
        self.assertEqual(elsewhere.requests, [])
        self.assertEqual(conn.execute("SELECT count(*) FROM pushed").fetchone()[0], 0)

    def test_without_the_token_nothing_is_read_or_written(self):
        conn = self.index()
        os.environ["FOCUSED_REMOTE_TOKEN"] = "wrong"
        with self.assertRaises(remote.RemoteError) as caught:
            remote.push(conn, "s1", "-work-repo")
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(conn.execute("SELECT count(*) FROM pushed").fetchone()[0], 0)
        self.assertEqual(self.worker.sessions, {})


class ReadBackTest(RemoteFixture):
    def test_a_session_reads_back_row_for_row_and_is_not_sent_back(self):
        here = self.index()
        remote.push(here, "s1", "-work-repo")
        with closing(store.connect(self.dir / "elsewhere.sqlite")) as there:
            data = remote.fetch("s1")
            self.assertEqual(remote.save(there, "s1", data), 5)
            self.assertEqual(local_rows(there, "s1"), local_rows(here, "s1"))
            self.assertEqual(store.turn_views(there, "s1"), store.turn_views(here, "s1"))
            self.assertEqual(len(store.search(there, ["hooks"])), 1)  # its full-text index is filled too
            self.assertEqual(len(store.search_paras(there, ["context"])), 1)
            requests = len(self.worker.requests)
            self.assertEqual(remote.push(there, "s1", "-work-repo")["requests"], 0)
            self.assertEqual(len(self.worker.requests), requests)
        self.assertIsNone(remote.fetch("nope"))


class HostedCliTest(RemoteFixture):
    def run_cli(self, *args, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch("sys.stdin", io.StringIO(stdin or "")):
            code = cli.main(list(args))
        return code, out.getvalue() + err.getvalue()

    def push_from_elsewhere(self):
        """s1, pushed by its Stop hook on another machine; this one starts empty."""
        payload = json.dumps({"session_id": "s1", "transcript_path": str(self.transcript)})
        self.assertEqual(self.run_cli("hook", "stop", stdin=payload), (0, ""))
        self.assertEqual(self.worker.sessions["s1"]["project"], "-work-repo")
        os.environ["FOCUSED_HOME"] = str(self.dir / "new-container")
        self.transcript.unlink()

    def new_session(self, prompt):
        fresh = self.transcript.parent / "s2.jsonl"
        fresh.write_text(json.dumps(user("c1", prompt, **HUMAN) | {"sessionId": "s2"}) + "\n")
        return fresh

    def test_the_stop_and_session_end_hooks_push(self):
        payload = json.dumps({"session_id": "s1", "transcript_path": str(self.transcript)})
        self.assertEqual(self.run_cli("hook", "stop", stdin=payload), (0, ""))
        self.assertEqual(len(self.pushes()), 1)
        self.write(SESSION + [user("u9", "And the retries?", ts="2026-11-03T06:20:00.000Z", **HUMAN)])
        self.assertEqual(self.run_cli("hook", "session-end", stdin=payload), (0, ""))
        self.assertEqual(len(self.pushes()), 2)
        self.assertEqual(len(self.worker.rows("s1")["turns"]), 6)

    def test_a_hosted_index_out_of_reach_is_logged_and_the_local_one_kept(self):
        self.worker.close()
        payload = json.dumps({"session_id": "s1", "transcript_path": str(self.transcript)})
        self.assertEqual(self.run_cli("hook", "stop", stdin=payload), (0, ""))
        with closing(sqlite3.connect(store.db_for_transcript(self.transcript))) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM turns").fetchone()[0], 5)
            self.assertEqual(conn.execute("SELECT count(*) FROM pushed").fetchone()[0], 0)
        log = (Path(os.environ["FOCUSED_HOME"]) / "errors.log").read_text()
        self.assertIn("push: session s1:", log)
        self.assertNotIn("Traceback", log)

    def test_index_pushes_each_session_it_indexes(self):
        code, out = self.run_cli("index", str(self.transcript))
        self.assertEqual(code, 0)
        self.assertIn("s1.jsonl: 3 messages, 2 other turns, pushed", out)
        self.assertEqual(len(self.worker.rows("s1")["turns"]), 5)
        os.environ["FOCUSED_REMOTE_URL"] = "http://focused.example.workers.dev"
        code, out = self.run_cli("index", str(self.transcript))
        self.assertEqual(code, 2)
        self.assertIn("must be an https URL", out)

    def test_context_in_a_new_container_loads_the_previous_session_from_the_hosted_index(self):
        self.push_from_elsewhere()
        self.new_session("<command-name>/focused:context</command-name>")
        with mock.patch.object(cli, "projects_dir", return_value=self.transcript.parent.parent):
            code, out = self.run_cli("--in-session", "s2", "context")
        self.assertEqual(code, 0)
        self.assertIn("Summary of session s1", out)
        self.assertIn("How do hooks work?", out)

    def test_answers_reads_a_session_this_machine_does_not_have(self):
        self.push_from_elsewhere()
        code, out = self.run_cli("answers", "--session", "s1")
        self.assertEqual(code, 0)
        self.assertIn("A: Hooks only add context.", out)
        self.assertTrue((self.dir / "new-container" / "-work-repo.sqlite").exists())
        code, out = self.run_cli("answers", "--session", "nope")
        self.assertIn("No indexed sessions match nope", out)

    def test_find_adds_matches_from_sessions_not_on_this_machine(self):
        self.push_from_elsewhere()
        fresh = self.new_session("How do hooks get installed?")
        self.run_cli("hook", "stop", stdin=json.dumps({"session_id": "s2", "transcript_path": str(fresh)}))
        with mock.patch.object(cli, "projects_dir", return_value=self.transcript.parent.parent):
            code, out = self.run_cli("--in-session", "s2", "find", "hooks")
        self.assertEqual(code, 0)
        local, hosted = out.split("In the hosted index, from sessions not on this machine:")
        self.assertIn("session s2", local)
        self.assertIn("session s1 · human: How do hooks work?", hosted)
        self.assertNotIn("session s2", hosted)  # this machine's own sessions are listed once
        self.worker.fail = [500]
        with mock.patch.object(cli, "projects_dir", return_value=self.transcript.parent.parent):
            code, out = self.run_cli("--in-session", "s2", "find", "hooks")
        self.assertIn("could not search the hosted index (HTTP 500: failing on purpose)", out)
        self.assertIn("session s2", out)


if __name__ == "__main__":
    unittest.main()
