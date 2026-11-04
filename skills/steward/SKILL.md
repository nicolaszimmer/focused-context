---
description: Hand a pull request to a steward session - a fresh cloud session on a smaller model that drives it to mergeable, answers its reviews and says what waits on you - so this session isn't woken by its CI and reviews. In cloud sessions, focused offers this by itself right after a pull request is opened.
argument-hint: "<pull request URL> [head branch]"
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/focused.py" *)
---

The user's options: $ARGUMENTS

The first option is the pull request's URL; the second, if given, its head branch. Read the
pull request with the GitHub tools whether or not the branch is given: its head must be in
the same repository (a steward can't push to a fork), so for a fork say so and run nothing.
Always pass the pull request's own head ref as the branch. If a branch was given and differs
from that ref, say so and run nothing: the steward must push to the pull request's branch, not
to another one.

Before anything goes on a command line, check both values character by character, and run
nothing if either fails; say which one and why:

- the URL is exactly `https://github.com/<owner>/<name>/pull/<number>`, the owner and name
  made only of `A-Z a-z 0-9 _ . -` and the number only of digits;
- the branch is made only of `A-Z a-z 0-9 _ . / -`, doesn't start with `-` or `/`, and
  doesn't contain `..`.

A branch name can hold quotes, `$` and parentheses, and whoever can push to the repository
chooses it: these checks keep anything the shell reads off the command line, so quoting
can't be got wrong. Then run this command with the Bash tool, the URL and the branch each in
single quotes:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/focused.py" --in-session "${CLAUDE_SESSION_ID}" steward '<URL>' --head '<branch>'
```

Go on only if the output contains a JSON object with `source_url` and `prompt`: those are
the arguments. For any other output (a problem the command names, a traceback, anything
else), show it to the user and stop. Otherwise first call unsubscribe_pr_activity for the
pull request (a pull request's events go to one session only), then call the create_session
tool with exactly those arguments, and tell the user in one line that a steward session drives
it, on which model. Run nothing else.
