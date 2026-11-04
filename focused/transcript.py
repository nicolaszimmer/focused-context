"""Read a Claude Code transcript (JSONL) into turns.

Every split here is a fact the transcript records. Nothing is guessed, and
no model is asked anything.

- A turn starts at each prompt: one a person typed, or one Claude Code
  started on its own (a wake-up, a notification, hook feedback).
- A prompt is a person's when the transcript says so (`origin.kind` or
  `turnOrigin` is "human"). A prompt sent through the SDK or `claude -p`
  records `turnOrigin: "sdk"` and no `origin`: it comes from the caller, so it
  is listed too, marked "via SDK". A prompt that records another origin, or
  that is marked `isMeta` or `isCompactSummary`, is synthetic, and is filed
  under the person's turn it followed. A prompt with no origin at all, as older
  transcripts store them, is "unknown" and is listed with that mark.
- A message sent while Claude is working (a `queued_command` attachment) joins
  the turn it was sent into, as long as that turn has no answer yet: Claude reads
  it within the turn, and the turn's answer covers both.
- An `isMeta` entry that arrives before Claude has replied is the expansion of
  the prompt it follows (a slash command's or skill's text), so it stays in
  that turn. A slash command is shown as "/name args".
- A turn's answer is its last text, provided no tool call came after it.
- Subagent entries (`isSidechain`) are skipped.
- A command Claude never answers is not a message: a slash command (Claude Code's
  `<command-name>` markup, not a message that merely starts with "/") with no reply and
  no tool call after it (`/clear`, `/model`, `/cost`: Claude Code runs them itself, and
  `/clear` is written as the new session's first entry), and the output Claude Code
  records for such a command (`<local-command-stdout>`). They are filed as "command".
  A command Claude is about to answer reads as one until its reply is written.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional

FIXED_ATTACHMENTS = {
    # Context loaded once per session or per change, not per turn.
    "prompt_snapshot", "skill_listing", "mcp_instructions_delta",
    "deferred_tools_delta", "deferred_tools_record", "instructions",
    "agent_listing_delta", "session_context", "credential_org", "model",
    "environment", "date", "auto_mode",
}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# git commit's first line: "[main abc1234]", "[main (root-commit) abc1234]", "[detached HEAD abc1234]"
COMMIT_LINE = re.compile(r"^\[[^\]]+? ([0-9a-f]{7,40})\]", re.M)
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")

HUMAN = "human"
UNKNOWN = "unknown"
SDK = "sdk"
LISTED = (HUMAN, UNKNOWN, SDK)  # turns a person may have started
# focused's own commands: anything in the plugin's /focused: namespace, and the copies a
# repository can carry for cloud sessions, by their exact names (so an unrelated
# /focused-review stays). Their answers are copies of other answers, so lists and searches
# leave them out.
PLUGIN_NAMESPACE = "/focused:"
REPOSITORY_COMMANDS = ("/focused-answers", "/focused-find", "/focused-context", "/focused-recall")


def is_own_command(prompt) -> bool:
    """Whether a turn is one of focused's own commands. The list and the search
    (store.search) both ask this. The name ends at any whitespace: a message sent
    while the command ran is appended after a blank line."""
    prompt = prompt or ""
    name = re.match(r"\S*", prompt).group()
    return prompt.startswith(PLUGIN_NAMESPACE) or name in REPOSITORY_COMMANDS
COMMAND_NAME = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.S)
COMMAND_ARGS = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.S)


def prompt_text(text: str) -> str:
    """A slash command's markup as the person typed it: "/name args"."""
    name = COMMAND_NAME.search(text or "")
    if not name:
        return text
    args = COMMAND_ARGS.search(text)
    return f"{name.group(1)} {args.group(1)}".strip() if args else name.group(1)


def tokens(value) -> int:
    """Characters / 4. Images are left out: their base64 is not text."""
    if isinstance(value, list):
        value = [b for b in value if not (isinstance(b, dict) and b.get("type") == "image")]
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return len(text) // 4


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def provenance(entry: dict) -> str:
    """'human', the recorded kind of a synthetic prompt, or 'unknown'. A compaction's
    summary and a meta entry are synthetic whatever origin they record: Claude Code gives
    a summary the origin of the turn that was compacted, "human" included."""
    if entry.get("isCompactSummary"):
        return "compact-summary"
    if entry.get("isMeta"):
        return "meta"
    origin = entry.get("origin") or {}
    kind = origin.get("kind") or entry.get("turnOrigin")
    if kind == HUMAN or entry.get("turnOrigin") == HUMAN:
        return HUMAN
    if kind:
        return str(kind)
    return UNKNOWN


@dataclass
class Turn:
    key: str
    seq: int
    origin: str
    started_at: Optional[str]
    prompt: str
    parent_key: Optional[str] = None
    replies: list = field(default_factory=list)
    last: Optional[str] = None
    tool_calls: int = 0
    files: list = field(default_factory=list)
    commits: list = field(default_factory=list)
    pull_requests: list = field(default_factory=list)
    command: bool = False  # written as a slash command (Claude Code's <command-name> markup)

    @property
    def listed(self) -> bool:
        """A turn a person may have started: shown in the answers list."""
        return self.origin in LISTED

    @property
    def answer(self) -> Optional[str]:
        return self.replies[-1] if self.replies and self.last == "text" else None

    @property
    def narration(self) -> list:
        return self.replies[:-1] if self.answer is not None else self.replies

    def add(self, items: list, value: str) -> None:
        if value and value not in items:
            items.append(value)


@dataclass
class Transcript:
    session_id: Optional[str]
    turns: list
    shares: Counter
    title: Optional[str] = None  # the session's title as Claude Code names it (its latest ai-title)
    places: Counter = field(default_factory=Counter)  # each working directory and tool path: its entries
    start: Optional[str] = None  # the working directory the session started in


def load(path) -> Iterator[dict]:
    # errors="replace": a character cut off at the end of a file still being written
    # must not stop the read.
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue  # a line still being written


def parse(path) -> Transcript:
    return parse_entries(load(path))


def parse_entries(entries: Iterable[dict]) -> Transcript:
    shares: Counter = Counter()
    turns: list = []
    tools: dict = {}
    session_id = None
    title = None
    places: Counter = Counter()
    start_cwd = None
    current: Optional[Turn] = None
    last_listed: Optional[Turn] = None

    def start(entry: dict, origin: str, prompt: str, command: bool = False) -> Turn:
        nonlocal current, last_listed
        turn = Turn(
            key=entry.get("uuid") or f"seq-{len(turns)}",
            seq=len(turns),
            origin=origin,
            started_at=entry.get("timestamp"),
            prompt=prompt,
            command=command,
        )
        if not turn.listed and last_listed is not None:
            turn.parent_key = last_listed.key
        turns.append(turn)
        current = turn
        if turn.listed:
            last_listed = turn
        return turn

    for entry in entries:
        if entry.get("isSidechain"):
            continue
        session_id = session_id or entry.get("sessionId")
        cwd = entry.get("cwd") if isinstance(entry.get("cwd"), str) else None
        if cwd:
            places[cwd] += 1
            start_cwd = start_cwd or cwd
        kind = entry.get("type")
        if kind == "ai-title":
            title = str(entry.get("aiTitle") or "").strip() or title
            continue

        if kind == "attachment":
            attachment = entry.get("attachment") or {}
            name = attachment.get("type", "?")
            if name == "queued_command" and provenance(attachment) == HUMAN:
                # A message typed while Claude was working is stored this way. While the
                # turn has no answer yet, Claude reads it within that turn and its answer
                # covers both, so it joins the turn instead of starting one.
                prompt = prompt_text(text_of(attachment.get("prompt")))
                shares["human prompts"] += tokens(prompt)
                if current is not None and current.listed and current.answer is None:
                    current.prompt += f"\n\n(sent while Claude was working) {prompt}"
                    continue
                start(entry, HUMAN, prompt)
                continue
            bucket = "fixed context (system, skills, tools)" if name in FIXED_ATTACHMENTS else f"per-turn attachment: {name}"
            shares[bucket] += tokens(attachment)
            continue

        content = (entry.get("message") or {}).get("content")
        if kind == "user":
            blocks = content if isinstance(content, list) else []
            results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
            if results:
                for block in results:
                    shares["tool results"] += tokens(block.get("content"))
                    if current is not None:
                        _note_result(current, tools.get(block.get("tool_use_id")), block.get("content"))
                continue
            origin = provenance(entry)
            prompt = text_of(content)
            if origin == "meta" and current is not None and not current.replies and not current.tool_calls:
                shares["command and skill expansions"] += tokens(prompt)
                continue  # the expansion of the prompt just before it, not a turn of its own
            bucket = {HUMAN: "human prompts", UNKNOWN: "prompts of unknown origin", SDK: "human prompts"}.get(
                origin, "synthetic prompts (wake-ups, notifications, hook feedback)")
            shares[bucket] += tokens(prompt)
            start(entry, origin, prompt_text(prompt), command=bool(COMMAND_NAME.search(prompt)))

        elif kind == "assistant":
            for block in content or []:
                btype = block.get("type")
                if btype == "thinking":
                    shares["assistant thinking"] += tokens(block.get("thinking", ""))
                elif btype == "tool_use":
                    shares["assistant tool calls"] += tokens(block.get("input"))
                    tools[block.get("id")] = (block.get("name", ""), block.get("input") or {})
                    _note_place(places, cwd, block.get("input"))
                    if current is not None:
                        current.tool_calls += 1
                        current.last = "tool_use"
                        if block.get("name") in EDIT_TOOLS:
                            path = (block.get("input") or {}).get("file_path") or (block.get("input") or {}).get("notebook_path")
                            current.add(current.files, path)
                elif btype == "text":
                    if current is None:
                        shares["assistant narration (text before the answer)"] += tokens(block.get("text", ""))
                        continue
                    current.replies.append(block.get("text", ""))
                    current.last = "text"

    _file_commands(turns)
    for turn in turns:
        answer = turn.answer
        if turn.listed:
            if answer is not None:
                shares["assistant final answers"] += tokens(answer)
            shares["assistant narration (text before the answer)"] += sum(tokens(t) for t in turn.narration)
        else:
            shares["assistant replies to synthetic turns"] += sum(tokens(t) for t in turn.replies)
    return Transcript(session_id=session_id, turns=turns, shares=shares, title=title, places=places, start=start_cwd)


LOCAL_OUTPUT = re.compile(r"\s*<local-command-(stdout|stderr|caveat)>")


def _file_commands(turns: list) -> None:
    """Commands Claude never answered are not messages (see the module's rules); what was
    filed under one goes under the message before it."""
    last = None
    for turn in turns:
        # Only what Claude Code wrote as a command: a typed message that starts with a path
        # ("/Users/me/app.py fails") stays a message, answered or not.
        if (turn.listed and not turn.replies and not turn.tool_calls
                and (turn.command or LOCAL_OUTPUT.match(turn.prompt))):
            turn.origin = "command"
        if turn.listed:
            turn.parent_key, last = None, turn
        else:
            turn.parent_key = last.key if last else None


def _note_place(places: Counter, cwd: Optional[str], tool_input) -> None:
    """The file or folder a tool was given counts for where the session worked
    (focused/repos.py); a relative one is taken from the entry's working directory."""
    if not isinstance(tool_input, dict):
        return
    path = next((v for v in (tool_input.get(k) for k in ("file_path", "notebook_path", "path"))
                 if isinstance(v, str) and v.strip()), None)
    if path is None:
        return
    if not os.path.isabs(path):
        if not cwd:
            return
        path = os.path.join(cwd, path)
    places[os.path.normpath(path)] += 1


def _note_result(turn: Turn, tool, content) -> None:
    if not tool:
        return
    name, tool_input = tool
    text = content if isinstance(content, str) else text_of(content)
    command = str(tool_input.get("command", "")) if isinstance(tool_input, dict) else ""
    if name == "Bash" and "git commit" in command:
        for match in COMMIT_LINE.finditer(text):
            turn.add(turn.commits, match.group(1))
    if name.endswith("create_pull_request") or (name == "Bash" and "pr create" in command):
        for url in PR_URL.findall(text):
            turn.add(turn.pull_requests, url)
