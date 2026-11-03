"""The hosted index: focused-context-worker, a Cloudflare Worker on your own account.

A cloud session's container is reclaimed after a while, and the next one starts with an
empty ~/.claude/focused. So the hooks also push each session's rows to the Worker, and
any later session, on any machine, can read them back or search them.

On when both are set in the environment Claude Code runs in:
- FOCUSED_REMOTE_URL: the Worker's URL, https (http only to this machine, for testing);
- FOCUSED_REMOTE_TOKEN: its FOCUSED_TOKEN secret.

What goes: the rows of the local index (store.py), as the Worker's API takes them. A
push carries only the turns whose rows changed since the last push (the `pushed` table
keeps each turn's digest), in pieces of about PIECE bytes, and names the turns that went
away locally (`gone`) so they go there too. A turn deleted here by retention is not
named: each side keeps its own retention.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from .jev import _context as tls_context

URL_VAR, TOKEN_VAR = "FOCUSED_REMOTE_URL", "FOCUSED_REMOTE_TOKEN"
PIECE = 512 * 1024        # bytes of turns per push: on Workers Free a request gets 10 ms of CPU
LARGEST_TURN = 1_900_000  # D1 keeps a row to 2 MB; a turn larger than this can't be pushed
PROJECT = re.compile(r"[A-Za-z0-9_-]{1,300}")
LOCAL = ("localhost", "127.0.0.1", "::1")

# The columns the Worker takes, in the order of focused's own tables.
COLUMNS = {
    "turns": ("turn_key", "seq", "origin", "parent_key", "started_at", "prompt", "answer", "answer_source",
              "tool_calls", "files", "commits", "pull_requests"),
    "parts": ("turn_key", "n", "text", "queued", "kinds", "wording"),
    "paras": ("para_key", "turn_key", "source_key", "seq", "place", "started_at", "text"),
    "pairs": ("turn_key", "n", "para_key", "answers", "partly", "wording"),
}
ORDER = {"turns": "seq", "parts": "turn_key, n", "paras": "turn_key, seq", "pairs": "turn_key, n, para_key"}


class RemoteError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def settings() -> Optional[tuple]:
    """(url, token), or None when the hosted index isn't set up. A URL that would send
    the token in the clear to another machine is refused."""
    url = os.environ.get(URL_VAR, "").strip().rstrip("/")
    token = os.environ.get(TOKEN_VAR, "").strip()
    if not url or not token:
        return None
    parsed = urllib.parse.urlsplit(url)
    if not (parsed.scheme == "https" or parsed.scheme == "http" and parsed.hostname in LOCAL):
        raise RemoteError(f"{URL_VAR} must be an https URL")
    return url, token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would follow a redirect with the token attached, to any host and over plain
    http too, so none is followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise RemoteError(f"HTTP {code}: the Worker redirected to {newurl}, which isn't followed, "
                          f"as it would carry the token; set {URL_VAR} to where it points", code)


def _call(method: str, path: str, body: Optional[bytes] = None, timeout: float = 15.0):
    """One request to the Worker; its JSON, or RemoteError (with the status, if any)."""
    config = settings()
    if config is None:
        raise RemoteError("the hosted index isn't set up")
    url, token = config
    headers = {"authorization": f"Bearer {token}", "accept": "application/json",
               "user-agent": "focused-context"}
    if body is not None:
        headers["content-type"] = "application/json"
    request = urllib.request.Request(url + path, data=body, method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=tls_context()), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            reason = json.loads(error.read()).get("error") or ""
        except (ValueError, AttributeError, OSError):
            reason = ""
        raise RemoteError(f"HTTP {error.code}{': ' + reason if reason else ''}", error.code) from None
    except (urllib.error.URLError, OSError, ValueError) as error:  # unreachable, timed out, not JSON
        raise RemoteError(str(getattr(error, "reason", error))) from None


def _session_path(session_id: str) -> str:
    return "/v1/sessions/" + urllib.parse.quote(session_id, safe="")


# ---- pushing ----

def _groups(conn: sqlite3.Connection, session_id: str) -> dict:
    """turn key -> its rows in every table, as the Worker takes them; turns in order."""
    groups: dict = {}
    for row in conn.execute(f"SELECT {', '.join(COLUMNS['turns'])} FROM turns WHERE session_id = ? ORDER BY seq",
                            (session_id,)):
        groups[row[0]] = {"turns": [dict(zip(COLUMNS["turns"], row))], "parts": [], "paras": [], "pairs": []}
    for table in ("parts", "paras", "pairs"):
        columns = COLUMNS[table]
        for row in conn.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE session_id = ? ORDER BY {ORDER[table]}",
                                (session_id,)):
            record = dict(zip(columns, row))
            if record["turn_key"] in groups:
                groups[record["turn_key"]][table].append(record)
    return groups


def _encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def push(conn: sqlite3.Connection, session_id: str, project: str, timeout: float = 30.0,
         clock=time.monotonic) -> dict:
    """Push what changed in a session since its last push. Each piece the Worker took is
    recorded at once, so a push cut short goes on from there next time. Returns counts,
    and the turns too large to push ("skipped"), which are recorded as pushed so they
    aren't tried again until they change."""
    deadline = clock() + timeout
    groups = _groups(conn, session_id)
    encoded = {key: _encoded(group) for key, group in groups.items()}
    digests = {key: hashlib.sha256(data).hexdigest() for key, data in encoded.items()}
    pushed = dict(conn.execute("SELECT turn_key, digest FROM pushed WHERE session_id = ?", (session_id,)).fetchall())
    changed = [key for key in groups if pushed.get(key) != digests[key]]
    gone = [key for key in pushed if key not in groups]
    done = {"turns": 0, "gone": 0, "requests": 0, "skipped": []}
    if not changed and not gone:
        return done
    skipped = [key for key in changed if len(encoded[key]) > LARGEST_TURN]
    if skipped:
        _record(conn, session_id, {key: digests[key] for key in skipped}, [])
        done["skipped"] = skipped
    changed = [key for key in changed if key not in skipped]
    first_at, last_at, title = (conn.execute("SELECT first_at, last_at, title FROM sessions WHERE session_id = ?",
                                             (session_id,)).fetchone() or (None, None, None))
    pieces, piece, size = [], [], 0
    for key in changed:
        if piece and size + len(encoded[key]) > PIECE:
            pieces.append(piece)
            piece, size = [], 0
        piece.append(key)
        size += len(encoded[key])
    if piece or gone:
        pieces.append(piece)
    for i, keys in enumerate(pieces):
        named_gone = gone if i == 0 else []
        body = {"project": project, "first_at": first_at, "last_at": last_at, "title": title, "gone": named_gone,
                **{table: [row for key in keys for row in groups[key][table]] for table in COLUMNS}}
        left = deadline - clock()
        if left < 1:
            raise RemoteError("out of time")
        _call("PUT", _session_path(session_id), _encoded(body), timeout=min(15.0, left))
        _record(conn, session_id, {key: digests[key] for key in keys}, named_gone)
        done["requests"] += 1
        done["turns"] += len(keys)
        done["gone"] += len(named_gone)
    return done


def _record(conn: sqlite3.Connection, session_id: str, digests: dict, gone: list) -> None:
    with conn:
        conn.executemany(
            "INSERT INTO pushed (session_id, turn_key, digest) VALUES (?, ?, ?) "
            "ON CONFLICT (session_id, turn_key) DO UPDATE SET digest = excluded.digest",
            [(session_id, key, digest) for key, digest in digests.items()])
        conn.executemany("DELETE FROM pushed WHERE session_id = ? AND turn_key = ?",
                         [(session_id, key) for key in gone])


# ---- reading back ----

def fetch(session_id: str, timeout: float = 15.0) -> Optional[dict]:
    """Every row of a session, or None if the Worker has no such session."""
    try:
        data = _call("GET", _session_path(session_id), timeout=timeout)
    except RemoteError as error:
        if error.status == 404:
            return None
        raise
    if not isinstance(data, dict) or not PROJECT.fullmatch(str(data.get("project", ""))):
        raise RemoteError("the Worker sent a session without a plain project name")
    return data


def save(conn: sqlite3.Connection, session_id: str, data: dict) -> int:
    """Put a fetched session into a local index, replacing what it held of that session,
    and note it as pushed, so it isn't sent back unchanged. Returns the turns saved."""
    turns = {row["turn_key"] for row in data.get("turns", [])}
    with conn:
        for table in ("pairs", "parts", "paras", "turns", "pushed"):
            conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
        conn.execute(
            "INSERT INTO sessions (session_id, transcript_path, first_at, last_at, title) VALUES (?, NULL, ?, ?, ?) "
            "ON CONFLICT (session_id) DO UPDATE SET first_at = excluded.first_at, last_at = excluded.last_at, "
            "title = excluded.title",
            (session_id, data.get("first_at"), data.get("last_at"), data.get("title")))
        for table, columns in COLUMNS.items():
            rows = [row for row in data.get(table, []) if row.get("turn_key") in turns]
            conn.executemany(
                f"INSERT INTO {table} (session_id, {', '.join(columns)}) VALUES (?{', ?' * len(columns)})",
                [(session_id, *(row.get(column) for column in columns)) for row in rows])
    groups = _groups(conn, session_id)
    _record(conn, session_id, {key: hashlib.sha256(_encoded(group)).hexdigest() for key, group in groups.items()}, [])
    return len(turns)


def sessions(project: Optional[str], limit: int = 20, timeout: float = 15.0) -> list:
    """Recent sessions, newest first: id, project, times, title, turn and message counts,
    and first message."""
    query = urllib.parse.urlencode({k: v for k, v in (("project", project), ("limit", limit)) if v is not None})
    return list(_call("GET", f"/v1/sessions?{query}", timeout=timeout).get("sessions") or [])


def search(words: list, project: Optional[str], limit: int = 10, timeout: float = 15.0) -> dict:
    """Messages and paragraphs matching every word, each with its `hit`."""
    query = {"q": " ".join(words), "limit": limit}
    if project is not None:
        query["project"] = project
    found = _call("GET", f"/v1/search?{urllib.parse.urlencode(query)}", timeout=timeout)
    return {"turns": list(found.get("turns") or []), "paras": list(found.get("paras") or [])}
