---
description: List this session's messages, each part with the paragraphs that answer it (or Claude's final text where Jev hasn't read the turn), and the wake-ups filed under it.
argument-hint: "[-n N] [--events] [--full]"
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/focused.py" *)
---

Run this command with the Bash tool. Add the options the user gave, if any, at the end,
each as its own single-quoted argument. Pass only `-n` with a number, `--events` and
`--full`; leave out anything else.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/focused.py" --in-session "${CLAUDE_SESSION_ID}" answers
```

The user's options: $ARGUMENTS

Then show the command's output to the user exactly as it is, in a code block. Do not
summarize, shorten or comment on it, and run nothing else.
