# Agent and skill authoring rules

## Main rule

> A canonical prompt describes **intent and procedure**, not a particular harness's API.

```text
GOOD: Inspect the changed files.
BAD:  Use the Read tool on each changed file.

GOOD: Delegate independent investigation when useful.
BAD:  Call the Agent tool with subagent_type=explorer.
```

The reason is portability: harnesses name their mechanisms differently, and a prompt
that hardcodes those names is no longer canonical.

## `description` versus body

```text
description = WHAT + WHEN     routing metadata
body        = HOW             procedure
```

Stating applicability conditions in `description` is **required, not prohibited** —
that is its purpose. Both harnesses use `description` for selection.

The main rule's restriction applies only to the body.

```yaml
description: >
  Reviews completed implementation for correctness and regressions.
  Use after non-trivial implementation changes.
```

## Skills: progressive disclosure

```text
SKILL.md      main workflow, decision logic, navigation, invariants
references/   large checklists, standards, detailed documentation
scripts/      deterministic automation, validation
assets/       templates, static resources
```

`SKILL.md` must be useful on its own. Read references as needed, rather than in full:
otherwise, progressive disclosure becomes one large file split into pieces.

## Language

Canonical prompts and skills are written **in English**. Both models work more accurately
in this language, and it removes ambiguity about the response language.

System documentation (`docs/`, `README.md`) and `AGENTS.md` are also written in English.
The repository uses English throughout, including scripts, generated text, and comments.

## What to avoid

- **Phrases such as "when you are invoked for X"** — the file must work both as role
  instructions and when read as ordinary text.
- **References to specific models** in the prompt body: models change, while the prompt is canonical.
- **Duplication between an agent and a skill.** If several roles need a procedure,
  make it a skill rather than a paragraph in three prompts.
