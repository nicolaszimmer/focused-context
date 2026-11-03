"""When the prompt cache has most likely expired: stop one message, once.

Claude Code sends the whole conversation with every request and caches it; the cache
this was built for lasts an hour from its last use. A message sent after that sends
the session again without the cache. In a large session that is the most expensive
message there is, and often a cheaper start is better: /clear, with the summary of
where the session left off (focused/summary.py).

So the UserPromptSubmit hook stops the first message a person sends after IDLE_MINUTES
without a reply, in a session of at least MIN_TOKENS tokens, saying: run /clear, or send
again. The next message goes through. A message is stopped at most once per idle spell.

Only messages a person typed are stopped. The hook is told nothing of where a prompt
comes from, and it also fires on turns Claude Code starts on its own; checked with
Claude Code 2.1.317:
- A background task or subagent finishing sends a prompt that starts with
  <task-notification>, and so does every wake-up in a cloud session (GitHub events,
  scheduled check-ins). A prompt that starts with a tag goes through.
- A scheduled task (CronCreate, /loop) sends its stored prompt as it is. So a prompt
  this session scheduled goes through, and a session whose loop uses a placeholder
  prompt (<<…>>) is never stopped.
Stopping one of these would lose it: a blocked prompt never reaches Claude.

The idle time is measured from the transcript's last reply, and the session's size is
that reply's context: its input, cached and output tokens.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import store

IDLE_MINUTES = 59        # the cache lasts 60; a minute's margin
MIN_TOKENS = 100_000
TAIL = 1 << 20           # how much of the transcript's end is read first
MAX_TAIL = 32 << 20      # and at most
SCHEDULERS = ("CronCreate", "ScheduleWakeup")
AUTOMATIC = re.compile(r"\s*<[A-Za-z][\w:-]*[\s/>]")
PLACEHOLDER = re.compile(r"\s*<<[^<>]+>>\s*$")
SESSION = re.compile(r"[A-Za-z0-9_-]{1,100}")


def settings() -> tuple:
    """(minutes, tokens): FOCUSED_IDLE_MINUTES (0 switches the stop off) and
    FOCUSED_IDLE_TOKENS."""
    def number(name: str, default: int) -> int:
        try:
            return max(0, int(os.environ.get(name, default)))
        except ValueError:
            return default
    return number("FOCUSED_IDLE_MINUTES", IDLE_MINUTES), number("FOCUSED_IDLE_TOKENS", MIN_TOKENS)


def _seconds(stamp: str) -> float:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def last_reply(path) -> Optional[tuple]:
    """(timestamp, context tokens) of the transcript's last reply from the model, read
    from the end; None when there is none."""
    with open(path, "rb") as handle:
        end = handle.seek(0, os.SEEK_END)
        size = TAIL
        while True:
            start = max(0, end - size)
            handle.seek(start)
            lines = handle.read(end - start).split(b"\n")
            if start:
                lines = lines[1:]  # cut through
            for line in reversed(lines):
                if b'"assistant"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
                usage = message.get("usage") or {}
                if (entry.get("type") != "assistant" or entry.get("isSidechain") or not entry.get("timestamp")
                        or message.get("model") == "<synthetic>"):
                    continue
                tokens = sum(int(usage.get(key) or 0) for key in (
                    "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
                if tokens:
                    return entry["timestamp"], tokens
            if not start or size >= MAX_TAIL:
                return None
            size *= 4


def scheduled(path) -> tuple:
    """(prompts this session scheduled, whether one of them is a placeholder)."""
    prompts, placeholder = set(), False
    with open(path, "rb") as handle:
        for line in handle:
            if not any(f'"{name}"'.encode() in line for name in SCHEDULERS):
                continue
            try:
                content = (json.loads(line).get("message") or {}).get("content")
            except (ValueError, AttributeError):
                continue
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in SCHEDULERS:
                    prompt = str((block.get("input") or {}).get("prompt") or "").strip()
                    if PLACEHOLDER.match(prompt):
                        placeholder = True
                    elif prompt:
                        prompts.add(prompt)
    return prompts, placeholder


def _note(session: str) -> Path:
    return store.home() / "idle" / session


def _stopped(session: str, stamp: str) -> bool:
    try:
        return _note(session).read_text() == stamp
    except OSError:
        return False


def _remember(session: str, stamp: str) -> None:
    path = _note(session)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)  # store.prune deletes old notes
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
        handle.write(stamp)


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


def check(payload: dict, now: Optional[float] = None) -> Optional[str]:
    """The reason to stop this prompt, or None to let it through."""
    minutes, least = settings()
    prompt, path, session = payload.get("prompt") or "", payload.get("transcript_path"), payload.get("session_id")
    if (not minutes or not prompt.strip() or not path or not session or not SESSION.fullmatch(str(session))
            or AUTOMATIC.match(prompt) or not os.path.exists(path)):
        return None
    reply = last_reply(path)
    if not reply:
        return None
    stamp, tokens = reply
    idle = (time.time() if now is None else now) - _seconds(stamp)
    if idle < minutes * 60 or tokens < least or _stopped(session, stamp):
        return None
    prompts, placeholder = scheduled(path)
    if placeholder or prompt.strip() in prompts:
        return None
    _remember(session, stamp)
    return (f"focused: no reply for {_duration(idle)}, so this session's prompt cache has most likely expired, "
            f"and this message would send its {tokens:,} tokens again without it. "
            "Run /clear to start fresh with a summary of where you left off, or send again to continue here.")
