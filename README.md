# focused-context

A Claude Code plugin that finds your answers in a long session. It indexes each
turn: your message, split into its parts, and everything Claude wrote in reply,
including after wake-ups such as GitHub notifications. `/focused:answers` lists
each part of what you said with the paragraphs that answer it;
`/focused:find` searches all of it.

```
## 3. 2026-11-03 07:24
You [instruction]: merge it and switch the hook on in my user settings.
  → **The hook in your user settings:** I couldn't do this one. Your user settings live on your computer, […]
  → - **By hand, on your machine:** […]
You [question, instruction]: Can this be turned into a Claude Plugin?
  → **Can it be a plugin?** Yes, and it fits well. […]
You [question]: What would be the implications?
  → - **Where it runs:** only on your machine (terminal, VS Code, JetBrains, desktop app). […]
  → - **Python:** every machine needs Python 3.10 or later on the path. […]
  → - **Trust:** the hook runs code with your permissions at the end of every turn. […]
   +1 events: 1 task-notification
```

(An excerpt of a real session, lines shortened; that message had three more answer
paragraphs.)

Design notes, measurements and the labelling tools live in
[focused-context-lab](https://github.com/nicolaszimmer/focused-context-lab)
(private).

## Jev

Which paragraph answers which part is decided by Jev, TypeSafe's decision
model, reached through OpenRouter and asked two closed questions
(`focused/jev.py`):

- **What each part does**, as four yes-or-no questions: asks for information,
  asks Claude to do something, settles an open choice, states a fact. A part can
  be several; one that is none of them is tagged `other`.
- **Whether each paragraph answers the part.** Paragraphs at P(answers) ≥ 0.5 are
  shown. A part that asks for something and has none shows "(no answer found)".

Measured on 2026-11-03 against the maintainer's labels for one session (27 parts,
342 paragraphs), over two runs: at 0.5, about 1.7 paragraphs were shown per
part, 82–85% of them answers, and 20–21 of the 22 parts that had an answer showed
one. The turn's final text, which is what focused showed before, held an answer
for every part, but only 12% of its paragraphs were answers. Details in the lab.

**It is off unless you switch it on**, because turned on, the text of your
messages and of Claude's replies goes to OpenRouter, which passes it to TypeSafe.
Requests ask providers not to keep it. To switch it on, set both in the
environment Claude Code runs in, for example under `env` in
`~/.claude/settings.json`:

```json
{ "env": { "FOCUSED_JEV": "1", "FOCUSED_OPENROUTER_API_KEY": "sk-or-…" } }
```

`OPENROUTER_API_KEY` is used when `FOCUSED_OPENROUTER_API_KEY` is not set.

- **When:** the `Stop` hook, which runs async after each turn, asks about what
  Jev hasn't read yet, latest turn first, for up to 90 seconds; the next turn
  picks up the rest. `SessionEnd` doesn't ask, as its time is short. To backfill,
  run `focused index --all --jev`.
- **Cost:** about $0.003 and two seconds per turn, and 57 requests ($0.018) for
  a whole session of seven messages and 342 paragraphs.
- **Kept:** every probability, with a digest of the question's wording. A
  reworded question is asked again rather than reusing old answers.
- **If Jev fails** (no network, an error), the problem goes to
  `~/.claude/focused/errors.log` and the turn shows as without Jev: your message
  and Claude's final text.

## Storage

- Claude Code already keeps every session as JSONL under
  `~/.claude/projects/<dir>/<session>.jsonl`, deleted after
  `cleanupPeriodDays` (30 by default). SQLite is an index beside it, not
  the record.
- One database per project, `~/.claude/focused/<project>.sqlite`, where
  `<project>` is Claude Code's own folder name for the project. It is readable
  by its owner only.
- Tables: `sessions`; `turns`, with full-text search on prompt and final
  answer; `parts` of your messages, with Jev's kinds; `paras`, every paragraph
  Claude wrote, with full-text search; and `pairs`, Jev's answer for each part
  and paragraph.
- Turns older than `cleanupPeriodDays` are deleted, with their parts,
  paragraphs and Jev's answers, each time `focused` indexes a transcript. An
  index that is never run again keeps them after Claude Code has deleted the
  transcript. Delete `~/.claude/focused/` to remove them at once.

## Install

As a Claude Code plugin (Claude Code 2.1.275 or later; Python 3.10 or later
on the `PATH` as `python3`, standard library only):

```
/plugin install focused --marketplace nicolaszimmer/focused-context
```

On older versions: `/plugin marketplace add nicolaszimmer/focused-context`,
then `/plugin install focused@focused-context`. If the one-step form says the
plugin is "not found in any marketplace", use the two steps: an older Claude
Code ignores `--marketplace`. If adding the marketplace fails, your git can't
read the repository: it has to be public, or your git needs GitHub credentials.

**Installed for your user, the plugin indexes every project on that machine.**
What it stores per turn is your prompt, Claude's final answer, the paths of
files edited, commit hashes and pull request URLs: no tool output and no file
contents. To leave a sensitive repository out, run this in it:

```
claude plugin disable focused@focused-context --scope local
```

That writes `"enabledPlugins": {"focused@focused-context": false}` to the
repository's `.claude/settings.local.json`, which is not committed, and the
hooks stop running there. Checked on 2026-11-03: of two projects under one
user install, only the one without this setting was indexed. To switch the
plugin off everywhere, run `/plugin disable focused@focused-context`.

It brings:

- **Two hooks.** `Stop` indexes after each turn, in the background, so the turn
  never waits for it. `SessionEnd` indexes once more when you exit or run
  `/clear`, in the foreground, because exiting cancels a background hook that
  is still running. Its budget is 1.5 s; a 62 MB transcript takes 0.6 s.
  Neither prints anything or fails a turn: errors go to
  `~/.claude/focused/errors.log`.
- **Two commands.** `/focused:answers [-n N] [--events] [--full]` lists this
  session's messages with their answers; `/focused:find <words>` searches every
  message and reply in this project, wake-ups included. Each reindexes the session first, and
  leaves out focused's own commands (`/focused:…`, and a repository's copies
  `/focused-answers` and `/focused-find`, below). Claude runs them through the
  Bash tool, passing each of your words as its own single-quoted argument, with
  a `'` inside a word written as `'\''`. The quoting is Claude's, under Claude
  Code's normal permission checks, which check each part of a compound command:
  a word left unquoted that would chain another command is refused or asked
  about, not run. The commands don't paste your text into a shell command
  themselves, because no heredoc or quoting written in advance holds against
  text that may contain newlines. Their output enters the conversation, so they
  cost what they show.

The index stays in `~/.claude/focused/`, outside the plugin, so it survives
uninstalling the plugin. Delete that folder to remove it.

Without the plugin, the same code runs as a command:

```
pipx install git+https://github.com/nicolaszimmer/focused-context   # gives `focused`
focused index --all             # backfill from ~/.claude/projects
focused answers -n 3 --events   # the last 3 messages, with the folded wake-ups
focused find stripe webhook     # search this project's messages and replies
focused index --all --jev       # also ask Jev about every turn it hasn't read
```

or, from a clone, `python3 scripts/focused.py …`.

## Where it runs

From the settings, plugin and cloud-session docs (code.claude.com/docs, read
2026-11-03), and an end-to-end run of the plugin in Claude Code 2.1.317:

| Surface | Does indexing run? | Does the index persist? |
|---|---|---|
| Terminal, VS Code and JetBrains extensions, desktop app (local) | Yes, with the plugin installed for your user | Yes, in `~/.claude/focused/` |
| Cloud session with one repository | Not through the plugin: cloud sessions install neither the plugins in your user settings nor those a repository declares. Only hooks in that repository's committed `.claude/settings.json` run; see "Cloud sessions" below | No. The VM is reclaimed after inactivity. The conversation is restored on reopen, so the index can be rebuilt (not yet verified that the transcript file is restored at the same path) |
| Cloud session with several repositories, or a thread in a project | No. Repository hooks are not read there | No. The command still works when run by hand |

### Cloud sessions

rapid-review-bot carries the setup for its own single-repository cloud sessions:

- `.claude/hooks/focused.sh` fetches focused-context at a pinned commit, once per
  machine, into `~/.cache/focused-context/<commit>`, and runs it. Its hooks act
  only when `CLAUDE_CODE_REMOTE=true`, so on a machine with the plugin installed
  they do nothing and the plugin indexes.
- `.claude/settings.json` runs it as the `Stop` and `SessionEnd` hooks.
- `.claude/skills/focused-answers` and `focused-find` are the two commands, as
  `/focused-answers` and `/focused-find`, because cloud sessions load a
  repository's skills but not plugins.

A cloud session can fetch focused-context only while it is public: a private
repository is readable only when it is attached to the session, and a session
with several repositories reads no repository's hooks. Until then the hooks
log the failure to `~/.claude/focused/errors.log` and do nothing. To take a
newer focused, change the commit pinned in `focused.sh`.

A prompt sent with `claude -p` or through the SDK is recorded with
`turnOrigin: "sdk"`; `answers` lists it marked "via SDK".

With Jev on, the hook also needs an OpenRouter key and a network that reaches
openrouter.ai. Locally that is your own environment. In the cloud it is the
environment's settings: rapid-review-bot's environment reaches openrouter.ai and
has the key set, and its `.claude/settings.json` sets `FOCUSED_JEV`.

## Decisions

Made on 2026-11-03 by the maintainer:

- An index of turns, in Python, shipped as this plugin, with the index in
  `~/.claude/focused/`.
- rapid-review-bot's cloud sessions are indexed through a repository hook;
  this repository goes public so they can fetch it.
- Jev reads each turn at indexing time, on the strength of the maintainer's
  labels for one session; a part can be several kinds; answers are shown at
  P ≥ 0.5; Jev is switched on for the maintainer.
- Development material lives in focused-context-lab.

Open:

1. Whether 30 days, following `cleanupPeriodDays`, is the right retention.
2. Whether to add a hosted index for cloud and mobile sessions, and for whom.
3. Whether other repositories used in cloud sessions get rapid-review-bot's
   setup.
