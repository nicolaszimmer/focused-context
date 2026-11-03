---
description: Search every message and reply indexed for this project, wake-ups included.
argument-hint: "<words>"
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/focused.py" *)
---

Run this command with the Bash tool, adding each word the user gave at the end, after
`--`, as its own single-quoted argument, with any single quote inside a word escaped as `'\''`:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/focused.py" --in-session "${CLAUDE_SESSION_ID}" find --
```

The user's words: $ARGUMENTS

Then show the command's output to the user exactly as it is, in a code block. Do not
summarize or comment on it, and run nothing else.
