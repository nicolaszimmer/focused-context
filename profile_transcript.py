#!/usr/bin/env python3
"""Profile a Claude Code session transcript: where its tokens go, and which
turns a person started.

Measurement only. It reads a transcript (~/.claude/projects/<dir>/<id>.jsonl)
and changes nothing. No model is asked anything: every split below is a fact
the transcript records.

    python3 profile_transcript.py <session.jsonl>            # token shares
    python3 profile_transcript.py <session.jsonl> --answers  # prompt -> reply, previews
    python3 profile_transcript.py <session.jsonl> --answers --full

Tokens are estimated as characters / 4, so shares are approximate. Images are
left out of the estimate: their base64 data is not what the model reads.

A prompt is a person's when the transcript says so (`turnOrigin: "human"` or
`origin.kind: "human"`), and synthetic when it names another origin. A prompt
with neither field, as older transcripts store them, is counted apart as
"origin unknown" and listed with that mark, never guessed.
"""

import json
import sys
from collections import Counter, defaultdict

FIXED_ATTACHMENTS = {
    # Context loaded once per session or per change, not per turn.
    "prompt_snapshot", "skill_listing", "mcp_instructions_delta",
    "deferred_tools_delta", "deferred_tools_record", "instructions",
    "agent_listing_delta", "session_context", "credential_org", "model",
    "environment", "date", "auto_mode",
}


def tokens(value) -> int:
    if isinstance(value, list):
        value = [b for b in value if not (isinstance(b, dict) and b.get("type") == "image")]
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return len(text) // 4


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def provenance(entry) -> str:
    """'human', 'synthetic', or 'unknown' when the entry records no origin."""
    origin = entry.get("origin") or {}
    if entry.get("turnOrigin") == "human" or origin.get("kind") == "human":
        return "human"
    if entry.get("turnOrigin") or origin.get("kind"):
        return "synthetic"
    return "unknown"


def new_turn(prompt, entry, origin="human"):
    return {"prompt": prompt, "replies": [], "synthetic": 0, "last": None,
            "origin": origin, "at": entry.get("timestamp")}


def load(path):
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def profile(path):
    shares = Counter()
    turns = []  # one per human prompt: {prompt, replies: [assistant texts], synthetic: n}
    current = None
    for entry in load(path):
        kind = entry.get("type")
        if kind == "attachment":
            attachment = entry.get("attachment") or {}
            name = attachment.get("type", "?")
            if name == "queued_command" and provenance(attachment) == "human":
                # A message typed while Claude was working can be stored this way.
                prompt = text_of(attachment.get("prompt"))
                shares["human prompts"] += tokens(prompt)
                current = new_turn(prompt, entry)
                turns.append(current)
                continue
            bucket = "fixed context (system, skills, tools)" if name in FIXED_ATTACHMENTS else f"per-turn attachment: {name}"
            shares[bucket] += tokens(entry.get("attachment"))
            continue
        message = entry.get("message") or {}
        content = message.get("content")
        if kind == "user":
            blocks = content if isinstance(content, list) else []
            results = [b for b in blocks if b.get("type") == "tool_result"]
            if results:
                shares["tool results"] += sum(tokens(b.get("content")) for b in results)
            elif provenance(entry) == "human":
                shares["human prompts"] += tokens(text_of(content))
                current = new_turn(text_of(content), entry)
                turns.append(current)
            elif provenance(entry) == "unknown":
                shares["prompts of unknown origin"] += tokens(text_of(content))
                current = new_turn(text_of(content), entry, origin="unknown")
                turns.append(current)
            else:
                shares["synthetic turns (wake-ups, notifications)"] += tokens(text_of(content))
                if current:
                    current["synthetic"] += 1
        elif kind == "assistant":
            for block in content or []:
                btype = block.get("type")
                if btype == "thinking":
                    shares["assistant thinking"] += tokens(block.get("thinking", ""))
                elif btype == "tool_use":
                    shares["assistant tool calls"] += tokens(block.get("input"))
                    if current is not None:
                        current["last"] = "tool_use"
                elif btype == "text" and current is not None:
                    current["replies"].append(block.get("text", ""))
                    current["last"] = "text"
    # A turn's answer is its last text, if nothing came after it; the rest is narration.
    for turn in turns:
        answer = answer_of(turn)
        narration = turn["replies"][:-1] if answer is not None else turn["replies"]
        if answer is not None:
            shares["assistant final answers"] += tokens(answer)
        shares["assistant narration (text before the answer)"] += sum(tokens(t) for t in narration)
    return shares, turns


def answer_of(turn):
    return turn["replies"][-1] if turn["replies"] and turn["last"] == "text" else None


def preview(text, limit, first_line=False):
    text = text.strip()
    if not text:
        return "(no text)"
    short = text.splitlines()[0] if first_line else text
    short = short[:limit]
    return short if short == text else short + " [...]"


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    shares, turns = profile(argv[1])
    if "--answers" in argv:
        full = "--full" in argv
        for i, turn in enumerate(turns, 1):
            answer = answer_of(turn)
            mark = "" if turn["origin"] == "human" else ", origin unknown"
            print(f"## {i}. {turn['at']}  ({turn['synthetic']} synthetic turns folded{mark})")
            if full:
                print("Q: " + (turn["prompt"].strip() or "(no text)"))
                print("A: " + (answer.strip() if answer is not None else "(no answer yet)") + "\n")
            else:
                print("Q: " + preview(turn["prompt"], 200, first_line=True))
                print("A: " + (preview(answer, 600) if answer is not None else "(no answer yet)") + "\n")
        return 0
    total = sum(shares.values()) or 1
    conversation = {k: v for k, v in shares.items() if not k.startswith("fixed")}
    conv_total = sum(conversation.values()) or 1
    print(f"{'bucket':50} {'~tokens':>9} {'of all':>7} {'of conversation':>16}")
    for bucket, count in shares.most_common():
        conv = "" if bucket.startswith("fixed") else f"{100 * count / conv_total:15.1f}%"
        print(f"{bucket:50} {count:9} {100 * count / total:6.1f}% {conv:>16}")
    signal = shares["human prompts"] + shares["assistant final answers"]
    print(f"\nhuman turns: {len(turns)}; prompts + final answers = {100 * signal / conv_total:.1f}% of the conversation")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
