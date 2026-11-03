# Architecture

What focused is for, how its parts fit, what is built and what is planned. The
decisions behind it are at the end. The early design notes, measurements and labelling
tools are in [focused-context-lab](https://github.com/nicolaszimmer/focused-context-lab)
(`docs/design.md`, from before most of this was decided).

## The goal

Make work with Claude Code reliable, focused and token-efficient:

- cut the chaff from what a session carries;
- keep decisions, to-dos and findings across `/clear`, compaction, new sessions and
  devices;
- keep pull-request checks and reviews from stalling the work a session is for.

## Constraints

- **Claude runs as the `claude` binary on the maintainer's plan**, not on API tokens.
  Anything that starts work starts a Claude Code session.
- **Chat only, in cloud sessions**, from the Claude app on an iPhone and a MacBook. VS
  Code is only for looking at code. There are no Claude Projects.
- **A cloud session's container is reclaimed** after a while, and the next one starts
  with an empty home directory.
- **Hooks are the only code that runs on every turn.** In cloud sessions, hooks written
  into `~/.claude/settings.json` by the environment's setup script run in sessions with
  any number of repositories (checked on 2026-11-03), but not in a project's threads.

## The principle

Code captures and acts; Claude does the work. Every step that must not be forgotten runs
in a hook or a service, never in Claude's habits.

## The parts

```
any cloud session, iPhone or Mac                      (installed per environment)
  hooks (code): Stop, SessionEnd, SessionStart, UserPromptSubmit
   ├─ index each turn ──────────────▶ local index (SQLite, ~/.claude/focused)
   │    split messages into parts;        sessions · turns · parts · paragraphs ·
   │    Jev: what each part does,         Jev's readings · titles
   │    which paragraphs answer it
   ├─ push what changed ────────────▶ hosted index: focused-context-worker
   │                                   (Cloudflare Worker + D1, the maintainer's account)
   ├─ after /clear, compaction ─────▶ summary of where the session left off
   ├─ after an hour idle ───────────▶ stop one typed message: "run /clear or send again"
   └─ /focused-context in a new session ─▶ choose a session by title; read it back

planned
   ledger: decisions waiting on you · to-dos · findings · PRs, from more Jev questions
   view page served by the Worker: answer a decision there, and it reaches the ledger
   GitHub ── webhook ──▶ Worker triage (rules) ── actionable ──▶ a fresh steward session
```

| Part | What it does | Where | State |
|---|---|---|---|
| **Capture** | The hooks index each turn: the person's message split into its parts, every paragraph Claude wrote, wake-ups filed under the message they follow, files, commits and PRs. | `focused/transcript.py`, `parts.py`, `store.py`; hooks in `hooks/hooks.json` | Built (0.1–0.3) |
| **Install** | Puts the hooks and the three commands into every session of a cloud environment from its setup script; locally, the plugin does it. | `scripts/install-cloud.sh`, `focused/install.py` | Built (0.5) |
| **Updates** | The setup script runs about once a week, so a `SessionStart` hook checks focused-context's main as each session starts and installs a newer version itself: a merge to main is a release. | `focused/update.py` | Built (0.7) |
| **Extraction** | Jev, TypeSafe's decision model through OpenRouter, reads each turn at indexing time: what each part of a message does, and which paragraphs answer it. Answers are kept with a digest of the question's wording. | `focused/jev.py` | Built (0.3), on for the maintainer |
| **Local index** | One SQLite database per project, readable by its owner only, pruned after 30 days. | `focused/store.py` | Built |
| **Restore** | After `/clear` and compaction, the `SessionStart` hook loads a summary of the session: decisions, open asks, files and PRs, and the latest messages with their answers, under 9,500 characters. The whole summary is kept in a file. | `focused/summary.py` | Built (0.4) |
| **Idle stop** | After 59 minutes without a reply, in a session of 100,000 tokens or more, the first typed message is stopped once, since it would resend the whole session without the prompt cache. | `focused/idle.py` | Built (0.4) |
| **Hosted index** | The hooks push the turns that changed to a Worker on the maintainer's Cloudflare account. Any later session reads them back and searches them, so the index outlives the container. | `focused/remote.py`; [focused-context-worker](https://github.com/nicolaszimmer/focused-context-worker) | Built (0.6; focused-context-worker merged), not yet deployed |
| **Choosing a session** | In a session where nothing has been said yet, `/focused-context` offers the project's three latest sessions by title, with a field for any other. Sessions are named by their titles everywhere. | `focused/cli.py`, `skills/context` | Built (0.6) |
| **Recall** | What earlier sessions said that bears on what the person does now, found by meaning (Workers AI's bge-m3 embeddings of each message part, in Vectorize) and by words, each with what answered it. A short block joins a session's first typed message; `/focused-recall` gives more, and Claude may use it itself. Jev can sort the candidates, switched off for now. | `focused/recall.py`; the Worker's `src/recall.ts` | Built, in review |
| **Ledger** | Three more Jev questions about Claude's paragraphs: does it ask the person to decide, does it report a checked result, does it commit Claude to doing something. Later messages are paired against those asks, the way answers are paired today, so an ask closes when it is settled. The ledger collects what is waiting on the person, the to-dos, the findings and the PRs across sessions. | to be built | Planned |
| **View page** | A page served by the Worker that shows the ledger. A decision answered there is written to it. | to be built | Planned |
| **Restore from the ledger** | The summary's decisions and open items come from the ledger, which spans every session and device, not one transcript. | to be built | Planned |
| **Triage and steward sessions** | Working sessions stop subscribing to their PRs. GitHub sends webhooks to the Worker, plain rules drop what needs nothing, and each actionable event starts a fresh, small session through a scheduled task's API trigger, on the maintainer's plan. A PR counts as ready in the ledger only when CI is green and no finding is open. | to be built | Planned |

## Why the steward sessions

In the session this was designed in, wake-ups (GitHub events, check-ins, background
results) took 65% of all tokens (778 requests, 381.5 million tokens read). Every wake-up
resends the whole working session; a steward session carries only its PR.

A scheduled task's API trigger (`POST /v1/claude_code/routines/<id>/fire`) starts a new
session for each fire, wraps the request's text as untrusted, and allows 30 fires an hour
per task and 100 per account. It is in research preview and not yet used here.

## Where the text goes

- **The local index**: the maintainer's machine or container, owner-only files.
- **The hosted index**: the maintainer's own Cloudflare account, behind a token kept in
  the environment's secrets. The client sends it only over https to the configured URL,
  and follows no redirect.
- **Jev**: the text of messages and replies goes to OpenRouter, which passes it to
  TypeSafe; requests ask providers not to keep it. It is off unless switched on.
- **Claude**: only as Claude Code sessions on the maintainer's plan.

## How it changed from the first sketch

The first sketch (2026-11-03, afternoon) put Jev in the Worker and asked for one
repository per working session. What was built differs:

- **Jev runs in focused's hooks, not in the Worker.** There is one implementation of the
  extraction, and a Worker on Workers Free gets 10 ms of CPU per request. The Worker only
  stores and serves.
- **No one-repository rule.** Hooks written by the setup script run in sessions with
  several repositories, so focused is installed once per environment.
- **The Worker has its own repository**, focused-context-worker.
- **A new session doesn't restore on a guess.** Not every session continues the last
  one's objective, so the person chooses, by title.

## Build order

1. **Hosted index and restore.** Built and merged: the client (0.6) and the Worker. Done when a cloud session on the iPhone and one on the Mac reach the same
   sessions. That needs the Worker deployed and the environment set up (README, "The
   hosted index").
2. **Recall** (decided on 2026-11-03, before the ledger). Built and in review. Done when a new
   session's first message brings what earlier sessions said about it.
3. **Ledger and view page.** The three new Jev questions, then the page. Ledger items join
   recall.
4. **Triage and steward sessions.** Done when working sessions get no PR wake-ups.

## Decisions

Made by the maintainer on 2026-11-03:

- An index of turns, in Python, shipped as a Claude Code plugin; focused-context public.
- Jev reads each turn at indexing time; a part can be several kinds; answers at P ≥ 0.5;
  switched on for the maintainer.
- A summary loaded automatically after `/clear` and compaction.
- After an hour without a reply, the first typed message is stopped once, at 59 minutes.
- Cloud sessions get focused from the environment's setup script, and follow
  focused-context's main: a merge to main is a release.
- The index and ledger are hosted on the maintainer's Cloudflare account, in a new
  repository; the new Jev questions ship switched off and are judged by their output.
- `/focused-context` in a new session offers a choice of sessions instead of loading the
  latest, and sessions are named by their titles.
- Cloud sessions follow `main`, and `main` is protected on GitHub: only a merged pull
  request reaches them.
- Context is built from every relevant data point: recall across sessions by keywords and
  embeddings, with a session's first message and on demand, before the ledger.

Open:

1. How the Worker is deployed: by hand with `wrangler`, or by a GitHub Actions workflow
   with a Cloudflare token.
2. Retention: 30 days, locally and hosted; and whether a turn with no timestamp should
   ever expire (none has been seen).
3. Project grouping: every cloud session with more than one repository runs in
   `/home/user`, so all of them are one project, `-home-user`. Recall spans every project
   by relevance, which may settle this.
4. The idle stop's minimum, 100,000 tokens, and the summary's limit, 9,500 characters.
5. Recall's similarity floor (0.6, a guess for bge-m3), whether Jev sorts its candidates,
   and Workers Free (about 4,900 parts) or Paid (about 9,700) for the vectors.
