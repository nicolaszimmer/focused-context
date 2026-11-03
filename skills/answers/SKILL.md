---
description: List this session's questions, each with Claude's final answer and the wake-ups filed under it.
argument-hint: "[-n N] [--events] [--full]"
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/focused.py *)
---

!`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/focused.py --in-session ${CLAUDE_SESSION_ID} answers $ARGUMENTS`

Show the list above to the user exactly as it is, in a code block. Do not summarize,
shorten or comment on it, and run nothing else.
