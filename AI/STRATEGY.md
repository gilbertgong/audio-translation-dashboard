# Strategy: a repo that any AI assistant can pick up

## Scope boundary

This AI/ directory documents audio-translation-dashboard. If the project is embedded as a subtree
or subdirectory in another repository, these instructions remain scoped to this project and its
contents. They are not host-repository policy. Do not extend its Git workflow, release process,
architecture or working conventions to sibling projects or the containing repo without explicit
host guidance. Keep this boundary clear when adding or reorganizing AI docs.

This describes how this repo is organized so that an AI assistant can continue the work from the repo alone: on
another computer, in a fresh session, or with a different assistant. It's written so an assistant can set up the
same structure in another repo by following it.

## The problem it solves

An assistant's understanding of a project usually lives in places that don't travel:
- **the conversation**, which ends;
- **the assistant's own memory**, which is tied to one tool, one account or one computer;
- **the user's head**, which means re-explaining preferences and decisions every time.

The fix is to keep that understanding in the repo, in plain Markdown, in a place every assistant is pointed to, and
to keep it current as part of the work itself.

## Principles

1. **The repo is the memory.** Everything an assistant needs to continue is a file in the repo. Nothing important
   lives only in a chat or in an assistant's private memory.
2. **One home for AI context, one entry point.** It all lives in `AI/`, and `AI/MEMORY.md` is the entry point: it
   says what else to read and when. Tool-specific entry files only point to it, so switching assistants means adding
   a pointer, not rewriting anything.
3. **People and assistants get separate docs.** `README.md` is for people. `AI/` is for assistants, and can be
   detailed, explicit and full of reasons.
4. **Record the why.** Decisions are written down with their reasons, so a later session can tell a deliberate
   choice from an accident, and knows what to reconsider if the reason no longer holds.
5. **Keep the present apart from the past.** `AI/PLAN.md` holds the current state and is rewritten so it's always
   true; `AI/PROJECT_LOG.md` holds shared history and is only appended to, except when relocating
   private material into `AI/PRIVATE.md`. That file keeps its own current context and dated history.
   *Why:* when one file mixes both, every change of direction means finding and correcting old statements, and the
   stale ones that slip through mislead the next session.
6. **Docs are updated with the code, unasked.** Every change includes updating the docs it affects. A stale plan is
   worse than none.
7. **Plain Markdown, no tooling.** Any assistant can read and write it; nothing needs installing.
8. **The repo stands alone.** The shared AI docs describe only the repo itself. There are no local paths, machine
   details, user-specific setups, or discussion of files, folders and projects outside the repo. What the repo needs
   from outside (data, tools, services) is described generically, as requirements ("a whisper.cpp `whisper-cli` and
   model, configured in the voice config"), never as where they happen to live on one computer.
   *Why:* the docs travel with the repo: to other computers, other people, and eventually the public. Local details
   are wrong everywhere else, can expose private information, and tie the project to one setup.
   *Exception, by declaration:* a project may deliberately disclose its reference hardware (in `AI/PLAN.md`), so that
   timings, throughput and memory figures can cite it as context, as a paper's methods section does. Local *setup*
   (paths, services, ports, what else runs on the machine) still goes in `AI/LOCAL.md`.
9. **Local knowledge goes in `AI/LOCAL.md`, which is not in version control.** An assistant on a particular computer
   still needs to know where things are there: data folders, related projects next to the repo, local services and
   their ports, machine details, and local-only rules ("leave that folder alone"). All of that goes in
   `AI/LOCAL.md`, listed in `.gitignore`. Each computer has its own, or none.
   *Why:* principle 8 keeps the shared docs clean, but the local knowledge is still worth keeping; this gives it a
   home that never leaves the machine.

## Layout

```
README.md            for people: what it is, how to run it, options, output
AGENTS.md            entry file: "read AI/MEMORY.md first"
CLAUDE.md            entry file for Claude Code: the same pointer (plus an import of AI/MEMORY.md)
(other entry files)  one per assistant in use, same content
.gitignore           includes AI/LOCAL.md
AI/
  MEMORY.md          the entry point: what to read and when; how to work here (preferences, each with its reason)
  PLAN.md            the present, high level: goal, direction, approach, results so far, next steps, open questions
  TODO.md            user-requested work, with completed and remaining items
  PROJECT_LOG.md     the past: dated history of what was tried, found and decided, and why; appended to
  STRATEGY.md        this file: how the structure works and how to replicate it
  PRIVATE.md         private branches only: private workflow, feature plans and history; absent from public
  LOCAL.md           this computer only, not in version control: local paths, related projects, services, rules
```

### `AI/MEMORY.md`: the entry point, and how to work

It opens with a **"Start here"** instruction:
1. read `AI/PLAN.md`;
2. read `AI/LOCAL.md` if it exists;
3. read `AI/PROJECT_LOG.md` when history matters (before trying something that may have been tried, or to understand
   how a decision came about);
4. read `AI/STRATEGY.md` when changing how the docs are organized;
5. read `AI/PRIVATE.md` when present for private workflow and feature context.

Then a short map of the repo's docs, then the user's lasting preferences about how the assistant should work:
process, testing, communication, what to ask about and what to just do. Not facts about the project.

- One short section per rule: the rule, then a `*Why:*` line with the reason or the incident behind it.
- Written for any assistant ("an assistant should…"), with no tool-specific names or paths.
- Added to whenever the user states a lasting preference, including corrections ("don't do X") and confirmations
  ("yes, keep doing that").
- Includes the rules that make this structure work (below), so they travel with every copy.

### `AI/PLAN.md`: the present

Where the project stands now, at a high level, rewritten when the direction changes so it's always true. It's about
the project, not one computer or session: what's running or queued right now doesn't belong here. Suggested sections:

- **Goal:** what it's for, and what good looks like.
- **Current direction.**
- **Approach:** the decisions in force, each with its reason. A replaced decision leaves the plan; the log records
  the change.
- **Results so far:** the headline findings.
- **Orientation:** where code and docs are, briefly.
- **Open questions:** decisions waiting on the user.
- **Known issues:** bugs and weak spots not yet fixed, with workarounds.
- **Next steps.**

### `AI/PROJECT_LOG.md`: the past

Dated entries, appended to: what changed and why, findings, approaches tried and dropped, decisions made and later
replaced. Read when history matters, e.g. before trying something that may have been tried. Facts that are already
plain from the code or version control don't need repeating; record what isn't.

### `AI/PRIVATE.md`: private development only

When present, this tracked file holds substantive private-branch workflow, plans, decisions and
history, including feature branches feeding private. Shared docs may contain brief references, but
keep the detailed private material here. Exclude this file from the public branch and public releases
when selecting shared changes; it is intentionally absent from public checkouts. This is separate
from machine-local `LOCAL.md`, and neither file should contain credentials. Relocating private history
here is an exception to PROJECT_LOG.md's append-only convention: preserve the dated entries here.

### `AI/LOCAL.md`: this computer

Everything local that principle 8 keeps out of the shared docs:
- where data and tools live on this machine;
- related projects and folders next to the repo;
- local services (ports, what they're for, who keeps them running);
- machine details;
- local-only rules (e.g. "leave the old experiments folder untouched").

It's optional (a fresh clone has none) and listed in `.gitignore`. Shared docs never depend on it; they describe
requirements generically, and `LOCAL.md` says how they're met here.

### `README.md`: for people

What it is, how to install and run it, options, output, and any information derived for a person's benefit.
Reasons, history and working rules go in `AI/` instead.

### Entry files: pointers only

Each assistant looks for its own instructions file. Each should contain only a pointer to `AI/MEMORY.md`, never
content of its own, so there's one place to update. Examples (check each tool's docs for the current name and format):

| File | Read by |
|---|---|
| `AGENTS.md` | a shared convention used by several assistants |
| `CLAUDE.md` | Claude Code; an `@AI/MEMORY.md` line imports the file automatically |
| others (e.g. `GEMINI.md`, `.github/copilot-instructions.md`) | add one when that assistant is used, with the same pointer |

The entry file says in plain words to read `AI/MEMORY.md` first; `AI/MEMORY.md` then says what else to read, in plain
language that works for every assistant.

## Rules that keep it working

These go in every `AI/MEMORY.md`, after the "Start here" instruction:

1. **Start here:** read `AI/PLAN.md`, `AI/LOCAL.md` if it exists, `AI/PROJECT_LOG.md` when history matters, and
   `AI/STRATEGY.md` when changing the docs' structure.
2. **Remember in the repo.** Save preferences in `AI/MEMORY.md`, the current state in `AI/PLAN.md`, history in
   `AI/PROJECT_LOG.md`, and anything about this computer in `AI/LOCAL.md`, not in the assistant's own memory.
3. **Keep the docs current without asking.** Each change includes updating the README, the plan, the log and the
   memory file as they're affected.
4. **README for people, `AI/` for assistants.**
5. **Keep the shared AI docs self-contained:** nothing local or outside the repo (principle 8); that goes in
   `AI/LOCAL.md` (principle 9).

## Setting it up in a repo

For an assistant doing this in a new or existing repo:

1. **Create `AI/`** with `MEMORY.md`, `PLAN.md`, `PROJECT_LOG.md` and a copy of this `STRATEGY.md`. If anything local
   is worth recording, add `AI/LOCAL.md`, and add `AI/LOCAL.md` to `.gitignore` either way.
2. **Write `AI/MEMORY.md`:** the "Start here" instruction and the five rules above (worded for that repo), then the
   user's known preferences. If the user has preferences saved in an assistant's own memory, carry over the ones that
   still apply, generalized so any assistant can follow them.
3. **Write `AI/PLAN.md` and `AI/PROJECT_LOG.md`:**
   - new repo: the goal and the first decisions in the plan; a first dated entry in the log;
   - existing repo: read the code, docs and history, then write the plan (goal, how things work, a code map, the
     current status, the open questions) and a log entry summarizing the history so far. Ask the user about anything
     that can't be learned from the repo, especially the reasons behind decisions.
4. **Sort the README.** Keep what people need; move reasons, history and working notes into `AI/`.
5. **Add entry files:** `AGENTS.md`, plus one per assistant the user works with, each only pointing to
   `AI/MEMORY.md`.
6. **Tell the user** what was created and what's still an open question.

## Day to day

- **Starting a session:** read `AI/MEMORY.md`, which says what to read next.
- **During work:** when a decision is made, a finding turns up, or a plan changes, update the plan and add a log
  entry as part of that step. When the user states a preference, add it to `AI/MEMORY.md`. Local facts (paths,
  services, machine-only rules) go in `AI/LOCAL.md`.
- **Finishing a piece of work:** make sure the plan's status, open questions and known issues are true, and the
  README matches what a person would see.
