---
session: <session-id>
at: <YYYY-MM-DDThh:mm:ssZ>
stage: <stage-id>
---

One journal entry is one file:
`journal/<YYYYMMDDThhmmssZ>-<hash32>-<ordinal6>.md`. The filename and frontmatter are
constructed by `agent-system task checkpoint`: hash32 is the first 32 lowercase
hexadecimal characters of the full session ID's SHA-256 hash; ordinal starts at
`000001`. Publication is atomic and does not overwrite existing files; if the name
is taken, the CLI tries the next ordinal.
`stage-id` fully matches `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`.

Read the entire journal through `agent-system task journal [slug]`; do not skip a
corrupted entry. Record the delta, not a retelling of the entire state. The criterion:
anything essential for continuing that cannot be recovered from repository files.

<What happened and what it means.>

<If applicable:>
Decision: <agreed intent, including the next step if it is not obvious>
Rejected: <approach> — <reason>
Checks: <result, including "not run">
Worktree: <clean | dirty: what was left and why>
