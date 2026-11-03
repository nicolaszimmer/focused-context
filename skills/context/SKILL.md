---
description: Show the summary focused loads after /clear and compaction - your messages, decisions, open items and the paragraphs that answered them.
argument-hint: "[--full]"
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/focused.py" *), Read(~/.claude/focused/summaries/*)
---

Run this command with the Bash tool. If the user gave `--full`, add it at the end as
`'--full'`; leave out anything else.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/focused.py" --in-session "${CLAUDE_SESSION_ID}" context
```

The user's options: $ARGUMENTS

Then show the command's output to the user exactly as it is, in a code block. Do not
summarize, shorten or comment on it.

If the output says instead that the full summary is in a file, because it is longer than
the Bash tool returns at once, read that file with the Read tool, in parts if it is long,
and show all of it the same way. Run nothing else.
