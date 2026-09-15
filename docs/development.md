# Developing Agent System

# agent-system

A shared set of agents, skills, and working state that works consistently
in Claude Code and Codex — one harness per session, with switching between them
without manually retelling the context.

Full design: [docs/design.md](design.md).
The operational contract read by both harnesses: [AGENTS.md](../AGENTS.md).

## Setup

```bash
./setup.sh
```

The script is idempotent. It:

1. creates an ordinary `~/.agents-memory/` directory without a shared `.git`;
2. creates a separate Git repository at `<key>/` for project memory;
3. creates the `.agents/memory` and `.claude/skills` symlinks.

Run it **in every worktree and on every machine** — the symlinks are gitignored
and are not shared between worktrees.

The project key is the name of a subdirectory in the store. It **must be the same
across all worktrees of a repository**; otherwise, the worktrees see different memory,
even though the symlinks exist and no error is reported. The name of the *current*
directory cannot be the key: a linked worktree has a different name by design.

`setup.sh` selects and **remembers** both values:

| | Priority |
|---|---|
| Key | Explicit argument → saved `agents.memoryKey` → existing link → main Git worktree directory name + `-memory` |
| Store | `AGENTS_MEMORY_STORE` → saved `agents.memoryStore` → existing link → `~/.agents-memory` |

They are saved in the shared local Git config (`git config --local`; neither global
config nor `config.worktree` is used), which is shared by all worktrees. The values
therefore survive both a rerun without the environment variable and a rename of
the repository directory.

Outside Git there is nowhere to save them: the script warns and uses the current directory
name + `-memory`; an explicit key must be passed every time. A fresh clone does not receive
the Git config — set the key once with `./setup.sh <project>`.

Explicitly changing the key or store reconfigures **only the current worktree**;
rerun `./setup.sh` in the others. `python3 tools/check_state.py` detects out-of-date
worktrees and reports an error. The contents of the old memory are not transferred automatically.

Moving memory to another machine:

```bash
cd ~/.agents-memory/<key> && git remote add origin <private-remote> && git push -u origin main
# On the new machine:
git clone <private-remote> ~/.agents-memory/<key>
```

## Repository contents

```
agents/             three roles: explorer, implementer, reviewer (canonical) → .agents/agents/
templates/          task.md, entry.md                                        → .agents/state/templates/
skills/             checkpoint and migrate-memory                            → .agents/skills/
resources/          contract.md                                             → .agents/agent-system/
tools/              generator, adapters, installer, state validator, tests
bin/agent-system    CLI entry point
.claude/agents/     GENERATED from agents/ — roles for work on this repository
.codex/agents/      GENERATED
```

The right side shows where the installer places each source in the target project.
State also lives there: `.agents/state/tasks/<slug>/task.md` +
`journal/<ts>-<hash32>-<ordinal6>.md` and `.agents/state/sessions/<session-id>`
(the chat binding, gitignored).

In the CLI clone, the entire `.agents/` directory is a workspace for agents working
on the CLI itself (local skills, memory, tasks), gitignored along with the `.claude/skills` link.
In a connected project, installed definitions and task state can be committed;
memory, bindings, telemetry, and legacy `ACTIVE`/`LOCK` are excluded until migration.
The installer preserves other project skills; the clone's local skills are not shipped.

`agent-system task journal [slug]` reads the journal: legacy `journal.md` first,
then validated old and new entries. Without a slug, the current chat's binding is used;
corruption causes an error, and old files are not converted. The timestamp defines
a deterministic display order, not a causal sequence across machines.

New names have the form `<YYYYMMDDThhmmssZ>-<hash32>-<ordinal6>.md`: the first 32 lowercase
hex characters of the full session ID's SHA-256 hash and a six-digit ordinal starting
at `000001`. After fully writing and syncing the temporary file with `fsync`, the CLI
publishes it through `link` without overwriting, retrying if the name is taken.
There is no shared journal lock. `bind`/`unbind` are serialized by a short per-session
OS `flock` in `sessions/.locks/`; `--force` does not bypass the lock. Legacy `ACTIVE`/`LOCK`
remain gitignored: the former is removed only after the corresponding `bind` without `LOCK`.
The latter is removed manually after verifying that the old session is inactive,
then the corresponding `bind` is repeated. See the operational contract for details
on task creation and validation.

## Commands

```bash
python3 tools/gen_agents.py           # generate native definitions
python3 tools/gen_agents.py --check   # drift check, makes no changes (for CI)
python3 tools/check_state.py          # check task state, bindings, and memory
python3 -m unittest discover tools/tests -v
```

The generator fails with an explanation if a canonical access boundary cannot be
expressed using the harness's mechanisms, rather than producing a configuration with
broader permissions than declared. This is its main property; see `docs/harness-differences.md`.

## Status

Stages 0–7 and 9 from design §19 are implemented: state, skills, roles, a generator
with adapters, drift checking, a state validator, 44 tests, and CI.

**Stage 8 — the complexity gate and fix loop — is implemented as policy in `AGENTS.md`,
but has not been exercised.** Code alone cannot settle this: real tasks are needed
to learn whether the gate triggers where it should and whether the reviewer actually
finds anything. See `docs/telemetry.md` for what to collect.

### Requires verification

- **Codex model names** were taken from the documentation and have not been verified
  by running them. They are marked `VERIFY` in `tools/adapters/codex.py`. If the names
  turn out to differ, only the adapter changes — not the canonical definitions.
- **The table of Claude `effort` levels by model** was taken from the documentation
  and has not been verified by running it (`VERIFY` in `tools/adapters/claude.py`).
  A model missing from the table is treated as unknown: `effort` is passed through,
  and the generator warns.
- **Explorer and reviewer have no `shell`**, so the read-only boundary is real.
  The cost: the reviewer cannot run tests, only read code.
- **None of the roles has been used in practice yet.** Passing tests confirm that
  the generator does what the tests expect — not that the harnesses accept its output.
  The next check is to run all three roles in both harnesses and interrupt a task,
  then continue it in the other harness.

