# Agent System

A local CLI that adds a shared system of agents, skills, memory, and task state
to an existing Git project. Requires Python 3.11+, Git, and Bash; supports macOS and Linux.
No dependencies or model calls from the CLI.

## Install the command

Keep this clone in a permanent directory. Add the command to PATH once:

```bash
mkdir -p ~/.local/bin
ln -s /absolute/path/to/agent-system/bin/agent-system ~/.local/bin/agent-system
# ~/.local/bin must be on PATH
agent-system --version
```

Inspect any existing command with the same name before replacing it. If you move
the clone, update this link. All executable tools remain in the clone.

## Connect a project

```bash
cd ~/codes/existing-project
agent-system init --dry-run
agent-system init
agent-system doctor
```

When run from a subdirectory, the command selects the current worktree's root. To select one explicitly:

```bash
agent-system init --project /path/to/project
```

The CLI installs canonical roles, skills from `skills/`, native definitions, and task templates.
It adds marked blocks to AGENTS.md, CLAUDE.md, and .gitignore, preserving existing text.
The contract lives in `.agents/agent-system/contract.md`; file ownership is recorded in
`.agents/agent-system.json`. Both can be committed with the installed definitions.
Files, role/skill names, and symlinks owned by others are not overwritten. Resolve any
conflicts manually before running the command again. Review semantic conflicts between
existing rules and the contract with an agent: the CLI checks files, not the meaning
of instructions.

Running init again is safe. Updating from the current local clone is a separate action:

```bash
agent-system update --dry-run
agent-system update
```

The CLI does not update the clone itself or use the network. Update replaces only
unchanged files/blocks listed in the manifest. Local edits stop the update; there is no
automatic merge, force, or adoption of files owned by others. User edits outside the
managed blocks are preserved. To change the standard roles, edit the canonical definitions
in the tool's clone, then run update in the selected projects.

`doctor` checks the manifest, drift, links, task state, and memory without writing anything.
Exit codes: 0 — success (warnings are possible), 1 — conflict or detected fault,
2 — invocation or execution error. Doctor does not check agent execution or model availability.

## Task list and chat names

Run `agent-system task list` in the project (or add `--project /path/to/project`).
The task header shows status, branch, and journal entry count. Bound chats appear
below it, grouped by harness, with their local UI title and full session ID:

```text
task-a                   active     branch=main, entries: 1
    claude:
        chats:
            Review implementation: <session-id>
    codex:
        chats:
            Implement feature: <session-id>
```

Titles are looked up on each invocation, so local renames appear without rebinding.
Codex titles come from its read-only state database or session index (`CODEX_HOME`,
default `~/.codex`). Claude titles come from exact-session title events or rename
sidecars under `CLAUDE_CONFIG_DIR` (default `~/.claude`). These local formats are
version-dependent. Unavailable titles appear as `Untitled`; remote-only chats and
unknown harnesses still show their IDs. Titles are not saved in bindings, and task
listing does not change the applications' data. No title is synthesized from message text.

## Checkpoint

The `checkpoint` skill runs only when explicitly invoked by the user or an agent.
Finishing a stage or task, switching harnesses, and reaching limits do not trigger it automatically.
It creates a **new entry** at `journal/<ts>-<hash32>-<ordinal6>.md`
(`agent-system task checkpoint`) and verifies the write; existing entries are not rewritten.
`ts` has the format `YYYYMMDDThhmmssZ`, `hash32` is the first 32 lowercase hex characters
of the full session ID's SHA-256 hash, and `ordinal6` starts at `000001`. The CLI writes
and syncs a temporary file, then publishes it atomically without overwriting; if the name
is taken, it tries the next ordinal. Matching session ID prefixes or hash collisions do not lose entries.

On resume, the agent reads `task.md`, reads the entire journal with
`agent-system task journal [slug]`, and checks Git. Without a slug, the chat's binding is used.
The reader validates entries, returns legacy `journal.md` first, then old and new files
in canonical order; corrupted entries cause an error, and old entries are not converted.
Sorting is deterministic but does not guarantee causal order across machines.
Context acquired after the last entry may be lost. An automatic trigger at <=10%
remaining context is not implemented.

## Memory and worktrees

Key: `--memory-key` → shared local `agents.memoryKey` → existing link → main worktree name + `-memory`.
Store: `AGENTS_MEMORY_STORE` → shared local `agents.memoryStore` →
existing link → `~/.agents-memory`. Both values are saved in Git config; the absolute path
is not included in the manifest. The store root is an ordinary directory without `.git`.
Each `<key>/` is a separate Git repository for that project's memory. The CLI does not create
commits, publish memory, or configure remotes; synchronization requires a separate private
remote for each project. Saved keys and valid links are not renamed.

```bash
AGENTS_MEMORY_STORE=/custom/store agent-system init --memory-key my-project
```

Run init in each new worktree. It uses the shared saved settings.
A fresh clone does not have these local settings — specify the previous key and store explicitly.
The CLI preserves an existing valid link; a mismatch with the selected settings requires
explicit resolution, rather than silently switching memory.

If `.agents/memory` is an ordinary directory, init stops before writing anything. Have
the current Claude/Codex read the [migration skill](skills/migrate-memory/SKILL.md). It prepares
one fact per file and a correspondence report, preserving the original. After review:

```bash
agent-system init --memory-from /path/to/reviewed-package --dry-run
agent-system init --memory-from /path/to/reviewed-package
```

The original memory and report are preserved in a unique directory under `agent-system-backups`
inside the Git common directory. Legacy copies are not active memory, are not deleted automatically,
and are not transferred by clone. Do not delete the original repository until you have saved
any archives you need. The skill does not invoke another harness: the current agent performs
the semantic restructuring.

The old shared Git store (with `.git` at its root) is rejected before any changes, including dry-run.
First preserve its history and prepare separate memory repositories inside an ordinary directory.
The CLI does not delete the old `.git` or migrate its history automatically.

The project's Git ignore rules exclude `.agents/memory`, `.agents/state/sessions/`,
`.agents/runs.jsonl`, and legacy `.agents/state/ACTIVE`/`LOCK` until migration —
not the entire `.agents/`: definitions and task state remain versionable.
Completed `task.md` files and journals are retained after merge/squash;
on startup, the selected task's journal is read, and completed tasks are consulted as needed.

## Tasks and chats

A task is tied to neither a branch nor a worktree: the **chat** selects it, and the binding
lives in `.agents/state/sessions/<session-id>` (gitignored, one JSON line). Multiple tasks
in a worktree and multiple chats on the same task are normal.

```bash
agent-system task list                # tasks, their status, and bound chats
agent-system task status              # what the current chat sees
agent-system task new <slug>          # create a task from the template
agent-system task bind <slug>         # bind the chat; --force to rebind
agent-system task unbind              # remove the chat's binding
agent-system task journal [slug]      # read the entire journal
agent-system task checkpoint          # new journal entry, text from stdin
agent-system task set-status <slug> <status>
```

The CLI obtains the session ID from the harness environment: `AGENTS_SESSION_ID`,
`CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID`/`CODEX_SESSION_ID`. The exact complete
value is validated: 1–128 ASCII characters from `[A-Za-z0-9._-]`, except `.` and `..`.
The internal names `.locks`, `.gitkeep`, `.DS_Store`, and `.agents-<32 lowercase hex>`
are also reserved.
Spaces and newlines are not trimmed; an invalid ID is **rejected, not sanitized**.
If none of the sources provides an ID, the chat works without a binding — a valid mode.

A binding is **never created silently**: even if there is only one active task,
the CLI suggests it but does not select it. `bind`/`unbind` use a short OS `flock`
for one session; `--force` permits rebinding but bypasses neither the lock nor validation.
Files in `sessions/.locks/` are retained; the OS releases the lock when the process exits.

The old `.agents/state/ACTIVE` is removed after a successful `bind` to the task it names
only if `LOCK` is absent. Binding to another task or the presence of `LOCK` preserves
`ACTIVE`. `LOCK` is not deleted automatically: before removing it manually, verify
that the old session is no longer running, then repeat the corresponding `bind`.
Both legacy files remain gitignored until migration.

`task new` creates the directory exclusively: two invocations with the same slug do not
overwrite each other. A directory left without `task.md` after a failure is a detectable
error; the CLI does not remove it automatically. Inspect the remaining files manually before retrying.

`task checkpoint <slug>` requires an existing task with status `active` or `paused`,
just like a checkpoint using a binding. First explicitly resume a completed task with
`task set-status <slug> active`. `--stage` accepts 1–64 ASCII characters matching the
complete pattern `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`; spaces, `#`, and newlines are rejected.

## Interrupted installation

Before making changes, the CLI creates a per-worktree `agent-system-pending.json` in the Git directory
and backups in the shared `agent-system-backups/<id>`. If an operation fails, the marker remains,
and subsequent init/update/doctor invocations stop. The CLI does not promise a transaction across files and Git config.

To recover with an agent, use the marker and `record.json` in the specified backup:
compare `before` with the current files; restore previous regular files from `files/`
and previous links using `target`. Delete newly created files only after verifying that
they contain no later user changes. Restore both `memory_config` values
(null means the key was absent). For migration, use `legacy-original`
or verified `legacy-memory`; never delete the entire external store.
Empty directories and copied facts may be left in place. Only after verifying recovery
should you delete the pending marker and rerun the command. Keep the backup.

## CLI skills and local skills

`skills/` contains the sources of the two shipped skills: `checkpoint` and `migrate-memory`.
The CLI installs them in `.agents/skills/` in the target project; other skills are preserved.
Edit shipped skills in `skills/`, then run `update`.

`.agents/` in this clone contains local CLI development material and is entirely gitignored;
this is a separate policy from that of installed files in a target project. `.agents/skills/`
contains local skills: the installer does not read them, and additions and edits are not shipped.
The copies are independent and are not synchronized automatically. `setup.sh` connects local
skills for work in the clone; the CLI handles installation into other projects.

## Testing and development

```bash
python3 -m unittest discover tools/tests -v
python3 tools/gen_agents.py --check
```

CLI tests use temporary repositories and stores. Older generator tests temporarily modify
the checkout: run the full suite in an isolated copy.
The existing developer commands and setup.sh still work in the clone; `--project` has been added.
For details of the earlier structure, see [development](docs/development.md).

For real harness acceptance testing, see [acceptance](docs/acceptance.md). Passing CLI tests do not
prove that a particular Claude/Codex version executes the roles or that models are available.
