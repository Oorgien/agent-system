# Operational contract

Both harnesses read this file. These rules apply to both Claude Code and Codex.

Write maintained documentation, prompts, task records, and memory facts in English; preserve literal identifiers, commands, and quoted evidence.

---

## 1. State: two levels

```text
.agents/memory/                project knowledge    outlives tasks
.agents/state/tasks/<slug>/    task state           retained after completion
```

**Project memory** describes how to work with this repository: where things live,
what takes a long time to build, testing pitfalls, and established conventions.
One fact per file.

Memory is **per project, not per branch**, and lives outside the worktree.
`~/.agents-memory/` is an ordinary directory with no `.git` of its own. Each project
subdirectory is a separate memory Git repository with its own history and a private
remote if transfer between machines is needed. The CLI does not create a remote
repository or set its visibility: the owner configures remote privacy.

```text
~/.agents-memory/                     ordinary directory
├── project-a-memory/                 separate Git repository for project A's memory
├── project-b-memory/                 separate Git repository for project B's memory
└── project-c-memory/                 separate Git repository for project C's memory

repo/.agents/memory               -> ~/.agents-memory/project-a-memory
../repo-2/.agents/memory           -> ~/.agents-memory/project-a-memory
repo/.worktrees/repo/.agents/memory -> ~/.agents-memory/project-a-memory
```

Existing saved keys and valid symlinks are preserved: the `-memory` suffix is added
only when a new key is selected automatically.

The project key is the name of a subdirectory in the store. It **must be the same
across all worktrees of one repository**; otherwise, the trees see different memory
even though the symlinks exist and no error is reported. The *current* directory name
cannot serve as the key: a worktree has a different name by design.

`agent-system init` selects and **remembers** both values:

| | Priority |
|---|---|
| Key | Explicit argument → saved `agents.memoryKey` → existing symlink → main Git worktree directory name + `-memory` |
| Store | `AGENTS_MEMORY_STORE` → saved `agents.memoryStore` → existing symlink → `~/.agents-memory` |

They are saved in the shared local Git config (`git config --local`; neither global
config nor `config.worktree` is used). All worktrees share this config, so the values
survive both reruns without the environment variable and repository directory renames.

Outside Git, there is nowhere to save them: the script warns and uses the current
directory name + `-memory`, and an explicit key must be passed each time. A fresh
clone does not inherit Git config: set the key once with `agent-system init --memory-key <project>`.

An explicit change of key or store reconfigures **only the current tree**; run
`agent-system init` again in the others. `agent-system doctor` detects trees that
still use the old settings and reports an error. Old memory is not moved automatically.

The symlink is gitignored, so run `agent-system init` **in every worktree and on every
machine**. `git clean -fdx` removes the symlink but not the store: symlinks are removed,
not followed.

**Task state** lives in `.agents/state/tasks/<slug>/`, is committed to the task branch,
and is **shared by both harnesses**. There are no separate Claude and Codex versions.

| Path | Contents |
|---|---|
| `task.md` | Task contract: goal, scope, acceptance criteria, non-goals, constraints |
| `journal/<ts>-<hash32>-<ordinal6>.md` | Entries: decisions, results, dead ends, worktree state |

`task.md` starts with frontmatter:

```yaml
id: task-a
status: active        # active | paused | done | abandoned
branch: feature-a     # informational field
created: 2026-09-10
```

`branch` is a hint, not an identity. Multiple tasks in one worktree are normal, so a
mismatch between `branch` and the current branch is a warning, not an error.

**The journal is a directory, not a file.** One entry per file. The new filename format:

```text
journal/<YYYYMMDDThhmmssZ>-<hash32>-<ordinal6>.md
journal/20260910T142233Z-8231adf1ce782ae5bd7a52c329e1ceae-000001.md
```

`hash32` is the first 32 lowercase hexadecimal characters of the full session ID's
SHA-256 hash; `ordinal6` is a six-digit attempt number, starting at `000001` for each
timestamp/hash pair. This is not a shared task counter. Different chats may share the
first eight characters of a session ID; a new file's uniqueness relies on neither
those characters nor the absence of hash collisions. The CLI writes a complete
temporary file in the same directory, calls `flush`/`fsync`, then publishes it using
`link` without overwriting an existing path. If the name is taken, the CLI tries the
next ordinal. Readers see only complete entries; two simultaneous checkpoints do
not overwrite each other. There is no shared journal lock.

Each entry contains `session`, `at`, and `stage` frontmatter followed by text. The full
session ID is retained in metadata. `--stage` is 1–64 ASCII characters fully matching
`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`; spaces, `#`, and newlines are not allowed.

The canonical reader is `agent-system task journal [slug]`; omitting the slug uses the
current chat's binding. It reads legacy `journal.md` first, followed by old and new
`journal/` files in a validated order. Ambiguous old names, `<ts>-<sid8>[-N].md`, are
parsed using the full `session` metadata, not just a filename regex. A corrupted entry
raises an error and must not be silently skipped. Old entries are **not converted**;
new entries always go into `journal/`.

Sorting by UTC timestamp, session hash32, numeric ordinal, and filename to break ties
produces a deterministic display order, but does not guarantee causal ordering across
machines: clocks can differ.

**Chat-to-task binding** is stored in `.agents/state/sessions/<session-id>`, gitignored,
as a single line of JSON:

```json
{"slug":"task-a","harness":"claude","bound_at":"2026-09-10T14:22:33Z"}
```

The filename is the session ID. The reverse index, "which chats are bound to task X,"
is a directory listing; there is no separate data structure for it.

**Session ID** is the chat identifier provided by the harness through the environment:

| Priority | Source |
|---|---|
| 1 | `AGENTS_SESSION_ID` — explicit override, works in any harness |
| 2 | `CLAUDE_CODE_SESSION_ID` |
| 3 | `CODEX_THREAD_ID`, then `CODEX_SESSION_ID` |

The exact full value is validated: 1–128 ASCII characters from `[A-Za-z0-9._-]`,
except `.` and `..`. The internal names `.locks`, `.gitkeep`, `.DS_Store`, and
`.agents-<32 lowercase hex>` are also reserved. Spaces and newlines are not trimmed.
Invalid values are **rejected, not sanitized**: converting an identifier to a "safe"
form can silently merge two different chats into one binding. If no source provides
an ID, the session works **without a binding** — a valid mode, not an error.

In an installed project, `.gitignore` excludes the `.agents/memory` symlink,
`.agents/state/sessions/`, `.agents/runs.jsonl`, and legacy `.agents/state/ACTIVE`/`LOCK`
until migration, rather than all of `.agents/`. Definitions, skills, and task state
are versioned in the main project.

There is no separate state snapshot. The journal is read in full; no projection is needed.

---

## 2. Session startup

```text
1. Read .agents/memory/ — repository knowledge
2. Determine the session ID; read .agents/state/sessions/<session-id>
3. Validate the binding (see below)
4. Read task.md and the ENTIRE journal
5. Check the actual repository state: git status, diff
```

**Step 1 is unconditional.** It applies even when there is no active task or no task
has been selected: memory is not tied to a task. Without this step, the cycle is
broken — task completion (§7) puts knowledge into memory, but the next session does
not read it.

The directory is small and flat: list the files and read those relevant to the work
ahead; if in doubt, read them all. If `.agents/memory` is missing or is a broken symlink,
this tree has no memory: run `agent-system init` (§1) instead of silently working without it.

**Task resolution.** The binding is the authoritative pointer; the branch is only a hint.

```text
Is the session ID known?
├── no → work without a binding; offer bind
└── yes
    ├── sessions/<id> exists → take its slug → validate
    └── missing
        ├── exactly one task with status: active → offer it; do NOT bind silently
        └── zero or multiple → show the list; ask
```

**Validation is mandatory.** Check that `tasks/<slug>/task.md` exists, its `id` matches
the slug, and `status` ∈ {`active`, `paused`}. A mismatch between `branch` and the current
branch is **a warning, not an error**: multiple tasks in one tree are now normal.

A broken binding is **not repaired automatically**: report it and fall back to
discovery. Loading the wrong task is worse than selecting none.

**Binding is idempotent.** `sessions/<id>` with the same slug is a no-op. A different
slug requires rebinding **only through an explicit command with `--force`**; do not
overwrite silently. `bind` and `unbind` hold a short OS `flock` for one session ID while
checking and changing the binding; different chats do not block each other. `--force`
permits rebinding but does not bypass locking or validation. Files in
`sessions/.locks/` are retained; the OS releases the lock when the process exits.
JSON is written atomically using a temporary file in the same directory and `os.replace`.

Legacy `.agents/state/ACTIVE` remains a candidate; a successful `bind` to the exact
task it names removes it only if no legacy `LOCK` exists. Binding to another task,
or the presence of `LOCK`, preserves `ACTIVE`. Legacy `LOCK` is not removed
automatically: before removing it manually, confirm that the old session is no longer
running. Then repeat the corresponding `bind`. Both legacy files remain gitignored
until migration is complete.

**Read the entire journal** through `agent-system task journal [slug]` (§1),
not by sorting filenames in the shell. A corrupted entry stops reading; do not
continue with a silently truncated journal.

---

## 3. Checkpoint

Invoke checkpoint **only manually, on an explicit user or agent command**, through
the `checkpoint` skill (`.agents/skills/checkpoint/`). Completing a stage or task,
receiving a subagent result, interruption, switching harnesses, and approaching a
limit neither trigger a checkpoint automatically nor make one mandatory.

Main writes an explicitly requested checkpoint after evaluating the results; subagents
do not write canonical task state. If the write fails, the checkpoint is not considered
saved: main reports the error. Without an explicit command, a write is not a condition
for proceeding to the next stage.

Mechanically, a checkpoint **creates a new file**,
`journal/<ts>-<hash32>-<ordinal6>.md` (`agent-system task checkpoint`), published atomically
without overwriting (§1). The task comes from the binding or is specified explicitly:
`agent-system task checkpoint <slug>`. In both cases, the task must exist with status
`active` or `paused`; explicitly resume a completed task first. An explicit slug neither
creates a task nor bypasses status validation.

### What to write

There is one criterion:

> Anything essential for continuing that **cannot be recovered from repository files**.

This includes:

- a new decision and **agreed intent**, including a decision about the next step;
- an important result, a rejected approach **with the reason**;
- a completed piece of implementation, a significant blocker;
- check results, including "not run";
- **worktree state: clean or dirty, and why.**

Intent is recorded alongside results. A decision such as "first check migration
compatibility with old data, then implement" leaves no trace in the code; without
a record, it cannot be recovered from anywhere.

Worktree state is called out deliberately: a new session will see changes in
`git status` but cannot tell whether they were abandoned or intentionally left unfinished.

**What not to write:** a complete retelling of the entire state every time. Record the
delta. People stop rereading a journal when nine tenths of it is repetition.

### Entry format

File `journal/20260909T182014Z-8231adf1ce782ae5bd7a52c329e1ceae-000001.md`:

```md
---
session: 019a3f7c-2c41-7b0e-9d55-3f1c8a0b7e21
at: 2026-09-09T18:20:14Z
stage: implementer
---

Tried replacing the legacy cache through the repository abstraction.
Rejected: the abstraction does not expose atomic compare-and-set,
which the current synchronization path requires.
```

The timestamp determines display order, hash32 distinguishes session IDs, and exclusive
publication with ordinal retries protects against collisions. This is neither a causal
clock nor a shared task counter.

---

## 4. Switching harnesses

Switching harnesses does not itself trigger or require a checkpoint. When starting or
resuming work, the new harness follows §2: it reads saved state and checks it against
Git. Unsaved context may be lost.

---

## 5. Delegation

Stages are determined by semantic signals, not intuition.

**TRIVIAL** — no public contract change, no schema/migration, no auth or security logic,
no concurrency or distributed state, no cross-component invariant; the pattern is clear.
→ `main or implementer → verification`. Reviewer is optional.

**NORMAL** — the default when a task is neither obviously trivial nor contains complex signals.
→ `explorer if needed → implementer → reviewer`.

**COMPLEX** — public API or protocol change, schema migration, risk of data loss, auth
and permissions, concurrency, distributed state, cross-service contract, architectural
boundary change, high ambiguity, or a new implementation strategy.
→ `explorer → planning → implementer → reviewer`.

The number of changed files is a **weak** signal. Renaming 20 generated files can be
trivial; changing one line of authorization logic may not be.

**When unsure, use NORMAL.** An explicit user override is allowed in either direction.

---

## 6. Fix loop

```text
implement → review → fix → review
```

Default to at most 2–3 corrective rounds. After that, main **diagnoses why the work is
not converging**, rather than escalating automatically because of a numerical limit:

```text
unclear requirement      → ask the user
technical unknown        → explorer
implementation thrashing → stop the loop and summarize
fundamental problem      → return to planning
```

There is no separate permanent orchestrator: the main session is the orchestrator.

---

## 7. Task completion

Completion is **a step for publishing knowledge, not deleting a directory**. Without
it, important findings remain only in the journal, which later tasks usually do not read.

Review the journal and separate its contents:

| What | Where |
|---|---|
| Code and architecture decisions people need to know | Project documentation, in Git |
| How to work with this repository | `.agents/memory/` |
| Progress of the specific task | Remains in the task journal; not copied to project memory |

The third row matters as much as the first two: publishing everything turns memory
into a second journal and makes it less useful.

Then set `status` to `done`/`abandoned` and remove bindings for chats bound to that task.
Live bindings to a completed task are a state error reported by `check_state.py`.

After completion, `task.md` and `journal/` remain in `.agents/state/tasks/<slug>/`,
including after merge/squash. Completed journals are not read during normal session
startup; consult them when needed. To resume a task, explicitly select it, check the
current code state, update `branch` and `status`, then bind the chat.

`status: paused` is for a task you intend to return to: it is valid for binding,
but discovery does not suggest it.

---

## 8. Invariants

- **The chat-to-task binding belongs to the chat, not the worktree.** Multiple tasks
  in one tree and multiple chats on one task are normal; they do not displace each other.
- **One journal entry is one file, written by exactly one session.** Existing entries
  are not edited: the journal is append-only at the directory level.
- **The task's main coordinates `task.md` edits** — status changes and scope edits.
  Atomic file writes do not resolve semantic conflicts between chats.
- **Only main writes canonical task state.** Parallel implementers are assigned
  non-overlapping areas; subagents must also stop writing before an explicitly invoked checkpoint.
- **`runs.jsonl` is never canonical state.**

---

## 9. Tools

```bash
agent-system init      # memory store + symlinks (§1), in every tree
agent-system update    # update definitions and skills from the Agent System clone
agent-system doctor    # check installation, task state, and bindings (§2)
```

Working with tasks and bindings:

```bash
agent-system task list                # tasks, their status, and bound chats
agent-system task status              # what the current chat sees: binding or discovery
agent-system task new <slug>          # create a new task from the template
agent-system task bind <slug>         # bind the current chat; --force to rebind
agent-system task unbind              # remove the current chat's binding
agent-system task journal [slug]      # read the entire journal in canonical order
agent-system task checkpoint          # new journal entry (§3), text from stdin
agent-system task set-status <slug> <status>
```

`task list` and `task status` do not write anything. Only `task bind` creates a binding.

`task new` reserves the task directory with an exclusive `mkdir`: a concurrent call
with the same slug is rejected, and an existing task is not overwritten. A failure can
leave a directory without `task.md`; validation reports this as incomplete creation.
The CLI does not delete such a directory automatically: inspect its contents manually.

`.claude/agents/` and `.codex/agents/` are **generated**. Editing them is pointless:
the next generator run overwrites them, and CI fails the drift check. Change definitions
in the Agent System clone and run `agent-system update`.
Local edits to installed files block updates until the conflict is explicitly resolved.

If the generator refuses to build an agent because an access boundary cannot be
expressed, this is not a bug. Claude permissions are defined by a tool list, which
cannot express `shell` without write access. Choosing between "remove shell" and
"acknowledge that the agent can write" is deliberate; do not bypass it.

---

## 10. Parallel work

Use Git worktrees for different features, with one harness per tree:

```text
repo/              → Claude, feature A
../repo-billing/   → Codex,  feature B
```

`sessions/` and the memory symlink are gitignored, so each tree creates them separately.
The trees have **different** bindings, but all memory symlinks point to the same store
directory: memory is shared; task state is not. The key and store selection rules,
and the requirement to rerun `agent-system init` in each tree after changing them, are in §1
and are not repeated here. Task state is committed and lives on its own branch.
Agent and skill definitions are versioned by branch: an edit on branch A is not visible
on branch B until merged.
