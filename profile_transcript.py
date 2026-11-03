#!/usr/bin/env python3
"""Profile a Claude Code session transcript: where its tokens go, and which
turns a person started.

Measurement only. It reads a transcript (~/.claude/projects/<dir>/<id>.jsonl)
and changes nothing. No model is asked anything: every split is a fact the
transcript records (see focused/transcript.py for the rules).

    python3 profile_transcript.py <session.jsonl>            # token shares
    python3 profile_transcript.py <session.jsonl> --answers  # prompt -> reply, previews
    python3 profile_transcript.py <session.jsonl> --answers --full

Tokens are estimated as characters / 4, so shares are approximate. Images are
left out of the estimate: their base64 data is not what the model reads.
"""

import json
import sys

from focused.cli import format_turns
from focused.transcript import parse


def as_rows(turns):
    """The parsed turns in the shape the index stores, for the shared formatter."""
    return [{
        "turn_key": t.key, "origin": t.origin, "parent_key": t.parent_key,
        "started_at": t.started_at, "prompt": t.prompt, "answer": t.answer,
        "files": json.dumps(t.files), "commits": json.dumps(t.commits),
        "pull_requests": json.dumps(t.pull_requests),
    } for t in turns]


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    parsed = parse(argv[1])
    if "--answers" in argv:
        print(format_turns(as_rows(parsed.turns), full="--full" in argv, events="--events" in argv))
        return 0
    shares = parsed.shares
    total = sum(shares.values()) or 1
    conv_total = sum(v for k, v in shares.items() if not k.startswith("fixed")) or 1
    print(f"{'bucket':58} {'~tokens':>9} {'of all':>7} {'of conversation':>16}")
    for bucket, count in shares.most_common():
        conv = "" if bucket.startswith("fixed") else f"{100 * count / conv_total:15.1f}%"
        print(f"{bucket:58} {count:9} {100 * count / total:6.1f}% {conv:>16}")
    listed = sum(1 for t in parsed.turns if t.listed)
    signal = shares["human prompts"] + shares["assistant final answers"]
    print(f"\nquestions: {listed}; other turns: {len(parsed.turns) - listed}; "
          f"prompts + final answers = {100 * signal / conv_total:.1f}% of the conversation")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
