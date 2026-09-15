---
name: checkpoint
description: >
  Saves task state as a new journal/ entry only on an explicit manual user or agent
  command. Invoke only on an explicit manual checkpoint command.
---

# Checkpoint

Invoke checkpoint **only manually, on an explicit user or agent command**, through
the `checkpoint` skill (`.agents/skills/checkpoint/`). Completing a stage or task,
receiving a subagent result, interruption, switching harnesses, and approaching a
limit neither trigger a checkpoint automatically nor make one mandatory.

Main writes an explicitly requested checkpoint after evaluating the results; subagents
do not write canonical task state. If the write fails, the checkpoint is not considered
saved: main reports the error. Without an explicit command, a write is not a condition
for proceeding to the next stage.

Mechanically, a checkpoint **creates a new file** in `journal/` instead of appending to
a shared file. The CLI writes a complete temporary file, calls `flush`/`fsync`, then
publishes it atomically using `link` without overwriting an existing path. If the name
is taken, it retries with the next ordinal; no shared journal lock is needed.

## Procedure

```text
0. If there are parallel writes to the worktree:
   stop new delegations; wait for or interrupt ongoing work,
   and check its results.

1. Read task.md and the entire journal through agent-system task journal [slug].
   Omitting the slug uses the chat's binding. Report any reading error;
   do not skip a corrupted entry.
2. Check git status and the relevant diff.
3. Create a NEW journal/<ts>-<hash32>-<ordinal6>.md entry with everything essential
   that is not yet reflected in the journal.
4. Record the worktree state — clean or dirty, and why.
```

The CLI constructs the entry filename and frontmatter; do not assemble them manually:

```bash
agent-system task checkpoint --stage implementer <<'EOF'
<entry text>
EOF
```

The task comes from the current chat's binding; optionally, use `task checkpoint <slug>`.
In both cases, it must exist with status `active` or `paused`. Explicitly resume a
completed task first with `task set-status <slug> active`; an explicit slug neither
creates a task nor bypasses status validation.

The CLI gets the session ID from the environment (`AGENTS_SESSION_ID`,
`CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID`/`CODEX_SESSION_ID`). If none is available,
provide an explicit `--session-id` with the real chat ID. The exact value is validated
without trimming: 1–128 ASCII characters from `[A-Za-z0-9._-]`, except standalone `.`
and `..`, and the internal names `.locks` and `.agents-<32 lowercase hex>`.
`--stage` is 1–64 ASCII characters fully matching
`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`; spaces, `#`, and newlines are not allowed.

Do not skip step 4 even when the tree is clean: "clean" is information too.

## What to write

Write entry prose in English; preserve literal identifiers, commands, and quoted evidence.

There is one criterion:

> Anything essential for continuing that cannot be recovered from repository files.

- a new decision and **agreed intent**, including a decision about the next step;
- an important result;
- a rejected approach **with the reason for rejection**;
- a completed piece of implementation;
- a significant blocker;
- check results, including "not run";
- worktree state.

Intent is recorded alongside results. A decision such as "first check migration
compatibility, then implement" leaves no trace in the code; without a record,
it cannot be recovered from anywhere.

## What NOT to write

A complete retelling of the state every time. Record the delta — what changed at this
stage. People stop rereading a journal when nine tenths of it is repetition; it then
fails at its only purpose.

## Format

File: `journal/20260909T182014Z-8231adf1ce782ae5bd7a52c329e1ceae-000001.md`

```md
---
session: 019a3f7c-2c41-7b0e-9d55-3f1c8a0b7e21
at: 2026-09-09T18:20:14Z
stage: implementer
---

Tried replacing the legacy cache through the repository abstraction.
Rejected: the abstraction does not expose atomic compare-and-set, which the current
synchronization path requires.

Decision: access the store directly; add a wrapper later as a separate task.
Checks: not run.
Worktree: dirty — cache_adapter.py is in an intermediate state, intentionally left
as the starting point for the next step.
```

The filename has the form `<YYYYMMDDThhmmssZ>-<hash32>-<ordinal6>.md`. `hash32` is the
first 32 lowercase hexadecimal characters of the full session ID's SHA-256 hash;
the ordinal for a timestamp/hash pair starts at `000001` and increases on collision.
The first eight characters of a session ID are not treated as unique; exclusive
publication also protects against hash collisions.

The canonical reader returns legacy `journal.md` first, then old and new entries in
a validated order. Ambiguous old filenames are parsed using the full `session`
metadata; a corrupted entry raises an error. Old files are not converted. Sorting by
UTC timestamp, hash32, numeric ordinal, and filename is deterministic, but does not
guarantee causal ordering across machines. There is no shared task counter.

## Invariants

- The **orchestrator creates the checkpoint after interpreting the result**, not the
  subagent. A subagent does not know whether its conclusion has been accepted.
- An entry is **added**; existing entries are not overwritten. The journal is
  append-only at the directory level, so simultaneous checkpoints from two chats for
  one task produce two separate files rather than a race for one file.
- If writing an explicitly requested checkpoint fails, it is not considered saved;
  report the error without presenting the write as successful.
- Unverified subagent conclusions do not enter the journal as accepted facts.
  Rejected conclusions are retained with an explicit status and reason.
