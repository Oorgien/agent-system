# Differences between harnesses

The goal of stage 5 is to **observe real differences before writing an abstraction layer**.
This file collects observations. The generator (stage 6) is based on them, not on guesses.

Add observations as you work with both harnesses. Each observation must include a date
and how it was verified.

---

## Observed while writing definitions by hand (2026-09-09)

These differences are visible from comparing files, **before** any actual run.

### 1. Model and effort

| | Claude | Codex |
|---|---|---|
| Model | `model: sonnet` (short aliases) | `model = "..."` (full ID) |
| Effort | `effort` in frontmatter | `model_reasoning_effort` |

**Corrected (2026-09-09).** The first version claimed that Claude had no field for
`effort`, and the adapter discarded the value with a warning. That was incorrect:
subagent frontmatter has an `effort` field that overrides the session's effort
(Claude Code documentation, "Subagents / Supported frontmatter fields"). While the value
was being discarded, the canonical definition promised an effort level, but the subagent
inherited the session's level.

There is still a difference, but it concerns **the allowed values and behavior when a value is unsupported**:

| | Claude | Codex |
|---|---|---|
| Levels | `low`, `medium`, `high`, `xhigh`, `max` | `minimal` … `high` |
| Model dependency | The set depends on the model | The same for all models |
| Unsupported value | **Silent downgrade** to the nearest supported lower level | — |

A silent downgrade is exactly the outcome the generator is meant to prevent,
so level/model compatibility is checked in `adapters/claude.py` before writing the file.
The canonical format knows only `low/medium/high` — the intersection available in both harnesses.

**Open.** The table of levels by model was taken from documentation and has not been
verified by running it; a model outside the table produces a warning rather than a failure.

### 2. Access boundaries — the main difference

| | Claude | Codex |
|---|---|---|
| Mechanism | `tools:` tool list | `sandbox_mode` sandbox setting |
| Nature | What the agent can invoke | What the process can do |

These are different things, with practical consequences. In Claude, `capabilities`
without `filesystem-write` is enforced by **omitting Edit/Write/Bash** from the list —
but adding `Bash` to provide `shell` immediately restores write access through `sed -i`.
In Codex, `sandbox_mode` restricts the entire process, so `shell` and read-only access are compatible.

**Consequence for the generator (design §9):** in Claude, the combination
`capabilities: [shell]` without `filesystem-write` is **inexpressible**, and generation
must fail with an explanation rather than silently provide Bash.

The current workaround is to give explorer and reviewer no `shell` at all.
This works but imposes a limitation: they cannot run `git log` or `rg`.

**Open.** Check whether Claude Code has a sandbox-level permission mechanism
(rather than a tool list) that could express read-only + shell.

### 3. System prompt storage

| | Claude | Codex |
|---|---|---|
| Format | Markdown, file body after frontmatter | TOML, string field |
| Escaping | Not required | Backslash, quotes, control characters |

**Clarified (2026-09-09).** The first version treated only triple quotes as dangerous
and proposed rejecting prompts containing them. The entire set matters: in a TOML
multiline basic string, a backslash starts an escape sequence, so parsing `\bword\b`
turned the backslashes into control characters, and `C:\temp` acquired a tab.
The text changed silently, without an error.

Escaping is **the adapter's responsibility**: a canonical prompt is not tailored to
one harness's output format. A round-trip test parses the output and compares it
with the original text.

### 3.1. Syntax is not schema

Codex's required prompt field is called `developer_instructions`. The adapter wrote
`instructions`, so all three definitions violated the schema, even though `tomllib.loads()`
parsed them without complaint: it checks TOML syntax, not the agent schema.

Consequence: output validation must check **required keys**, not just parseability.
This is implemented in `_check()` as `RenderError`, separate from `Inexpressible`:
the latter means the canonical definition cannot be expressed in principle;
the former means the adapter itself has a bug.

### 4. What turned out to be the same

- `description` as routing metadata phrased as WHAT + WHEN works in both;
- the prompt body is transferred **verbatim**, without harness-specific edits;
- `overrides` are still empty for all three roles — no behavioral differences have
  been observed because no runs have taken place yet.

---

## Observations from actual runs

*(Empty — fill in after the first runs.)*

Entry format:

```md
### <date> — <observation>

Harness: <which>
Expected: <what>
Actual: <what>
Consequence for the canonical format or generator: <what>
```

---

## Verified by the generator (2026-09-09, stage 6)

The observations above are implemented in `tools/adapters/` and covered by tests.

### The generator reproduces the handwritten definitions

After the adapters were written, the generated files were compared with those written
by hand in stage 5. **There are no semantic differences.** The only differences are the
`GENERATED` marker replacing the note about manual authorship, wrapping `description`
at 88 characters, and sorting the list in the capabilities comment.

This confirms that the canonical schema (design §9) is sufficient: it retains everything
in the handwritten definitions and loses nothing.

### `effort` is passed to both harnesses

Changing `effort: high → low` in the canonical definition causes drift in both files:
`effort` in Claude frontmatter and `model_reasoning_effort` in Codex TOML. Previously,
only the Codex file drifted — that was the visible sign of the bug, but it was interpreted
as the absence of a field rather than an adapter bug.

An incompatible level/model combination is a generation error, not a warning:
the harness would silently lower the level, so the definition would claim an effort level it does not provide.

### Access boundaries — failure instead of a false guarantee

`capabilities: [filesystem-read, shell]` without `filesystem-write` causes a generation
error with an explanation and options for resolving it. Covered by
`test_shell_without_write_is_inexpressible` and `test_vcs_without_write_is_also_inexpressible`.

The same set generates successfully for Codex with `sandbox_mode = "read-only"`:
the sandbox restricts the entire process. This is the most significant difference
between the harnesses — and the only place where the same canonical definition is
**fundamentally** expressible in one but not the other.

### The cost of the restriction

Explorer and reviewer currently have no `shell` at all, so the boundary is real.
The cost: they cannot run `git log`, `rg`, or tests. This matters for the reviewer:
it cannot "verify that tests pass"; it can only read.

**The decision is deferred** until there are observations: if this hinders real tasks,
there are two options — give the reviewer `filesystem-write` and acknowledge that it can
write, or leave test execution to main.
