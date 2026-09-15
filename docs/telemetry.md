# Telemetry

`.agents/runs.jsonl` provides **local observability, not portable state.** It is gitignored,
outside the integration surface, and never canonical state.

Its purpose is to answer questions that the design currently answers with assumptions:

- whether the complexity gate triggers where needed and avoids unnecessary delegation;
- whether the reviewer actually finds anything and whether those findings are addressed;
- whether Claude and Codex behave differently in the same roles.

Without these data, design §13 and §14 remain hypotheses. That is why stage 8
(validating the gate's usefulness) cannot be "implemented" in code — it requires actual runs.

## Format

One JSON line per completed task or significant stage:

```json
{
  "ts": "2026-09-09T18:20:04Z",
  "harness": "claude",
  "task": "auth-refactor",
  "gate": "NORMAL",
  "agents": ["explorer", "implementer", "reviewer"],
  "findings": 3,
  "fix_rounds": 1,
  "result": "done"
}
```

| Field | Meaning |
|---|---|
| `gate` | `TRIVIAL` / `NORMAL` / `COMPLEX` — the selected path |
| `agents` | Agents actually invoked, in invocation order |
| `findings` | Number of findings returned by the reviewer |
| `fix_rounds` | Number of corrective rounds required |
| `result` | `done` / `abandoned` / `escalated` |

## What telemetry does not replace

**Regression tests.** Log entries describe what happened, not what should happen.
Portability needs separate checks: one canonical agent → artifacts for both harnesses
→ schema validation → a behavioral smoke test where practical. The first two steps
are covered by `tools/tests/`; the third requires actual runs.

## Limitation

The file is gitignored and therefore **per-worktree**. Comparing Claude and Codex works
within one working environment. This is a deliberate v1 limitation: aggregation across
machines is a separate feature that is not provided here.
