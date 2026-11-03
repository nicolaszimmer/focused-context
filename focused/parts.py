"""Split a person's message into parts and Claude's texts into paragraphs.

Jev is asked about each part and each paragraph, so these are the units it
reads. The rules are the ones the labelling sheet used when Jev was measured
(focused-context-lab, eval/): change them and the measurement no longer holds.
"""

from __future__ import annotations

import re
from typing import Optional

QUEUED = "\n\n(sent while Claude was working) "
SENTENCE_END = re.compile(r"(?<=[.?!])\s+(?=[A-Z0-9\"'(„“])")
NUMBER_ONLY = re.compile(r"(?:^|\s)\d+\.$")
TOP_ITEM = re.compile(r"^(?:[-*+] |\d+[.)] )")
SUMMARY = re.compile(r"<summary>(.*?)</summary>", re.S)


def _sentences(line: str) -> list:
    """A sentence each; a fragment of three words or fewer stays with the one before,
    and so does the sentence after a bare list number such as "1."."""
    out = []
    for piece in (p for p in SENTENCE_END.split(line.strip()) if p):
        if out and (NUMBER_ONLY.search(out[-1]) or len(piece.split()) <= 3):
            out[-1] += " " + piece
        else:
            out.append(piece)
    return out


def message_parts(prompt: str) -> list:
    """[(text, queued)] in order: a line each, then a sentence each. A message sent
    while Claude was working is marked queued. An indented line, such as a command's
    output, stays with the line before it."""
    parts = []
    for index, block in enumerate((prompt or "").split(QUEUED)):
        lines = []
        for line in block.splitlines():
            if not line.strip():
                continue
            if lines and (line[:1].isspace() or line.lstrip().startswith("⎿")):
                lines[-1] += "\n" + line
            else:
                lines.append(line)
        for line in lines:
            pieces = [line] if "\n" in line else _sentences(line)
            parts.extend((piece, index > 0) for piece in pieces)
    return parts


def paragraphs(text: str) -> list:
    """Blank-line paragraphs. A top-level list splits into its items, each keeping its
    indented sub-items; a fenced block stays whole."""
    out, current, fenced = [], [], False

    def flush():
        if current and "".join(current).strip():
            out.append("\n".join(current).strip("\n"))
        current.clear()

    for line in (text or "").splitlines():
        if line.lstrip().startswith("```"):
            if not fenced:
                flush()
            current.append(line)
            fenced = not fenced
            if not fenced:
                flush()
            continue
        if fenced:
            current.append(line)
        elif not line.strip():
            flush()
        elif TOP_ITEM.match(line) and current:
            flush()
            current.append(line)
        else:
            current.append(line)
    flush()
    return out


def place(prompt: Optional[str], is_reply: bool) -> str:
    """Where a paragraph was written, as Jev is told: in the reply to the message, or
    after a wake-up, named by its notification summary or its first line."""
    if is_reply:
        return "reply to the message"
    match = SUMMARY.search(prompt or "")
    if match:
        label = match.group(1).strip()
    else:
        first = (prompt or "").strip().splitlines()[:1]
        label = (first[0] if first else "(no text)")[:160]
    return f"after a wake-up ({label})"
