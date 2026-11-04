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

How the parts fit, what is built and what is planned: [ARCHITECTURE.md](ARCHITECTURE.md).
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

## After /clear and compaction

A session can't be made smaller while it runs: no hook can remove what is already
in the context. Starting fresh can. After `/clear`, and after compaction, a
`SessionStart` hook loads a summary of where the session left off:

- the decisions you made, each with Claude's acknowledgement;
- what is still open: your questions and requests with no answer found;
- the files, commits and pull requests;
- your recent messages, each part with the paragraphs Jev found answer it. A
  turn Jev hasn't read shows your message and Claude's last text.

The session `/clear` ended stays the one this session continues: in a session where
nothing has been said yet, `/focused-context` and `/focused-answers` show it, however
long after the `/clear`. A command Claude never answers (`/clear` itself, which Claude
Code writes as the new session's first entry, `/model`, `/cost`) isn't counted as a
message.

Tool output, narration and wake-ups are left out. For the session this was built
in, that came to about 5,300 tokens for 13 messages, out of about 730,000 tokens
of conversation, three quarters of them tool output.

Claude Code adds at most 10,000 characters of a hook's output to the context, so
the summary loaded keeps under 9,500 (`FOCUSED_SUMMARY_CHARS` changes that, down to
2,000). The
newest messages are kept and older ones dropped first. The full summary, with
every message, is written to `~/.claude/focused/summaries/<session>.md`, readable
by you only, and the loaded one names that file for Claude to read when an earlier
detail matters.

- **After `/clear`** it is the summary of the session you just cleared: the
  `SessionEnd` hook notes which one that was.
- **After compaction** it is the current session's, beside Claude Code's own
  compaction summary.
- `/focused:context` shows it, and `/focused:context --full` the whole of it. A
  full summary over 25,000 characters is read from its file instead, since the
  Bash tool returns at most 30,000 by default. The command may read files in
  `~/.claude/focused/summaries/` without asking, and no others.
- **In a session where nothing has been said yet**, `/focused:context` shows the
  session `/clear` just ended, if it did. Otherwise it doesn't guess, since a new
  session doesn't always continue the latest one: it asks which to show, offering the
  project's three latest sessions by their titles (as Claude Code names them), with the
  time, number of messages and first message of each, and a field for typing another
  session's title.
- **A session is named by its title.** `/focused:context <title>` shows that session:
  its title, or some of its words in any case and order, name it, as does its id or
  the first 8 characters of its id. When several sessions fit, it asks which, the same
  way. The summary, `/focused:answers` and `/focused:find` name each session by its
  title, with the start of its id.

Before building the summary, the hook re-reads the transcript into the index, which
takes under a second. It asks Jev nothing, so it stays quick, and if the re-read
fails, the summary comes from the index as it is. Without Jev, every turn shows as
"not read by Jev".

## Findings and asks

The summary shows the paragraphs that answer your messages. A report's findings often
answer none of them: in the "Kahnemann" session (2026-11-03), the last report listed what
an experiment found and five decisions left to you, but your messages around it asked
where things stood, so the summary kept the status answers and dropped the report.

With `FOCUSED_JEV_LEDGER=1` as well as Jev, Jev also reads the last text Claude wrote in
each turn and each wake-up, and asks three yes-or-no questions about each paragraph:
does it report a finding, does it ask you for a choice or for something only you can do,
does it report progress (tests, CI, commits, pull requests, what a reviewer said). The
summary then has two more sections, each newest last, a later repeat of the same words
replacing an earlier one:

- **Findings:** paragraphs Jev reads as a finding and not as progress, up to 800
  characters each in the summary loaded.
- **Asked of you:** paragraphs Jev reads as asking you. Whether you settled one isn't
  tracked yet: "Decisions" shows what you settled.

**Only last texts are read.** What Claude writes between tool calls is mostly narration
("Checking whether…"). On this repository's longest session, every ask was in a last
text, and no turn had a short last text after a long earlier one.

**Checked by eye, not measured against labels** (2026-11-04):

- On the Kahnemann report: the two review findings (KT1, KT3) and a test result were read
  as findings; the opening status and closing line as progress; decisions 1–4 as asks.
  The fifth, "KT2, KT4–KT13 … are all still undecided", was missed.
- On the newest six messages of this repository's longest session (350 paragraphs of last
  texts): asked alone, the finding question took 85, most of them test, CI and pull
  request reports; with the progress question, 35, the newest of them real findings. 94
  were read as asks.

**Cost:** three questions a paragraph of a last text. On that whole session, about 120
requests and 0.8 million tokens, a third of what Jev's answer questions take on it.

**Switched on by the maintainer on 2026-11-04**, on these checks, without first
measuring the questions against labels. Where it is set:

- a cloud environment installed with `--jev` gets `FOCUSED_JEV_LEDGER=1` in its settings'
  `env` (below, "Cloud environments");
- rapid-review-bot's `.claude/settings.json` sets it in its `env`;
- locally, add `"FOCUSED_JEV_LEDGER": "1"` under `env` beside `FOCUSED_JEV`.

It goes in `env`, not on a hook's command, because the summary after `/clear` and
`/focused-context` need it as much as the `Stop` hook that asks Jev. Switched off, no
summary shows findings or asks, even of a session Jev read with it on. The hosted index keeps Jev's readings from focused-context-worker's `0003_marks`; a
Worker without it ignores them, and a session read back from it has no findings or asks.

## After an hour without a reply

Claude Code caches the conversation it sends with each request, and in the sessions
this was built in the cache lasts an hour from its last use. A message sent after
that sends the whole session again without the cache: in a large session, the most
expensive message there is. Often `/clear`, with the summary above, is the better
start.

So the first message you send after 59 minutes without a reply, in a session of at
least 100,000 tokens, is stopped once by the `UserPromptSubmit` hook. From a run in
Claude Code 2.1.317, with the wait set to one minute:

```
UserPromptSubmit operation blocked by hook:
focused: no reply for 1 min, so this session's prompt cache has most likely expired, and this message would send its 46,857 tokens again without it. Run /clear to start fresh with a summary of where you left off, or send again to continue here.

Original prompt: Reply with just the word two.
```

- **Send it again** and it goes through. The next stop comes after the next hour
  without a reply. Claude Code shows the message under "Original prompt" to copy;
  it doesn't put it back in the input box.
- **Only messages you type are stopped.** A stopped prompt never reaches Claude, so
  stopping a turn Claude Code starts itself would lose it, and the hook runs on
  those too without being told which they are. A background task or subagent
  finishing, and in a cloud session every wake-up (GitHub events, scheduled
  check-ins), sends a prompt that starts with `<task-notification>`: any prompt
  that starts with a tag goes through. A scheduled task (CronCreate, `/loop`) sends
  its stored prompt, so a prompt the session scheduled goes through, and a session
  whose loop uses a placeholder prompt (`<<…>>`) is never stopped. All three
  checked in Claude Code 2.1.317.
- **Measured from the transcript:** the time since its last reply, and that reply's
  context (input, cached and output tokens) as the session's size.
- **Settings:** `FOCUSED_IDLE_MINUTES` changes the 59 (`0` switches the stop off)
  and `FOCUSED_IDLE_TOKENS` the 100,000.
- A prompt sent with `claude -p --resume` to a large session idle for an hour is
  stopped too: the hook can't tell it from a typed one.

## The hosted index

A cloud session's container is reclaimed after a while, and the next session starts with
an empty `~/.claude/focused`. The hosted index keeps the index where every session can
read it: [focused-context-worker](https://github.com/nicolaszimmer/focused-context-worker),
a Cloudflare Worker with a D1 database, on your own Cloudflare account
(`focused/remote.py`).

**It is off until you set it up**, as it sends the same text the local index holds to
your Worker. Deploy the Worker (its README has the steps), then set both in the
environment Claude Code runs in, for a cloud environment among its environment variables:

```
FOCUSED_REMOTE_URL=https://focused-context.<your-subdomain>.workers.dev
FOCUSED_REMOTE_TOKEN=<the Worker's FOCUSED_TOKEN>
```

A cloud environment also needs to reach the Worker: under **Network access** in the
environment's settings, add its host to the allowed domains, or choose a broader level.

- **Pushing:** the `Stop` hook pushes the session after each turn, after Jev; `SessionEnd`
  for up to 8 seconds; `focused index` each session it indexes, so `focused index --all`
  fills the hosted index from this machine's transcripts. A push carries only the turns
  whose rows changed since the last one, in pieces of about 512 KB, and names the turns
  deleted here, so they go there too. A piece the Worker took is noted at once: a push cut
  short goes on from there next time.
- **Reading back:** the sessions `/focused-context` offers in a new session include the
  hosted index's, so a new container offers those of earlier containers, and the one
  chosen is read into this machine's index. They come those of this session's
  repositories first, then those of related repositories (see "Recall"), and other
  projects' sessions join them when they worked in one of those.
  `focused answers --session <title>` and
  `focused context --session <title>` read a session this machine doesn't have the same
  way. `/focused-find` adds the matches from sessions not on this machine.
- **Retention is each side's own:** a turn deleted here for its age isn't deleted there.
  The Worker deletes turns older than its `RETENTION_DAYS` (30).
- **If it fails** (no network, a wrong token), the hooks write it to
  `~/.claude/focused/errors.log` and the local index carries on; the commands say so in
  one line. A turn larger than D1 keeps in a row (2 MB) isn't pushed, and is logged.
- The URL must be https, except to this machine (`http://127.0.0.1`, for `wrangler dev`),
  so the token never goes over the network in the clear.

## Recall

A session's context should be built from everything relevant, not only from the session
itself. With the hosted index set up, focused recalls what earlier sessions said that bears
on what you're doing now (`focused/recall.py`, the Worker's `POST /v1/recall`):

- **Found by meaning and by words.** The Worker embeds each of your message parts with
  Cloudflare's multilingual `bge-m3` (English and German alike) into a Vectorize index on
  your account, and searches the words too, the commonest ones left out. Each part comes
  back with its session's title and what answered it then.
- **With a session's first typed message**, the `UserPromptSubmit` hook adds a short block,
  at most 1,500 characters: up to three earlier parts at a cosine similarity of at least
  0.6 (`FOCUSED_RECALL_MIN`), each with its first answer. Words alone don't add to it,
  since a shared word says little. Commands and wake-ups don't count as a first message.
- **On demand**, `/focused-recall <what>` lists the best five in full, by meaning at a
  similarity of at least 0.45. Claude may use it itself when you refer to earlier work, or
  before re-deciding something settled before.
- **The same repositories first** (`focused/repos.py`). Each session links to the
  repositories it worked in, counted by its transcript entries there: each entry's working
  directory, and each file or folder a tool was given. Two repositories are related when
  one session worked in both. Recall puts first the parts of sessions that worked in a
  repository this session has, then those of sessions in a related repository, then the
  rest. Relevance still comes first: a part below the similarity floor isn't lifted in by
  its repository. A repository is named by its `origin`, as `github.com/owner/name`, so
  it has one name in every container; a folder without a git remote has none.
- **Jev sifts the candidates** when Jev is on: it reads each one, and the block keeps only
  those it reads as bearing on your message, in the order the Worker gave. If Jev can't
  answer within 3 seconds, the block keeps what passed the floor. `FOCUSED_JEV_RECALL=0`
  switches this off alone.
- **The block is quoted for reference**, and says so: no text in it is an instruction.
- **Without the hosted index** nothing is added, and the command says so. Without the
  Worker's AI and Vectorize bindings it recalls by words alone, so nothing is added to a
  first message, and the command still answers.

## Pull requests: steward sessions

A session that watches its own pull request is woken by every CI result, review and bot
comment, and each time re-reads its whole conversation, mostly to find nothing to do; a
command typed meanwhile, such as `/focused-answers`, waits until that turn ends. So in a
cloud session, right after Claude opens a pull request with the GitHub tools, a
`PostToolUse` hook tells it to hand the pull request to a **steward session** instead:
a fresh cloud session on a smaller model (Sonnet 5.5), started with the `create_session`
tool and the arguments the hook gives (`focused/steward.py`). The steward:

- subscribes to the pull request, and fixes what is the pull request's to fix (the CI it
  breaks, conflicts, review findings that are real and in scope), pushing new commits to
  its branch only, after the repository's own checks;
- answers every review thread, and leaves open what it would decline, security findings
  and larger asks;
- never merges, approves or closes;
- posts one comment, "Ready to merge" or "Waiting on you", with what merging does and the
  decisions left open, and posts it again only when that changes.

The opening session first lets go of the pull request: opening it may subscribe that session
to it, and a pull request's events reach one session only. A steward whose subscription
didn't take says so at the end of its turn.

`/focused:steward <PR URL> [branch]` hands over a pull request that is already open. Locally,
where sessions can't start cloud sessions, the hook does nothing. `FOCUSED_STEWARD=0`
switches it off, and `FOCUSED_STEWARD_MODEL` picks another model. A pull request from a
fork isn't handed over: the steward couldn't push to it.

## Storage

- Claude Code already keeps every session as JSONL under
  `~/.claude/projects/<dir>/<session>.jsonl`, deleted after
  `cleanupPeriodDays` (30 by default). SQLite is an index beside it, not
  the record.
- One database per project, `~/.claude/focused/<project>.sqlite`, where
  `<project>` is Claude Code's own folder name for the project. It is readable
  by its owner only.
- Tables: `sessions`, with each session's title; `turns`, with full-text search on prompt and final
  answer; `parts` of your messages, with Jev's kinds; `paras`, every paragraph
  Claude wrote, with full-text search; and `pairs`, Jev's answer for each part
  and paragraph; `session_repos`, the repositories each session worked in, by
  its entries in each; and `pushed`, the digest of each turn as last pushed to
  the hosted index.
- Beside the databases: `summaries/<session>.md`, the full summary of each
  session summarized, and `idle/<session>`, the note that a message was stopped
  after an hour without a reply.
- Turns older than `cleanupPeriodDays` are deleted, with their parts,
  paragraphs and Jev's answers, each time `focused` indexes a transcript. A
  session left with no turns loses its summary and note too, and any summary or
  note not rewritten within that time is deleted as well. An
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
What it stores per turn is your message, also split into its parts; every
paragraph Claude wrote, wake-up replies included; and the paths of files edited,
commit hashes and pull request URLs. No tool output and no file contents. With
Jev switched on, the parts and paragraphs are also sent to OpenRouter (see "Jev").
To leave a sensitive repository out, run this in it:

```
claude plugin disable focused@focused-context --scope local
```

That writes `"enabledPlugins": {"focused@focused-context": false}` to the
repository's `.claude/settings.local.json`, which is not committed, and the
hooks stop running there. Checked on 2026-11-03: of two projects under one
user install, only the one without this setting was indexed. To switch the
plugin off everywhere, run `/plugin disable focused@focused-context`.

It brings:

- **Four hooks.**
  - `Stop` indexes after each turn, in the background, so the turn never waits
    for it. With Jev on, it asks Jev here too.
  - `SessionEnd` indexes once more when you exit or run `/clear`, in the
    foreground, because exiting cancels a background hook that is still
    running. Its budget is 1.5 s; a 62 MB transcript takes 0.6 s.
  - `SessionStart`, only after `/clear` and compaction, loads the summary of
    where the session left off (see "After /clear and compaction").
  - `UserPromptSubmit` stops the first message you type after an hour without a
    reply, once (see "After an hour without a reply"). It reads only the end of
    the transcript, and fails open.

  None of them fails a turn: errors go to `~/.claude/focused/errors.log`.
- **Three commands.** `/focused:context [--full]` shows the summary loaded after
  `/clear` (see "After /clear and compaction"). `/focused:answers [-n N] [--events] [--full]` lists this
  session's messages with their answers; `/focused:find <words>` searches every
  message and reply in this project, wake-ups included. Each reindexes the session first, and
  leaves out focused's own commands (`/focused:…`, and a repository's copies
  `/focused-answers`, `/focused-find` and `/focused-context`, below). Claude runs them through the
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
| Cloud session, any number of repositories | Yes, with focused installed by the environment's setup script; see "Cloud environments" below. Cloud sessions install neither the plugins in your user settings nor those a repository declares | Not on the VM, which is reclaimed after inactivity. With the hosted index set up, yes, in your Worker; see "The hosted index" above |
| Cloud session with one repository, without the setup script | Only if that repository's committed `.claude/settings.json` runs focused; see "One repository's own setup" below | As above |
| A thread in a project | No. Threads read neither repository hooks nor, as far as the docs say, the environment's | No. The command still works when run by hand |

### Cloud environments

A cloud environment's setup script runs before Claude Code starts, in every session of
that environment, and Claude Code reads hooks and skills from the container's own
`~/.claude`. So one setup script installs focused for every session, whatever the
number of repositories in it. Checked on 2026-11-03: hooks merged into
`~/.claude/settings.json` by the setup script ran in a session with two repositories.

In a session, open the cloud environment menu in the title bar, then **Edit**, then
**Setup script**, and add:

```sh
U=https://github.com/nicolaszimmer/focused-context
REF=$(git ls-remote "$U" refs/heads/main | cut -f1); [ -n "$REF" ] || exit 1
D="$HOME/.cache/focused-context/$REF"
[ -f "$D/scripts/install-cloud.sh" ] || { git init -q "$D" &&
  git -C "$D" fetch -q --depth 1 "$U" "$REF" &&
  git -C "$D" -c advice.detachedHead=false checkout -q FETCH_HEAD; }
sh "$D/scripts/install-cloud.sh" --jev --follow main
```

`install-cloud.sh` (`focused install`):

- merges the four hooks into `~/.claude/settings.json`: `Stop` (async), `SessionEnd`,
  `SessionStart` after `/clear` and compaction, and `UserPromptSubmit`. Other hooks and
  settings stay as they are, and running it again replaces what it installed.
- writes `/focused-answers`, `/focused-find`, `/focused-context` and `/focused-recall` as personal skills
  in `~/.claude/skills`. A personal skill takes precedence over a repository's skill of
  the same name, so a repository's own copies are shadowed, not doubled.
- sets `FOCUSED_INSTALL=environment` in the settings' `env`, so a repository's own
  focused hooks, such as rapid-review-bot's, stand down instead of running twice.
- `--jev` asks Jev in the `Stop` hook, and switches on findings and asks
  (`FOCUSED_JEV_LEDGER=1` in the settings' `env`, unless it is already set: a `0` you set
  stays through every update; `--uninstall` removes the `1` it set). The environment then needs
  `FOCUSED_OPENROUTER_API_KEY` or `OPENROUTER_API_KEY` among its variables, and access to
  openrouter.ai. The setup script itself can't read the variables; the hooks can.
- `--follow main` keeps the install at focused-context's main; see "Updates" below.
- `--uninstall` removes what it installed, and nothing else.

Every session of the environment is indexed. Keep a repository out by using another
environment for it.

**Updates.** A cloud environment runs its setup script once, keeps what it wrote, and
starts later sessions from that for about seven days. It runs the script again only when
the script, or the environment's allowed network hosts, change. So the setup script alone
would keep the focused it fetched for up to a week. With `--follow main`, focused also
installs a `SessionStart` hook that runs in the background as each session starts or
resumes (`focused update`, `focused/update.py`):

- it asks GitHub where `main` is (`git ls-remote`, well under a second);
- if `main` has moved, it fetches that commit into `~/.cache/focused-context/<commit>` and
  runs that version's own installer, which takes over the hooks and commands;
- Claude Code usually picks up changed hooks while a session runs; otherwise the new
  version takes over from the next session;
- it fails open: without network, git or a working commit, the session keeps the focused
  it has, and the reason goes to `~/.claude/focused/errors.log`.

So a merge to focused-context's main reaches every new cloud session of the environment by
itself. To stay on one version instead, leave out `--follow main` and set `REF` to a
commit; a changed setup script rebuilds the cached environment.

### One repository's own setup

Without the setup script, a repository can carry focused for its own
single-repository cloud sessions. rapid-review-bot does:

- `.claude/hooks/focused.sh` fetches focused-context at a pinned commit, once per
  machine, into `~/.cache/focused-context/<commit>`, and runs it. Its hooks act
  only when `CLAUDE_CODE_REMOTE=true`, so on a machine with the plugin installed
  they do nothing and the plugin indexes. They also stand down when the environment
  installed focused (`FOCUSED_INSTALL=environment`).
- `.claude/settings.json` runs it as the `Stop`, `SessionEnd`, `SessionStart` and
  `UserPromptSubmit` hooks, `SessionStart` after `/clear` and compaction only.
- `.claude/skills/focused-answers`, `focused-find` and `focused-context` are the
  three commands, as `/focused-answers`, `/focused-find` and `/focused-context`,
  because cloud sessions load a repository's skills but not plugins.

A cloud session can fetch focused-context only while it is public: a private
repository is readable only when it is attached to the session. Otherwise the hooks
log the failure to `~/.claude/focused/errors.log` and do nothing. To take a
newer focused, change the commit pinned in `focused.sh`.

A prompt sent with `claude -p` or through the SDK is recorded with
`turnOrigin: "sdk"`; `answers` lists it marked "via SDK".

With Jev on, the hook also needs an OpenRouter key and a network that reaches
openrouter.ai. Locally that is your own environment. In the cloud it is the
environment's settings: rapid-review-bot's environment reaches openrouter.ai and
has the key set, and its `.claude/settings.json` sets `FOCUSED_JEV` on its hooks and
`FOCUSED_JEV_LEDGER` in its `env`.

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
- After an hour without a reply, the first message typed is stopped once, with
  "run /clear or send again"; at 59 minutes, to err on the side of caution.
- Cloud sessions get focused from the environment's setup script, for any number of
  repositories (`install-cloud.sh`).
- Cloud sessions follow focused-context's main (`--follow main`): a merge to main is a
  release, which new sessions pick up by themselves.
- The index and a ledger of decisions, to-dos and findings will be hosted on the
  maintainer's Cloudflare account, in a Worker in its own repository. New Jev questions
  for it ship switched off and are judged by their output.
- `/focused-context` in a session where nothing has been said yet doesn't load the
  latest session on a guess, as not every session continues the last one's objective:
  it offers the project's three latest sessions, and a field for any other, as Claude
  Code's own questions do. Right after `/clear` it still shows the session cleared.
- Sessions are named by their titles, as Claude Code names them: a person names one by
  its title, and focused shows each by its title.
- A session's context is built from everything relevant: focused recalls earlier sessions
  by keywords and embeddings, with a session's first message and on demand, before the
  ledger is built.
- focused-context's main is protected on GitHub (a pull request is required; direct and
  force pushes are blocked), since cloud sessions follow it.
- How sessions relate is a graph of repositories: first the repositories a session works
  in, then those worked on together with them in another session. Recall and the choice
  of a session follow it.

Made on 2026-11-04 by the maintainer:

- `/focused-recall` keeps the permission pattern the other commands have: `focused.py`
  with any arguments.
- Quoting recalled text as JSON strings, under a header that says it is data, is enough.
  The quoting keeps recalled text from passing for the block's own lines; it doesn't stop
  Claude from reading an instruction in it, which the header tells it not to follow.
- The hosted index runs on Workers Paid.
- Recall's similarity floors are 0.6 for a first message and 0.45 on demand, set from
  bge-m3's scores on real sessions (a paraphrase 0.87, a related question 0.78, an unrelated
  one 0.43).
- Jev sifts the candidates for a first message whenever Jev is on.
- The block added to a first message keeps what answered each earlier message, quoted as
  the messages are: the answers carry most of what recall is for.
- Findings and asks in the summary are built next, before an index of development
  documents and an MCP endpoint.
- Findings and asks are switched on (`FOCUSED_JEV_LEDGER=1`) on their output, checked by
  eye, without first measuring the questions against labels.
- A last text with no words leaves the text before it as the turn's last text (the
  review thread on `_final`, #13).
- A working session doesn't watch its pull requests: it hands each to a steward session
  (a fresh cloud session) on Sonnet 5.5, so PR events don't wake it and commands typed
  meanwhile, such as `/focused-answers`, don't wait behind them. Steward sessions first,
  then Worker triage; the view page is built in parallel in its own session.

Open:

1. Whether 30 days, following `cleanupPeriodDays`, is the right retention.
2. The smallest session the stop after an hour applies to: 100,000 tokens, Claude's
   suggestion, not yet confirmed.
3. The limit on the summary loaded after `/clear` and compaction: 9,500
   characters, set by Claude under Claude Code's 10,000.
4. Which sessions count as one project: every cloud session with more than one
   repository runs in `/home/user`, so all of them share the project `-home-user`, and
   the hosted index's search spans all of them. Recall and the choice of a session now go
   by repository, which may settle this: whether the project should still matter
   anywhere (search, naming a session by its title) is open.
