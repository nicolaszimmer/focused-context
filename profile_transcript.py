#!/usr/bin/env python3
"""Profile a Claude Code session transcript: where its tokens go, and which
turns a person started.

Measurement only. It reads a transcript (~/.claude/projects/<dir>/<id>.jsonl)
and changes nothing. No model is asked anything: every split below is a fact
the transcript records.

    python3 profile_transcript.py <session.jsonl>            # token shares
    python3 profile_transcript.py <session.jsonl> --answers  # prompt -> reply

Tokens are estimated as characters / 4, so shares are approximate.
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


def is_human(entry) -> bool:
    origin = entry.get("origin") or {}
    return entry.get("turnOrigin") == "human" or origin.get("kind") == "human"


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
            name = (entry.get("attachment") or {}).get("type", "?")
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
            elif is_human(entry):
                shares["human prompts"] += tokens(text_of(content))
                current = {"prompt": text_of(content), "replies": [], "synthetic": 0, "at": entry.get("timestamp")}
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
                elif btype == "text" and current is not None:
                    current["replies"].append(block.get("text", ""))
    # The last assistant text of a human turn is its answer; the rest is narration.
    for turn in turns:
        if turn["replies"]:
            shares["assistant final answers"] += tokens(turn["replies"][-1])
            shares["assistant narration (text before the answer)"] += sum(tokens(t) for t in turn["replies"][:-1])
    return shares, turns


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    shares, turns = profile(argv[1])
    if "--answers" in argv:
        for i, turn in enumerate(turns, 1):
            answer = turn["replies"][-1] if turn["replies"] else "(no answer yet)"
            print(f"## {i}. {turn['at']}  ({turn['synthetic']} synthetic turns folded)")
            print("Q: " + turn["prompt"].strip().splitlines()[0][:200])
            print("A: " + answer.strip()[:600] + "\n")
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
