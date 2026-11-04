"""Steward sessions: a pull request driven to mergeable by a session of its own.

Decided by the maintainer on 2026-11-04: a working session doesn't watch the pull requests
it opens. Each CI result, review and bot comment would wake it and re-read its whole
conversation, mostly to find nothing to do, and a command typed meanwhile (/focused-answers)
waits until that turn ends. So the session that opens a pull request hands it to a fresh
cloud session, the steward, on a smaller model (Sonnet 5.5): it subscribes to the pull
request, fixes what is the pull request's to fix, answers the reviewers, never merges, and
says in one comment what waits on the maintainer.

`arguments()` is what the opening session passes to the create_session tool. The
PostToolUse hook after mcp__github__create_pull_request gives them to Claude, in cloud
sessions only, where that tool exists; /focused:steward does the same for a pull request
already open. FOCUSED_STEWARD=0 switches the hook off; FOCUSED_STEWARD_MODEL picks another
model.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

MODEL = "claude-sonnet-5-5"
TAG = "focused-steward"
NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}")
BRANCH = re.compile(r"[A-Za-z0-9_./-]{1,200}")  # a branch of the same repository: no fork's "owner:branch"
PR_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)")

BRIEF = """\
You are the steward of the pull request {url} ({owner}/{repo}#{number}, branch `{head}`). \
Another Claude Code session opened it and handed it to you, so that it isn't woken by every \
review and CI result. Get the pull request mergeable, keep it so, and say what waits on the \
maintainer.

1. Call subscribe_pr_activity for {owner}/{repo} #{number} now, then read the pull request: \
its description, CI, open review threads and mergeability. Follow the rules the subscription \
gives you. If the subscription says another session is watching the pull request, its events \
won't reach you: say so in one line at the end of your turn, so the maintainer knows.
2. Fix what is this pull request's to fix: CI it breaks, merge conflicts, and review findings \
that are real and in its scope. Push to `{head}` only, as new commits: never force-push, \
rebase or amend. Before every push, run the repository's own checks: those its CLAUDE.md \
names, or else its tests, as its README or CI runs them.
3. Answer every review thread: what you changed, or why not, with what you traced. Resolve \
the threads you fixed. Leave open any finding you would decline, any security finding and \
any larger ask: those are the maintainer's.
4. Never merge, approve, close or retarget the pull request, and don't widen it beyond its \
description.
5. When CI is green on the current head and every thread is answered, post one comment on \
the pull request: "Ready to merge" or "Waiting on you", what merging does (from its \
description), and "Waiting on you:" with the decisions left open, or "Nothing". Post it \
again only when that changes. Then end your turn: the next event wakes you.
6. End each turn with a short report: what you changed, what you found, what waits on the \
maintainer."""

HANDOFF = (
    "focused: the maintainer decided (2026-11-04) that a session doesn't watch the pull requests it "
    "opens. Hand {owner}/{repo}#{number} to a steward session instead. First call unsubscribe_pr_activity "
    "for it: opening it may have subscribed this session, and a pull request's events go to one session "
    "only. Then call the create_session tool (claude-code-remote) with exactly these arguments, don't "
    "subscribe to it here, and tell the maintainer in one line that a steward session on {model} drives "
    "it. Then go on with the rest of the work, if any.\n{arguments}"
)


def enabled() -> bool:
    return os.environ.get("FOCUSED_STEWARD") != "0"


def model() -> str:
    return os.environ.get("FOCUSED_STEWARD_MODEL") or MODEL


def arguments(owner: str, repo: str, number: int, head: str) -> dict:
    """The create_session arguments for a steward of owner/repo#number on branch `head`.
    ValueError for anything that isn't a plain name, number or branch of the repository."""
    if not (NAME.fullmatch(owner or "") and NAME.fullmatch(repo or "")):
        raise ValueError("the repository isn't named as owner/name")
    if not isinstance(number, int) or number < 1:
        raise ValueError("the pull request number isn't a positive number")
    if not BRANCH.fullmatch(head or "") or ".." in head or head.startswith(("/", "-")):
        raise ValueError(f"{head!r} isn't a branch of {owner}/{repo} a steward can push to")
    url = f"https://github.com/{owner}/{repo}/pull/{number}"
    return {
        "model": model(),
        "source_url": f"https://github.com/{owner}/{repo}",
        "source_revision": head,
        "outcome_branch": head,
        "title": f"Steward: {owner}/{repo}#{number}",
        "tags": [TAG],
        "prompt": BRIEF.format(url=url, owner=owner, repo=repo, number=number, head=head),
    }


def handoff(args: dict) -> str:
    """What Claude is told: hand the pull request over, with these arguments."""
    owner, repo = args["source_url"].rsplit("/", 2)[-2:]
    number = args["title"].rsplit("#", 1)[1]
    return HANDOFF.format(owner=owner, repo=repo, number=number, model=args["model"],
                          arguments=json.dumps(args, indent=2))


def parse_url(url: str) -> Optional[tuple]:
    """(owner, repo, number) of a pull request's URL, or None."""
    match = PR_URL.fullmatch((url or "").strip().rstrip("/"))
    return (match.group(1), match.group(2), int(match.group(3))) if match else None


def _pull_request_of(response) -> Optional[tuple]:
    """(owner, repo, number) of the pull request a create_pull_request result is about. Read from
    the result's own `html_url`/`url` field (looking through content blocks and JSON text), never
    from a link in its body or title. Without such a field, a URL found in the text counts only
    if it is the only pull request URL there."""
    fields, texts = [], []

    def walk(node):
        if isinstance(node, str):
            texts.append(node)
            try:
                walk(json.loads(node))
            except ValueError:
                pass
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for key in ("html_url", "url"):
                if isinstance(node.get(key), str) and parse_url(node[key]):
                    fields.append(parse_url(node[key]))
            for value in node.values():
                walk(value)

    walk(response)
    if fields:
        return fields[0] if len(set(fields)) == 1 else None
    found = {(m.group(1), m.group(2), int(m.group(3))) for t in texts for m in PR_URL.finditer(t)}
    return found.pop() if len(found) == 1 else None


def from_hook(payload: dict) -> Optional[dict]:
    """The steward's arguments for the pull request a create_pull_request call just opened,
    or None: another tool, no pull request in the result, or a head branch in a fork."""
    if not isinstance(payload, dict) or payload.get("tool_name") != "mcp__github__create_pull_request":
        return None
    given = payload.get("tool_input") or {}
    found = _pull_request_of(payload.get("tool_response"))
    if not found or not isinstance(given, dict):
        return None
    owner, repo, number = found
    if str(given.get("owner", "")).lower() != owner.lower() or str(given.get("repo", "")).lower() != repo.lower():
        return None
    try:
        return arguments(owner, repo, number, str(given.get("head", "")))
    except ValueError:
        return None
