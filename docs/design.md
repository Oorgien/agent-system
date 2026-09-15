# Agent System — agreed design v9

Status: ready for implementation.
Design frozen: 2026-09-09
v1 storage clarification: 2026-09-11 — atomic journal publication, canonical reading
of old and new entries, and serialization of bindings for the same session.

Changes from v8:

- **Added a project memory layer** `.agents/memory/` — long-term project knowledge,
  separate from task state and outside the project's Git repository (§2.1);
- **Removed `handoff.md`.** `task.md` and the task journal remain; working tree state
  moved into the journal contract, the precedence rule was removed as unnecessary,
  and the skill was renamed to `$checkpoint`;
- added a rule for publishing knowledge when a task is completed (§7);
- added a commit hygiene rule (§11);
- recorded a journal rotation criterion without introducing a mechanism (§2.2).

Retained from earlier versions: reading the entire journal, checkpoint as an explicitly invoked
manual operation with successful-write verification, honest durability limits, the squash
correction, the requirement for adapters to enforce access boundaries, and no full status recap
in every entry.

---

## 1. Goal and scope

**Sequential interchangeability of harnesses**: one set of agents, skills, and working
state is used from Claude Code or Codex, with one harness per session.

Scenarios:

- work from whichever harness is convenient at the moment;
- reach a usage limit and continue in another harness without manually retelling the context;
- changes made in one harness are picked up by the other together with task state;
- two harnesses work **on different features simultaneously** in separate worktrees (§11);
- the `explorer` / `implementer` / `reviewer` roles use the native subagent mechanisms of both;
- the same portable skills are available to both.

### Out of scope

Cross-harness orchestration (`Claude orchestrator → Codex subagent` and vice versa).
Each harness uses its own runtime, subagents, permissions, sandboxing, and lifecycle.

Cross-model review remains a **manual** action: open the same branch in another
harness → request an independent review.

What this removes:

| Removed | Reason |
|---|---|
| exec-recipe as a generation target | the other harness is not invoked as a subprocess |
| `run_agent.sh` wrapper | native subagents are used |
| custom sandbox orchestration | permissions are the harness's responsibility |
| cross-harness RPC / state protocol | integration is limited to Git + `.agents/` |

---

## 2. State model

Two layers with different scopes and lifetimes:

```text
.agents/memory/                  project knowledge     outlives tasks
.agents/state/tasks/<slug>/      task state            retained after completion
```

Do not mix them: they have different visibility, storage, and reading rules.
What moves between these layers, and when, is covered in §7.

---

### 2.1 Project memory

`.agents/memory/` is long-term knowledge about **how to work with this project**:
where things are, what takes a long time to build, testing pitfalls, and established conventions.
It is useful to the agent, of no interest to people, and not tied to a specific task.

Memory is **project-scoped, not branch-scoped**. Storing it in Git next to the code makes
the branch its visibility boundary: something learned while working on billing is unavailable
on the auth branch until a merge. This is a mismatch between semantics and storage, not an
inconvenience.

Memory is **project-scoped, not branch-scoped**, and is stored outside the working tree.
`~/.agents-memory/` is an ordinary directory with no `.git` of its own. Each project directory
inside it is a separate memory Git repository with its own history and a private remote
if transfer between machines is needed. The CLI does not create a remote repository or set
its visibility: the owner configures the remote's privacy.

```text
~/.agents-memory/                     ordinary directory
├── project-a-memory/                 separate Git repository for project A's memory
├── project-b-memory/                 separate Git repository for project B's memory
└── project-c-memory/                 separate Git repository for project C's memory

repo/.agents/memory               -> ~/.agents-memory/project-a-memory
../repo-2/.agents/memory           -> ~/.agents-memory/project-a-memory
repo/.worktrees/repo/.agents/memory -> ~/.agents-memory/project-a-memory
```

Properties of this layout:

| Need | How it is addressed |
|---|---|
| shared memory across worktrees | symlinks to one store — all worktrees see the same content |
| transfer between machines | `git clone` of the project's memory repository, a separate private remote per project |
| backups and change history | a separate Git repository in the project's memory directory |
| project key | the directory name in the store; the symlink establishes the association |

**One fact — one file.** Otherwise, merges between machines conflict on every append;
with separate files, conflicts practically disappear.

`.agents/memory` is in the project's `.gitignore`. The symlink is created **in every worktree
and on every machine**: gitignored files are not shared between worktrees. This takes two
lines in the setup script — clone the relevant project's memory repository (once per machine)
and create a symlink (once per worktree).

Do not introduce project key resolution schemes based on remote URLs or path hashes:
resolve a name collision manually if one ever occurs.

**The key must be identical across all worktrees of a repository**. Otherwise, the scheme
silently fails at its purpose: the symlinks exist, there is no error, but the worktrees have
different memory. The *current* directory name cannot be the key: worktree directory names
are different by design (`repo/` and `repo-billing/`). The setup script uses the **main**
working tree's directory name + `-memory` — `git rev-parse --git-common-dir` points to the
main repository's `.git` from every worktree. Outside Git, there is no stable source:
the script warns and accepts the key as an argument. The regression is covered by
`test_worktrees_share_one_memory_directory`.

An incidental property: `git clean -fdx` in the project removes the symlink but not the store —
symlinks are removed, not dereferenced. One line restores the link; the memory remains intact.

---

### 2.2 Task state

The contract and journal are both committed to the task branch:

| Path | Question answered | Writes | Owner |
|---|---|---|---|
| `task.md` | what we are doing | infrequently, when requirements change | human / main |
| `journal/<ts>-<hash32>-<ordinal6>.md` | what we have learned and where we are now | a new entry only on an explicit manual command | main |

There is no separate state snapshot (`handoff.md`): the journal is read in full, so a projection
is unnecessary. This also removes the precedence rule between sources, the question of snapshot
freshness, and an entire step from the switching procedure.

The files are **shared by both harnesses**. There are no separate Claude and Codex versions:
after a switch, the same set of files is read and extended.

#### `task.md`

The task's authoritative contract: goal, scope, acceptance criteria, non-goals, constraints,
known relevant context. It is not immutable — it changes when the requirements actually change.
An explicitly invoked checkpoint records significant contract changes in the journal.

Frontmatter:

```yaml
---
id: auth-refactor
status: active | paused | done | abandoned
branch: feature/auth-refactor   # advisory hint, may become stale
created: 2026-09-09
---
```

`branch` is a hint, not an identity. Renaming the branch makes it stale, and the resolution
procedure must handle that (see §5). `paused` means a task we intend to return to:
it is valid for binding, but discovery does not suggest it.

#### `journal/`

Append-oriented history: decisions, experiments, dead ends, important research findings,
scope changes, and reasons for architectural decisions.

**The journal is a directory, not a file**: one entry — one file. The new filename format is
`<YYYYMMDDThhmmssZ>-<hash32>-<ordinal6>.md`. `hash32` is the first 32 lowercase hexadecimal
characters of the full session ID's SHA-256 hash; the ordinal for a timestamp/hash pair starts
at `000001`. The full session ID remains in the metadata.

```md
journal/20260909T182014Z-8231adf1ce782ae5bd7a52c329e1ceae-000001.md

---
session: 019a3f7c-2c41-7b0e-9d55-3f1c8a0b7e21
at: 2026-09-09T18:20:14Z
stage: implementer
---

Tried replacing the legacy cache through the repository abstraction.
Rejected: the abstraction does not expose atomic compare-and-set,
which the current synchronization path requires.
```

The CLI fully writes a temporary file in the same directory, performs `flush`/`fsync`, then
publishes it using `link` without overwriting an existing path. If the name is taken, it tries
the next six-digit ordinal. This is neither a shared task counter nor a preliminary check for
an available name: the exclusive operation decides whether publication succeeds. Readers see
a complete entry, and concurrent checkpoints do not overwrite each other. The first eight
characters of a session ID are not assumed to be unique; retrying on collision also protects
against matching hashes. There is no shared journal lock.

The canonical reader is `agent-system task journal [slug]`; omitting the slug uses the current
chat's binding. Legacy `journal.md` is read first, followed by old and new `journal/` entries
in a validated order. An ambiguous old filename `<ts>-<sid8>[-N].md` is parsed using the full
`session` in its metadata, including the numeric suffix if present. Corrupted entries raise
an error; none are silently skipped. Old journals are not converted; new entries always go
into `journal/`.

Display order is deterministic: UTC timestamp, session hash32, numeric ordinal, then filename
if the preceding fields are equal. Causal order across machines is not guaranteed: clocks may
differ. `--stage` must fully match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`; spaces, `#`, and newlines
are rejected.

With `handoff.md` removed, the journal also holds what used to be in the snapshot:

- unfinished work and non-obvious next actions;
- **working tree state** — clean or dirty, and **why**.

The latter is highlighted deliberately. A new session sees changes in `git status` but cannot
tell whether they were abandoned or intentionally left unfinished. This is precisely the kind
of fact that cannot be reconstructed from files.

**Read the entire journal on every resumption**, then check the repository's actual state.
v1 has no partial-reading mechanism — no markers, cursors, or search for a continuation point.

The journal is **per-task**, not shared across tasks. Completed tasks are retained (§7), but
their journals are not loaded during normal startup. Only the selected task's journal is read
in full; completing other tasks does not, by itself, limit the length of an individual journal.

##### Rotation: record the criterion without introducing a mechanism

If a journal grows large, simply creating `journal-2` is **not enough**: the current journal
must start with a summary of the current state derived from the entries being archived.
Otherwise, rotation silently discards early decisions that impose constraints — their value
grows over time, unlike routine progress entries.

Do not build compaction machinery in advance. A large journal is a **signal**: either the task
should have been split, or the journal is recording narrative instead of decisions. Keeping
only the tail would suppress that signal without addressing its cause.

---

## 3. Durability

The model of “work throughout the session → serialize state at the end” is unreliable: when
a limit is reached or a crash occurs, there is no turn left for serialization. The very scenario
this system was built for is the first to fail.

Only explicitly recorded state is preserved; no frequency is prescribed for manual checkpoints.

### 3.1 Checkpoint — a manual operation

A checkpoint is invoked **only manually, on an explicit command from the user or agent**
through the `checkpoint` skill (`.agents/skills/checkpoint/`). Finishing a stage or task,
a subagent result, interruption, switching harnesses, and approaching a limit neither
trigger a checkpoint automatically nor require one to be invoked.

Main writes an explicitly invoked checkpoint after evaluating the results; subagents do not
write canonical task state. If writing fails, the checkpoint is not considered saved: main
reports the error. Without an explicit command, writing a checkpoint is not a prerequisite
for moving to the next stage.

#### What goes into a checkpoint

There is one criterion:

> Everything important for continuation that **cannot be reconstructed from repository files**.

This includes a new decision, an adopted intention (including the next step), an important
result, a rejected approach with its reason, a completed piece of implementation, a significant
blocker, and verification results — including “not run”.

Intentions belong in the journal alongside results. The decision “first check migration
compatibility with old data, then implement” leaves no trace in code: if it is not recorded,
neither the journal nor `git status` can reconstruct it. Reading preserves only what someone
has written — and that is the only protection against losing intentions.

An entry must not contain **a complete recap of all state every time**. The objection is purely
practical: friction on every write and a journal that people stop rereading because nine tenths
of it are repetition. Record the delta — what changed at this stage.

### 3.2 What is deliberately absent

Within a session, subagents communicate with the orchestrator natively in both harnesses —
no files are needed. Everything described here addresses **cross-session** continuity only.

v1 has **no** separate recovery log (`.recovery.jsonl`) or agent-completion hooks.
The reason: v1 relies on manual checkpoints and does not add a second writing mechanism.
A local transcript may help recovery, but its completeness, preservation, and availability
to another harness are not guaranteed. Omitting a recovery log is a deliberate simplification,
not a guarantee of zero loss.

Degradation after an unexpected termination:

```text
recovery point — the last successfully written checkpoint, if one exists
    ↓
work without a checkpoint is checked against repository state
    ↓
if needed — an available transcript from the original harness
```

The transcript is forensic evidence of last resort, not a working channel: to another harness,
it is a wall of noise. Only the journal provides a curated record that both can read, and
nothing replaces it — that is its entire value.

Context loss is not limited to one stage: it depends on the last explicit write. If no
checkpoint has been invoked, the journal may contain no work results. A disk write survives
a session interruption in the same worktree, but not loss of the machine. Moving to another
machine requires committing and transferring the code together with task state through Git;
uncommitted and untracked files are not transferred automatically.

### 3.3 `$checkpoint` — an explicit manual invocation

Switching harnesses does not, by itself, trigger or require a checkpoint. When starting or
resuming work, the new harness follows §2: it reads saved state and checks it against Git.
Unsaved context may be lost.

Procedure for the `.agents/skills/checkpoint/` skill:

```text
0. If writes are concurrent, stop new delegations; wait for or interrupt current work and inspect the result.
1. Read task.md and the entire journal using agent-system task journal [slug].
   If reading fails, report corruption; do not skip the entry.
2. Check git status and the relevant diff.
3. Create a NEW journal/<ts>-<hash32>-<ordinal6>.md entry containing everything important
   that is not yet recorded: decisions, unfinished work, non-obvious next actions.
4. Record working tree state — clean or dirty, and why.
```

The checkpoint's task is taken from the binding or specified by an explicit slug; in either
case, it must exist with status `active` or `paused`. An explicit slug neither creates a task
nor bypasses status validation. A completed task must first be explicitly resumed.

The procedure runs only on an explicit command, regardless of harness switching.
There are no automatic checkpoints or lifecycle write hooks. A remaining-context threshold
of <=10% is not implemented; it could only be a future optional best-effort mechanism with
separate opt-in and a contract change.

---

## 4. Repository layout

In the CLI source repository, the shipped `checkpoint` and `migrate-memory` skills live in
`skills/`; its own `.agents/` supports development and is locally gitignored. The installer
does not read `.agents/skills/`; unrelated skills in the target project are preserved.
The layout below shows installed skill paths in the target project; the copies are independent.
Versioning rules for an installed `.agents/` do not change the CLI clone's local policy.

```text
repo/
├── AGENTS.md                        canonical project contract
├── CLAUDE.md                        import AGENTS.md
│
├── .agents/
│   ├── agents/
│   │   ├── explorer.md
│   │   ├── implementer.md
│   │   └── reviewer.md
│   │
│   ├── memory/                      -> ~/.agents-memory/<project>  (symlink, gitignored)
│   │
│   ├── skills/
│   │   ├── checkpoint/SKILL.md
│   │   └── migrate-memory/SKILL.md
│   │
│   ├── state/
│   │   ├── sessions/                # gitignored, chat-to-task binding
│   │   │   ├── .locks/              # per-session OS flock files
│   │   │   └── 019a3f7c-…           #   {"slug":…,"harness":…,"bound_at":…}
│   │   ├── tasks/
│   │   │   └── auth-refactor/
│   │   │       ├── task.md
│   │   │       └── journal/
│   │   │           ├── <timestamp>-<hash32>-000001.md
│   │   │           └── <timestamp>-<hash32>-000002.md
│   │   └── archive/
│   │
│   └── runs.jsonl                   # gitignored, local telemetry
│
├── tools/
│   ├── gen_agents.py
│   ├── adapters/{claude.py,codex.py}
│   └── tests/
│
├── .claude/
│   ├── agents/                      # GENERATED
│   ├── skills -> ../.agents/skills
│   └── settings.json                # native settings, if needed
│
└── .codex/
    ├── agents/                      # GENERATED (*.toml)
    └── config.toml
```

In a target project, `.agents/` is the canonical root for installed agent definitions, skills,
and portable state. `tools/` transforms canonical definitions into vendor formats.
Only `.claude/agents/` and `.codex/agents/` are generated, not the entire directory trees.

**The skills symlink is retained**: objections concerned Windows, CI checkouts, and Docker
`COPY`, none of which are currently in use. Replacing it with a deterministic copier is a
one-line generator change; the canonical source stays the same.

---

## 5. Task resolution

The **chat** selects the task, not the worktree or branch. The binding is
`sessions/<session-id>` — **workspace state, not task state**, so it is gitignored.

Using a branch as a task identifier assumes too much: there can be multiple tasks on a single
feature branch, detached HEAD, research tasks without a branch, hotfixes on an existing branch,
worktrees, and renames. The worktree is not suitable either: multiple chats on different tasks
may legitimately be open in one worktree. Therefore:

```text
sessions/<session-id>  = authoritative pointer, owned by the chat
branch                 = advisory hint in task.md
```

**Session ID** is the chat identifier from the harness environment, in priority order:
`AGENTS_SESSION_ID`, `CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID`/`CODEX_SESSION_ID`.
The exact value is validated in full: 1–128 ASCII characters from `[A-Za-z0-9._-]`, excluding
the standalone values `.` and `..`, and reserved `.locks`, `.gitkeep`, `.DS_Store`, and
`.agents-<32 lowercase hex>`. Spaces and newlines are not trimmed. The binding filename and
journal entry hash depend on the ID, so an invalid value is **rejected, not sanitized**.
A missing ID is a valid mode without a binding.

Session startup procedure:

```text
is the session ID known?
├── no → work without a binding; offer bind
└── yes
    ├── sessions/<id> exists → take its slug → validate
    └── does not exist
        ├── exactly one task with status: active → suggest it, do NOT bind silently
        └── zero or multiple → show the list and ask
```

### Staleness check

A binding does not update automatically on `git checkout` and survives deletion of its task,
so validation is mandatory: `tasks/<slug>/task.md` must exist, its `id` must match the directory
name, and `status` must be in {active, paused}.

```text
is the binding valid?
├── yes → load the task
└── no → do NOT load silently; report the issue and enter discovery
```

A mismatch between `branch` and the current branch is **a warning, not an error**: multiple
tasks in one worktree are normal, and branch remains advisory; update it or leave it empty.

Silently loading the wrong task is worse than making no automatic selection. Silent rebinding
is also unacceptable: `bind` to a different task requires an explicit `--force`.

`bind` and `unbind` hold a short OS `flock` for one session ID while reading, validating, and
changing the binding. Different session IDs operate independently; there is no shared lock.
`--force` permits rebinding but bypasses neither locking nor validation. Files in
`sessions/.locks/` are retained; the OS releases the lock when the process exits. JSON is
replaced atomically using a temporary file and `os.replace`.

`task new` reserves the directory with an exclusive `mkdir`; a second invocation with the same
slug is rejected. A crash may leave a directory without `task.md`, which the state checker
reports. Such a directory is not automatically deleted or overwritten: inspect its contents
before recovery.

---

## 6. Session lifecycle

```text
SESSION START
    ↓
resolve task (sessions/<id> → validate → discovery, without silent selection)
    ↓
load task.md + agent-system task journal [slug] in full, without skipping errors
    ↓
inspect repository state
    ↓
WORK
    ↓
explicit checkpoint command? → yes: main → NEW journal/<ts>-<hash32>-<ordinal6>.md
    ↓
continue work / finish / switch harnesses without a mandatory write
```

In v1, startup/resume is an explicit main procedure defined in `AGENTS.md`, with no mandatory
hooks. If automated in the future, session-end hooks may add telemetry and validation, but
they are neither a source of state nor a prerequisite for recovery.

---

## 7. Completed task lifecycle

During work, `.agents/state/tasks/<slug>/` is committed to the task branch.

After completion, `task.md` and `journal/` remain in `.agents/state/tasks/<slug>/`, including
after merge/squash. Completed journals are not read during normal session startup; consult
them as needed. To resume a task, select it explicitly, inspect the current code, update
`branch` and `status`, then bind the chat.

Status changes to done/abandoned, and bindings to the task are removed. A remaining live
binding to a completed task is a state error reported by the validator. `status: paused`
means a task we intend to return to: it is valid for binding, but discovery does not suggest it.

### Knowledge publication rule

Task completion is a **knowledge publication step**. Without it, important findings remain
only in a journal that subsequent tasks do not normally read.

At completion, review the journal and distribute knowledge to two destinations:

| What | Where | Why |
|---|---|---|
| code and architecture decisions that people need to know | project documentation, in Git | this is project knowledge, not agent knowledge |
| how to work with this repository: where things are, what builds slowly, testing pitfalls | `.agents/memory/` (§2.1) | useful to the agent, of no interest to people, does not belong in project history |
| progress of the specific task | remains in the task journal | retained for returning to the task without cluttering project memory |

The third row is as important as the first two: publishing everything would turn memory into
a second journal and make it useless.

---

## 8. `AGENTS.md` and `CLAUDE.md`

`AGENTS.md` is the canonical project contract and contains only rules applicable to both
harnesses: architecture rules, coding constraints, testing expectations, delegation policy,
state lifecycle, definition of done, and repository conventions.

`CLAUDE.md` is a bridge through an import, not a second copy. Claude-specific settings, if
needed, are added on top of the imported content.

---

## 9. Canonical agent schema

Markdown + YAML frontmatter:

```md
---
name: implementer

description: >
  Implements scoped code changes and their tests.
  Use when the implementation approach is sufficiently understood.

role: implement

models:
  claude: sonnet
  codex: <codex model id>
effort: high

capabilities:
  - filesystem-read
  - filesystem-write
  - code-search
  - shell
  - vcs

overrides:
  claude: {}
  codex: {}
---

Implement the requested change within the defined scope.
Inspect the existing implementation before editing.
Follow existing project patterns unless there is a concrete reason not to.
Write or update tests together with the implementation.
Verify the resulting behavior before returning.
```

### Configuration axes

**`models`** contains **explicit model names per harness**, not an abstract tier.

There is only one reason for indirection here: one canonical field must expand to two different
values because model names differ between harnesses. A couple of lines in the agent file solve
that problem — read the definition and immediately see what will run.

A tier abstraction (`premium/standard/fast` + a mapping table) pays off when there are many
agents and “move every standard agent to another model” becomes a real bulk operation. With
three roles, editing three files costs less than maintaining a dictionary that must be kept
in mind. Introduce it when the set grows.

**The orchestrator model is not part of the canonical definitions.** It is session configuration:
`/model` in Claude Code, `model` in `.codex/config.toml`. It changes as circumstances require —
running it through a generator serves no purpose. Set it manually where it belongs.

**`effort`** is `high | medium | low`. It specifies the required reasoning effort, independently
of the model: only combinations supported by the selected model and harness version are valid.
The adapter validates the resulting pair after `overrides`; an unsupported combination is an
error, not a silent change to the model or effort.

**`capabilities`** is `filesystem-read | filesystem-write | code-search | shell | vcs | web`.
Capabilities describe intent and the role's required restrictions, but do not enforce permissions
on their own. The adapter records which restrictions are enforced natively and which remain
instructions. `shell` and `vcs` can change files: the absence of `filesystem-write` cannot be
enforced merely by disabling Edit/Write. If a required boundary cannot be expressed natively,
generation fails with an explanation; permissions are never silently expanded. `overrides`
undergo the same validation.

This is **where** abstraction does real work: harnesses have different toolsets and permission
models, and `filesystem-write` really must be translated into specific configuration. A model
name needs no translation — it is a substitution that should be visible directly.

There is no separate `write_access`: `filesystem-write` already encodes the same permission.
Two fields for one permission are a source of inconsistency.

### `overrides`

An escape hatch for behavioral divergence between harnesses. Rules:

1. Empty by default.
2. Test the canonical prompt without a delta first.
3. Add an override only for **observed** divergence.
4. Prefer a regression test/eval that explains why the override exists.
5. An override must not grow into a second complete prompt fork.

The goal is `shared semantics + small compatibility deltas`, not two different prompts.

### The `skills:` field is not used in v1

“Available to the agent”, “discoverable by the agent”, “selected dynamically”, “preloaded into
the subagent's context”, and “required for the role” have different meanings that should not
be combined prematurely.

Skills are selected by the harness's native mechanism based on their own `description`, or
invoked explicitly. If a strict dependency becomes necessary later, introduce a separate field
with defined semantics (`required_skills:`), after checking both harnesses' behavior.

---

## 10. Skills and the authoring rule

The format is the Agent Skills specification, unchanged:

```text
.agents/skills/<name>/
├── SKILL.md      core workflow, decision logic, navigation, invariants
├── references/   large checklists, standards, detailed documentation
├── scripts/      deterministic automation, validation, generation
└── assets/       templates, static resources
```

Not every directory is required.

### Rule

> A canonical prompt describes intent and procedure, not a specific harness API.

```text
GOOD: Inspect the changed files.
BAD:  Use the Read tool on each changed file.

GOOD: Delegate independent investigation when useful.
BAD:  Call the Agent tool with subagent_type=explorer.
```

### `description` versus body

```text
description = WHAT + WHEN     routing metadata
body        = HOW             procedure
```

Stating applicability conditions in `description` is **required, not prohibited** — that is
its purpose. The restriction applies only to the body: do not hardcode the names of a specific
harness's mechanisms.

*(An early version justified this rule by suggesting that Codex might not have skill dispatch,
and prohibited wording such as “use when…”. That justification was incorrect — see §16 — and
the prohibition harmed `description`. The rule above supersedes it.)*

---

## 11. Invariants and parallel work

### Working on different features simultaneously

This is supported and is the primary scenario for parallel use — through **Git worktrees**,
with one harness per worktree:

```text
repo/              main worktree    → Claude, feature A
../repo-billing/   linked worktree  → Codex,  feature B
```

The design supports this without changes:

- `sessions/` is gitignored → **not shared between worktrees**; each has its own;
- `.agents/state/tasks/<slug>/` is committed → task A's state lives on branch A and task B's
  on branch B; changes remain isolated until merge, where ordinary conflicts are still possible;
- the skills symlink is relative → it resolves independently in each worktree.

Consequences to keep in mind:

- **Agent and skill definitions are versioned by branch.** A skill change on branch A is not
  visible on branch B until merge. This is acceptable for infrequently changed files; when
  actively editing skills, keep them in separate small commits on `main` and pull them in.
- **`runs.jsonl` is also per-worktree**, so telemetry is fragmented. To compare harness
  behavior, logs must be compared explicitly: different tasks and revisions do not, by
  themselves, constitute a controlled experiment.

### Invariants

**The task belongs to the chat, not the worktree.** Multiple tasks in one worktree and multiple
chats on one task are normal. The earlier invariant of “one active task and one main session
per worktree” has been removed: it protected against exactly one harmful case — two
orchestrators writing to one `journal.md` — and the format has eliminated that case.

**The journal is append-only at the directory level.** One entry — one file,
`journal/<ts>-<hash32>-<ordinal6>.md`; existing entries are not edited. The temporary file is
written in full and `fsync` completes before exclusive publication; a name collision selects
the next ordinal (§2.2). There is no shared task counter or shared journal lock. A short OS
`flock` serializes bindings for the same session.

**The task's main coordinates changes to `task.md`** — `status` changes and scope edits.
Atomic file replacement does not resolve substantive conflicts between chats.

**Only main writes canonical task state.** Within a session, concurrent implementers are
assigned non-overlapping areas; subagents and background processes also stop writing before
an explicitly invoked `$checkpoint`.

**`runs.jsonl` is never canonical state.**

The new protocol does not use the shared advisory file `.agents/state/LOCK`. A remaining
legacy `LOCK` is not removed automatically: the validator warns, and before removing it
manually, verify that the old session is no longer running. Legacy `ACTIVE` remains a
candidate; a successful `bind` to the exact task it names removes it only when `LOCK` is absent.
Binding to another task or the presence of `LOCK` preserves `ACTIVE`. After checking the old
session and manually removing `LOCK`, repeat the corresponding `bind`. Both legacy files
retain their gitignore rules until migration is complete. They do not replace per-session
OS `flock`.

> **Automatic binding GC** is not implemented: a chat might be bound to a task completed
> from another window, and its file cannot be silently removed without being asked.
> The traversal that such a GC would reuse is already extracted into `check_state.gc_candidates`.


---

## 12. v1 agents

Three roles with clearly distinct responsibilities.

**`explorer`** — codebase investigation, documentation research, finding existing patterns,
tracing dependencies, and gathering facts. Does not change production code by default.

**`implementer`** — implementation, accompanying tests, local verification, and targeted fixes.
Tests are part of implementation, not a separate later phase.

**`reviewer`** — independent verification: correctness, regressions, broken invariants,
security-sensitive changes, test adequacy, and scope violations. Inspects the repository's
actual state, not just the implementer's summary. Does not fix findings by default.

### Why there is no `tester`

Tests derived only from the implementation risk reproducing its errors, regardless of the
author's role. Initially, this is sufficient: the implementer writes code and tests → the
reviewer independently assesses their adequacy against the requirements.

Separate roles (`test-designer`, `security-reviewer`, `integration-verifier`) are added when
real tasks demonstrate a need for independent context.

### Why there is no `architect`

Add this role when a recurring class of tasks requires cross-component design, public API
or data model changes, architectural boundaries, migration planning, or involves high
ambiguity. Until then, main handles planning.

---

## 13. Complexity gate

Delegation is determined by semantic signals, not the model's impression.

**TRIVIAL** — no public contract change, no schema/migration, no auth/security logic, no
concurrency or distributed state, no cross-component invariant, an obvious implementation
pattern, and low ambiguity.
Path: `main or implementer → verification`. Reviewer is optional.

**NORMAL** — the default; the task is not obviously trivial and has no complex signals.
Path: `explorer if needed → implementer → reviewer`.

**COMPLEX** — a public API/protocol change, schema migration, risk of data loss,
auth/permissions, concurrency, distributed state, a cross-service contract, an architectural
boundary change, highly ambiguous requirements, or a new implementation strategy.
Path: `explorer → planning/architect if available → implementer → reviewer`.

### Gate rules

The number of changed files is a **weak** signal. Renaming 20 generated files may be trivial;
changing one line of authorization logic may be complex.

When uncertain, choose `NORMAL`. An explicit user or orchestration policy override is allowed
in either direction.

---

## 14. Fix loop and orchestration

Reviewer findings return to the implementer: `implement → review → fix → review`.
The default maximum is 2–3 corrective rounds.

After that, main **diagnoses why the process is not converging**, instead of escalating
automatically upon reaching a numeric limit:

```text
unclear requirement      → ask the user
technical unknown        → explorer / main's planning (architect once the role is introduced)
implementation thrashing → stop the loop and summarize
fundamental problem      → return to planning
```

In v1, orchestration is a policy layer over the main session: `understand → implement →
independent review → fix if needed → verify`, without a mandatory multistage chain for every
small task; the gate determines which stages are needed.

No separate permanent `orchestrator` agent is created: the corresponding harness's main
session is the orchestrator.

---

## 15. Generator, adapters, drift

```text
agents/*.md            (in an installed project: .agents/agents/*.md)
        ├── .claude/agents/*.md
        └── .codex/agents/*.toml
```

The Codex format has been confirmed against the current documentation: project agents are
standalone TOML files in `.codex/agents/` (personal agents in `~/.codex/agents/`); see the
[official documentation](https://learn.chatgpt.com/docs/agent-configuration/subagents).
Verification records the supported harness versions and confirms that the actual runtime
loads the generated agent, rather than only checking valid TOML/YAML. The adapter is still
needed so a future vendor format change does not affect the canonical definitions.

The generator is deterministic and supports `generate` and `--check`. Generated artifacts
are committed; CI runs `--check` and fails if the canonical source changed but the output did
not. `--check` does not change files and also detects stale generated files; orphan cleanup
removes only artifacts whose ownership by the generator is confirmed by a manifest or marker.
A pre-commit hook may run generation for convenience, but CI is the authoritative enforcement.

### Adapter implementation order

A generic `capabilities.py` is unnecessary before real differences emerge:

```text
1. Manually define the same agent for Claude and Codex.
2. Record the actual differences.
3. Implement minimal adapters.
4. Extract shared capability mapping only when repetition appears.
```

---

## 16. Native harness capabilities

Both harnesses have native project subagents and native skills with implicit and explicit
invocation. Consequently:

- a fallback in which one session sequentially simulates all roles is **unnecessary**;
- the generator has two fully supported targets;
- the authoring rule is justified by portability, not a lack of dispatch (§10).

The corresponding harness's main session is the orchestrator.

---

## 17. Telemetry and evals

`.agents/runs.jsonl` provides **local observability, not portable state**, and is gitignored.
It may contain the task, harness, selected gate path, invoked agents, number of findings,
fix rounds, and outcome.

Its purpose is to observe behavioral divergence between Claude and Codex, identify unnecessary
delegation, and assess the value of roles. The limitation is deliberate: comparisons work
within one working environment. Aggregation across machines is a separate feature absent
from v1.

Telemetry **is not** a regression test. Portability requires separate checks:

```text
one canonical agent → Claude artifact + Codex artifact
    → schema validation
    → behavioral smoke case where practical
```

---

## 18. Integration surface

All integration between harnesses consists of:

```text
git / working tree
+ AGENTS.md
+ .agents/agents/
+ .agents/skills/
+ .agents/state/tasks/
```

There is no other state-transfer channel. Telemetry and session transcripts are not part of it.
This is an architectural constraint, not an oversight.

---

## 19. Implementation order

| Stage | Content |
|---|---|
| 0 | `.agents/memory/`: store, symlink, `.gitignore` (§2.1) — first of all, two lines |
| 1 | `.agents/state/` + `tasks/` layout, lifecycle contract, active task resolution |
| 2 | `checkpoint` skill (§3.3); verify switching in both directions without a generator |
| 3 | `AGENTS.md` + `CLAUDE.md`; verify that both harnesses interpret the rules identically |
| 4 | Shipped portable skills: `checkpoint`, `migrate-memory`; unrelated project skills are preserved |
| 5 | Three canonical agents; **write native definitions for both harnesses manually in parallel** |
| 6 | `gen_agents.py` + adapters + `--check` + orphan cleanup + schema validation |
| 7 | Verify startup/resume according to AGENTS.md in both harnesses; hooks are not mandatory in v1 |
| 8 | Complexity gate and a bounded fix loop |
| 9 | Evals, drift tests, optional telemetry |

Stage 5 aims to **understand the actual differences before writing an abstraction layer**.

Stages 1–4 require no Python at all: they are Markdown files and a rule in `AGENTS.md`.
The generator arrives at stage 6, once its purpose is clear.

---

## 20. v1 readiness criteria

```text
 1. Create a task in Claude and record it in canonical task state.
 2. Complete part of the implementation; verify that no automatic checkpoints occur.
 3. Explicitly invoke $checkpoint and verify the journal entry.
 4. Open the same repo/branch in Codex.
 5. Codex determines its session ID and task (binding or explicit bind).
 6. Codex understands the current state without a manual recap.
 7. Codex invokes an equivalent canonical subagent.
 8. Codex continues the work.
 9. Switch back without $checkpoint; Claude checks saved state against Git.
10. Generated definitions are in sync according to --check.
```

A separate durability check:

```text
11. Interrupt the session WITHOUT invoking $checkpoint.
12. The next session recovers state from the task journal,
    subject to the loss boundaries in §3.2.
    Test interruption while main is working without subagents and during parallel delegations;
    do not present unverified changes as completed work.
```

And a concurrency check:

```text
13. A second worktree on another branch, another harness, another task.
14. Both sessions work without interfering with each other or confusing bindings.
15. Two chats on ONE task in one worktree write checkpoints simultaneously:
    two distinct complete files are produced, with no lost entries.
    Repeat for a shared session ID prefix, the same session, and the same timestamp;
    verify retry when a name is taken, including a hash collision.
16. The canonical reader displays legacy journal.md, old and new entries in validated order;
    it resolves ambiguous old names using session metadata, rejects corrupted entries,
    and does not convert any old files.
17. Concurrent bind/unbind for the same session ID are serialized; --force does not bypass
    flock. A process crash releases the OS lock; different sessions remain independent.
18. Concurrent task new with the same slug creates only one task. An incomplete directory
    without task.md after a crash is detected and is not automatically deleted.
19. Session IDs are not trimmed; . and .. are invalid. A checkpoint with an explicit slug
    requires an existing active/paused task; --stage rejects spaces, #, and newlines.
20. Legacy ACTIVE is removed only after the corresponding bind without LOCK;
    if LOCK exists, both are retained with a warning to check the old session
    before manually removing LOCK and repeating bind.
```

Items 11–12 matter more than the others: without them, the system works only after a clean
session ending and therefore fails to solve the original problem.

None of this requires Claude-specific or Codex-specific task state, duplicate canonical
prompts, or cross-harness subprocess orchestration.

---

## 21. Source lineage

| Layer | Source | What we take |
|---|---|---|
| canonical/generated lifecycle, drift check | `Lukk17/agent-standards` | source-of-truth → generated targets structure |
| portable authoring, adapter ideas | `wshobson/agents` | intent instead of tool APIs, mapping principles |
| skill format | `agentskills/agentskills` | Agent Skills spec |
| workflow semantics | `obra/superpowers` | understand/implement/review/fix, without bloat |
| deferred | `Claudex5` | model escalation, cross-model review — not in v1 |

Marketplace structures are not copied wholesale: we take patterns, checklists, adapter ideas,
and workflow semantics, not dozens of narrowly specialized roles. When borrowing code or
substantial prompt sections, check licenses and retain the required notices.

---

## 22. Decision log

To avoid reopening settled decisions.

| Decision | Status | Reason |
|---|---|---|
| Cross-harness orchestration | rejected | each has its own runtime, permissions, and lifecycle; removes exec-recipe, the wrapper, and sandbox orchestration |
| Handoff only at session end | rejected | SPOF: when a usage limit is reached, there is no turn left for serialization |
| Write `handoff.md` on `SubagentStop` | rejected | a subagent does not know orchestration state; parallel execution causes a race for the file; incorrect ownership |
| The orchestrator writes checkpoints after evaluating results | accepted | distinguishes accepted facts from explicitly rejected approaches |
| Checkpoint only on an explicit manual command | accepted | session events do not require a write; there are no automatic triggers |
| Checkpoint marker and partial journal reading | rejected | the selected task's journal is read in full; the canonical reader validates filename format and metadata |
| Monotonic timestamp-based ID | rejected | timestamps provide display order, not causal clocks: clock changes and different machines rule out that guarantee |
| Separate `handoff.md` snapshot | rejected | the journal is read in full, so no projection is needed; removes the precedence rule, snapshot freshness question, and a procedure step |
| Full status recap in every entry | rejected | friction on every write and a journal that people stop rereading; intentions and decisions still belong alongside results |
| Memory in the project's Git repository | rejected | memory is project-scoped, not branch-scoped: knowledge on a feature branch is invisible to other branches until merge |
| A memory repository per project | accepted | the store root is an ordinary directory; each project has its own history and private remote |
| Project key resolution scheme | rejected | the symlink establishes the association; resolve name collisions manually if they occur |
| Versioning installed `.agents/` | accepted for the target project | definitions and journals are versioned; memory, bindings, telemetry, and legacy ACTIVE/LOCK are gitignored. The CLI clone separately excludes its entire local `.agents/` |
| Journal rotation that keeps only the tail | rejected | truncation is biased against early decisions that impose constraints; a new journal must start with a summary |
| Shared task lock | not introduced | the journal publishes separate files exclusively; a short OS flock protects bind/unbind for one session ID, with no force bypass |
| Separate recovery log + hooks | rejected | v1 relies on manual checkpoints; a transcript is optional forensic evidence, not a durability guarantee |
| Persist subagent state | not required | within a session, subagents communicate natively with the orchestrator; files are needed only between sessions |
| Commit the chat binding | rejected | this is workspace state; causes merge conflicts and duplicates identity |
| Branch instead of a binding | rejected | detached HEAD, multiple tasks per branch, worktrees, renames; the branch is only an advisory hint |
| Silently load a task despite a mismatch | rejected | loading the wrong task is worse than selecting none |
| One pointer per worktree (`ACTIVE`) | rejected | the chat selects the task: multiple chats on different tasks may legitimately be open in one worktree |
| Shared journal entry numbering | rejected | instead of a shared counter, the name contains a timestamp, hash32 of the full session ID, and an ordinal starting at 000001; exclusive publication with retry protects against collisions |
| Sanitize session IDs | rejected | converting an ID to a “safe” form silently merges two distinct chats; invalid values are rejected |
| Separate reverse index of “chats for task X” | rejected | this is a listing of `sessions/`; a second structure would drift out of sync |
| `write_access` alongside `filesystem-write` | rejected | two fields for one permission |
| `model_tier` + mapping table | rejected | with three agents, a dictionary costs more than three edits; model names are explicit |
| `policy.yml` / orchestrator model in canonical definitions | rejected | this is session configuration that changes during work; set it manually in the harness |
| One harness per worktree as a restriction | reframed | this is a concurrency mechanism: different features use different worktrees |
| Agent `skills:` field | deferred | combines five different semantics; introduce as `required_skills:` after verification |
| Separate `tester` | deferred | implementer + reviewer are sufficient for v1; independent tests are derived from requirements |
| Separate `architect` | deferred | main handles planning until a real class of tasks requires the role |
| File count as a gate criterion | downgraded to a weak signal | 20 renames are trivial; one line in auth is not |
| Escalate to the user at a numeric limit | rejected | diagnose why the process is not converging and route accordingly |
| `runs.jsonl` as portable state | rejected | local observability; not part of the integration surface |
| Skills symlink | retained | objections concerned Windows/CI/Docker, none of which are currently in use |
| Retain task state in `main` after merge | accepted | task.md and the journal are kept for returning to the task; completed journals are read as needed |
