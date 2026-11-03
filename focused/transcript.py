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
- An `isMeta` entry that arrives before Claude has replied is the expansion of
  the prompt it follows (a slash command's or skill's text), so it stays in
  that turn. A slash command is shown as "/name args".
- A turn's answer is its last text, provided no tool call came after it.
- Subagent entries (`isSidechain`) are skipped.
"""

from __future__ import annotations

import json
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
    """'human', the recorded kind of a synthetic prompt, or 'unknown'."""
    origin = entry.get("origin") or {}
    kind = origin.get("kind") or entry.get("turnOrigin")
    if kind == HUMAN or entry.get("turnOrigin") == HUMAN:
        return HUMAN
    if kind:
        return str(kind)
    if entry.get("isCompactSummary"):
        return "compact-summary"
    if entry.get("isMeta"):
        return "meta"
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
    current: Optional[Turn] = None
    last_listed: Optional[Turn] = None

    def start(entry: dict, origin: str, prompt: str) -> Turn:
        nonlocal current, last_listed
        turn = Turn(
            key=entry.get("uuid") or f"seq-{len(turns)}",
            seq=len(turns),
            origin=origin,
            started_at=entry.get("timestamp"),
            prompt=prompt,
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
        kind = entry.get("type")

        if kind == "attachment":
            attachment = entry.get("attachment") or {}
            name = attachment.get("type", "?")
            if name == "queued_command" and provenance(attachment) == HUMAN:
                # A message typed while Claude was working can be stored this way.
                prompt = text_of(attachment.get("prompt"))
                shares["human prompts"] += tokens(prompt)
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
            start(entry, origin, prompt_text(prompt))

        elif kind == "assistant":
            for block in content or []:
                btype = block.get("type")
                if btype == "thinking":
                    shares["assistant thinking"] += tokens(block.get("thinking", ""))
                elif btype == "tool_use":
                    shares["assistant tool calls"] += tokens(block.get("input"))
                    tools[block.get("id")] = (block.get("name", ""), block.get("input") or {})
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

    for turn in turns:
        answer = turn.answer
        if turn.listed:
            if answer is not None:
                shares["assistant final answers"] += tokens(answer)
            shares["assistant narration (text before the answer)"] += sum(tokens(t) for t in turn.narration)
        else:
            shares["assistant replies to synthetic turns"] += sum(tokens(t) for t in turn.replies)
    return Transcript(session_id=session_id, turns=turns, shares=shares)


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
