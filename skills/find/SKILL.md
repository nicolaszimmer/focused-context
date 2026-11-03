---
description: Search every question and answer indexed for this project.
argument-hint: "<words>"
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/focused.py *)
---

!`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/focused.py --in-session ${CLAUDE_SESSION_ID} find $ARGUMENTS`

Show the matches above to the user exactly as they are, in a code block. Do not
summarize or comment on them, and run nothing else.
