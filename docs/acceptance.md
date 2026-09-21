# Acceptance testing in real harnesses

Use a temporary Git project with a simple function and test. Connect the current CLI,
create a task with `agent-system task new smoke`, and bind the chat to it
(`agent-system task bind smoke`). Do not use a production project or personal memory
for the run.

1. In Claude Code, check discovery of explorer, implementer, reviewer, and the shipped
   checkpoint and migrate-memory skills. Other project skills must be preserved.
   Explorer should read the code, implementer should change the function according
   to the contract and run the test, and reviewer should independently inspect the diff.
2. Verify that completing a stage and receiving a subagent result did not trigger a checkpoint.
   Explicitly invoke the `checkpoint` skill and verify that a NEW file appears in `journal/`
   with the verification result and deliberately unfinished work, while previous entries remain unchanged.
   End the original session without a final checkpoint; stop background writes.
3. Open the same project in Codex. Verify that it reads project memory, determines
   its session ID, binds to the task through an explicit command, reads all of task.md
   and the entire journal with `agent-system task journal [slug]`, and checks git status.
   Check the three roles and availability of the two shipped skills.
   Separately verify that two open chats on one task write checkpoints concurrently
   and obtain two different files rather than overwriting each other's entries.
4. Repeat the transfer in the opposite direction without a checkpoint command. Verify that
   ending the session and switching harnesses do not add a checkpoint; the new harness checks
   Git against the last explicit entry and does not present unsaved context as known.
5. Record the harness versions, models actually selected, results, and limitations.

A person or the current agent performs this run separately from the CLI. A list of names,
a TOML syntax check, or the main session's response cannot be treated as proof that a custom
subagent ran successfully. Disabled networking or missing authentication means
"not verified," not successful acceptance.

## v1 storage verification

These are additional verification scenarios, not a report that they have been completed:

1. Write checkpoints concurrently to one task from different chats whose session IDs share
   the first eight characters, then from one session with one timestamp.
   All entries must remain complete and distinct. Names have the form
   `<YYYYMMDDThhmmssZ>-<hash32>-<ordinal6>.md`; the ordinal starts at `000001`.
   Verify exclusive publication retries when a name is taken and when hash32 collides.
2. Read a mixed journal with `agent-system task journal [slug]`:
   legacy `journal.md` first, then old and new entries in validated order.
   Check an old session ID containing a hyphen/digits, a numeric ordinal greater than 9,
   rejection of a corrupted entry, and no conversion or modification of old files.
   The check covers deterministic display order; timestamps do not claim causal order
   across machines.
3. Run concurrent `bind`/`unbind` operations for one session ID and repeat with `--force`:
   the operations must use the same per-session OS `flock`. Interrupting the owner
   must release the OS lock; different session IDs must not block each other.
4. Two concurrent `task new` invocations with one slug create only one task. After a failure
   between reserving the directory and writing `task.md`, the remnant is detected as
   an error; a repeated command neither overwrites it nor removes it automatically.
5. Session IDs are validated without trimming; spaces, newlines, `.`, and `..` are rejected,
   as are the internal names `.locks`/`.agents-<32 lowercase hex>`.
   A checkpoint with an explicit slug rejects a missing or completed task before writing;
   a completed task must first be explicitly resumed. `--stage` accepts only a full match
   of `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`, rejecting `#` and multiline text, among other things.
6. Binding to another task preserves legacy `ACTIVE`; a matching bind removes it only
   when `LOCK` is absent. With `LOCK`, both files remain, with a warning to verify that
   the old session is inactive before manually deleting `LOCK` and repeating the bind.
   Both legacy paths remain gitignored until migration.

## Local attempt results, 2026-09-09

Automated checks: 70 tests pass, including 21 new CLI scenarios;
generated drift and Bash syntax checks pass.

The real run has not been completed yet. Claude Code 2.1.266 exited with
`Not logged in · Please run /login` when starting explorer. Codex CLI 0.149.0 could not
initialize app-server because access to its local state database was restricted.
These results neither confirm nor disprove that the installed roles load.
After signing in to Claude and running outside these restrictions, perform the scenario above.

## v1 stabilization verification, 2026-09-11

Automated checks ran in an isolated copy: **164 tests passed**,
including 23 safety and concurrency scenarios. The generated drift check is clean.
Coverage includes partial writes and fsync failure, reading before publication, identical names,
concurrent new/bind operations, flock release after process exit, symlinks in parent directories
and files, corrupted metadata, and reading legacy journals.

The live run used a temporary Git project with its own temporary memory. Codex CLI
**0.149.0** (main session model `gpt-5.6-sol`) successfully performed bind, an explicit checkpoint,
and task journal using the new CLI version. The marker's presence in the journal was verified
independently of the model's response. After `exec resume`, both `CODEX_THREAD_ID` and
`CODEX_SESSION_ID` retained their values; a new chat received different values, read the existing
entry, and did not create a new checkpoint. The first run selected an old command from the
login-shell PATH; a repeat using an absolute path to the CLI under test passed.
Acceptance testing must always verify which executable is used.

**Codex named roles were not counted as verified:** the spawn_agent schema available to this run
has no parameter for selecting a role configuration. Calls with task_name="explorer"/"implementer"/
"reviewer" only prove the creation of subtasks with those names. A separate run is required
where loading the installed configurations themselves can be confirmed.
The environment also reports warnings about invalid global role files;
those were not changed as part of this work.

Claude Code **2.1.266** reports `loggedIn: false`. The live Claude run, its resume check,
and switching between the two harnesses remain **not completed** until sign-in.
This is an acceptance limitation; automated tests do not replace these scenarios.
Resolved by the live Claude Code 2.1.268 run in the next section.

## Live Claude Code run, 2026-09-11

Claude Code **2.1.268**, authenticated. The run used headless mode (`claude -p`,
`--output-format stream-json --verbose`, `--setting-sources project` to exclude
user-level role and skill definitions) in a temporary Git project in the scratchpad,
with a temporary memory store (`AGENTS_MEMORY_STORE`). Evidence comes from the harness's
stream-json log, not model responses. The default main session model was `claude-sonnet-5`.

**Installation.** `agent-system init` wrote the roles, skills, contract, `CLAUDE.md`,
`AGENTS.md`, and the `.claude/skills -> ../.agents/skills` link; the preexisting
`foreign-skill` was preserved. `task new smoke` created the task; its contract was to fix
`clamp` in `mathlib.py`, with two tests known to fail.

**Step 1 — roles and skills verified.** The `system/init` event listed agents
`explorer`, `implementer`, `reviewer` and skills `checkpoint`, `migrate-memory`,
`foreign-skill`; the machine has no user-level `~/.claude/agents/`, so the source is the project.
There were three `Agent` calls with `subagent_type` = `explorer` → `implementer` → `reviewer`.
The subagents' tools matched the installed definitions: explorer used only `Read`;
implementer used `Read`, `Edit`, `Bash` (editing `mathlib.py`, running
`python3 -m unittest -q` until green); reviewer used `Read`, `Glob`, with no edits or shell.
The subagent models from `message.model` — sonnet, sonnet, opus — match the `model:` fields
in the installed `.claude/agents/*.md`. This distinguishes loading those specific configurations
from running a generic agent. Diff: two branches in `clamp`, tests unchanged, 4/4 passing.

**Step 2 — explicit checkpoints only.** The `sessions/<id>` binding was created by
`task bind smoke`; the ID came from `CLAUDE_CODE_SESSION_ID` (matching the supplied
`--session-id`). After three subagents and the end of the session, `journal/` was empty.
In the resumed session (`--resume`), an explicit command invoked `Skill: checkpoint`;
the skill ran `task journal smoke`, `git status`/`git diff`, then `task checkpoint --stage
reviewer`. One file appeared: `20260911T145033Z-<hash32>-000001.md`; `hash32` equals the
first 32 characters of the full session ID's SHA-256 hash, and the frontmatter contains
the full `session`. The entry records the review result and deliberately uncommitted `mathlib.py`.

**Step 4 within Claude — a new chat.** A fresh session with a different `--session-id`
and no bind command read the contract and `.agents/memory/` (empty), determined the session ID
from the environment, and saw the missing binding and the only active task — it **suggested
bind but did not create it**; `sessions/` was unchanged. It read `task.md` and the entire journal
with `agent-system task journal smoke`, checked `git diff` against the last entry
(a match), separately noted the uncommitted `journal/`, and did not present any unsaved
context as known.

**Two chats on one task.** An explicit `bind` for the second chat, followed by concurrent
`checkpoint` invocations from both (`--resume` for two sessions in parallel), produced
two different files with different `hash32` values (`…145243Z-5bd78e3d…-000001.md`,
`…145251Z-035f4518…-000001.md`). The first entry was unchanged, and `task list` showed
3 entries and 2 bindings. The entries were written 8 seconds apart — this run did not
reproduce a race for the same name at the `link` level; that is covered by
`tools/tests/test_state_safety.py`.

**Limitations.** The run was headless, not interactive; the transition was Claude → Claude,
not Codex → Claude in the same temporary project. Because task state is harness-independent
(the same files), the transfer is covered by the Codex run above and this run, but a single
end-to-end Codex ↔ Claude run on one project has not been performed.

## Project and chat launch configuration, 2026-09-17

The CLI stores per-role project preferences and independent chat overrides. Resolution
is field-by-field: chat role, chat defaults, project role, then inheritance. Project-wide
defaults and Claude effort overrides are rejected. The orchestrator applies preferences
when launching a role; saving config is not a harness control operation.

The final full suite passed: **190 tests** (`python3 -B -m unittest discover tools/tests -q`).
Generator drift, state validation, and `git diff --check` passed.

Local checks cover two chats with different settings, explicit inheritance, unset,
concurrent writes, malformed files, symlink rejection, custom roles surviving updates,
an unrelated application installer, and initialization with an empty role directory.
Generated files match their sources. Comparing all three canonical roles against HEAD
confirms that bodies, descriptions, capabilities, and overrides are unchanged; only
model/effort pins were removed. Review findings received regression tests and were closed.

Live acceptance remains incomplete:

- Claude Code 2.1.273 reported `loggedIn: false` in the local preflight.
- Codex CLI 0.149.0 could not initialize its app-server inside the filesystem sandbox.
  A request to run a bounded read-only test in a temporary project outside that sandbox
  was rejected by automatic approval review because the external process could send
  instructions/configuration to a service without separately approved data transfer.
  That run was not performed and the rejection was not bypassed.

After authorization is available, verify the actual selected model and effort from
harness launch metadata, including named-role loading and preserved access boundaries.
For Claude, verify session effort inheritance separately from the per-call model.
Do not count an agent's self-reported model or CLI resolution output as runtime proof.

### Authorized synthetic Codex attempt, 2026-09-17

After the user authorized a live test and transfer of test instructions, automatic
approval review still rejected the repository-derived fixture: it required explicit
permission for sending repository contract/role contents. No rejected command ran.

A narrower test containing only newly written neutral instructions, a `probe_reader`
TOML role with `sandbox_mode = "read-only"`, and `marker.txt` was approved and executed.
Codex CLI 0.149.0 ran with `--ignore-user-config --ephemeral --json`, parent model
`gpt-5.6-sol`, and parent effort `low`. The requested child was the named test role
with model `gpt-5.6-sol` and effort `high`.

The event stream contains no subagent spawn. The parent reported that its exposed
`spawn_agent` had `task_name`, `message`, `fork_turns`, `model`, and `reasoning_effort`,
but no role-selection parameter. This is a reported tool limitation, not independent
schema inspection or evidence of child execution. It did not substitute a generic
agent. Thus the run does not establish named-role loading, child effort/model, or
permission preservation. Desktop tool capabilities can differ from this CLI surface.
The CLI also emitted warnings about malformed global agent definitions even with
`--ignore-user-config`; those user files were not modified.

### Repository-authorized and Desktop attempts, 2026-09-17

The user explicitly authorized sending the repository role definitions and operational
contract. The full temporary-project CLI run was approved and executed. It resolved
reviewer to `gpt-5.6-sol` / `high`, read the native read-only definition, and did not
spawn: the CLI parent again reported no role-selection parameter. Permission review
is no longer the blocker for this test; the CLI tool surface is.

A separate Desktop `collaboration.spawn_agent` call selected `agent_type="reviewer"`,
`model="gpt-5.6-sol"`, `reasoning_effort="high"`, and `fork_turns="none"`. The child read
the temporary probe and returned `model-config-smoke`. Its local rollout metadata,
not its answer, records role `reviewer`, model `gpt-5.6-sol`, and effort `high`.
Evidence session: `01a0af9a-2a5f-70f0-a2d8-82c1c1ea72aa`, parent
`01a0abef-d33b-7071-a59b-3ea7bad593ee`, agent path `/root/named_role_acceptance`.

**Access-boundary acceptance did not pass.** The generated reviewer definition has
`sandbox_mode = "read-only"`, but the child's `turn_context.sandbox_policy.type` is
`workspace-write`. No write was attempted; the test therefore does not prove that
writes would succeed, but it cannot establish the requested read-only boundary.
Named-role metadata alone is insufficient evidence that the native definition's
permissions were enforced. Model/effort selection is verified on this Desktop surface;
complete native-role loading and permission preservation remain unresolved. Do not
present this as a successful end-to-end acceptance of all feature requirements.
