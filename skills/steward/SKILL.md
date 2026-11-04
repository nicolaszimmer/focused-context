---
description: Hand a pull request to a steward session - a fresh cloud session on a smaller model that drives it to mergeable, answers its reviews and says what waits on you - so this session isn't woken by its CI and reviews. In cloud sessions, focused offers this by itself right after a pull request is opened.
argument-hint: "<pull request URL> [head branch]"
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/focused.py" *)
---

The user's options: $ARGUMENTS

The first option is the pull request's URL; the second, if given, its head branch. If the
head branch isn't given, read it from the pull request with the GitHub tools (its head
ref), and use it only if the head is in the same repository: a steward can't push to a fork,
so for a fork say so and run nothing. Then run this command with the Bash tool, the URL and
the branch each as its own single-quoted argument, with any single quote inside escaped as
`'\''`:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/focused.py" --in-session "${CLAUDE_SESSION_ID}" steward '<URL>' --head '<branch>'
```

If the output starts with `focused:` and names no arguments, show it to the user and stop.
Otherwise it says how to hand the pull request over and gives the arguments: call the
create_session tool with exactly those arguments, unsubscribe this session from the pull
request if it is subscribed, and tell the user in one line that a steward session drives
it, on which model. Run nothing else.
