# Capability Map: Two Atomic Feature Commits

Module boundaries and order approved by the user in this conversation.

| Module id | Responsibility | Depends on | Specification |
|---|---|---|---|
| chat-titles | Display local UI titles for task-bound chats | Existing task state | SPEC-chat-titles.md |
| model-config | Persist and resolve project/chat model and effort preferences | Existing roles and state filesystem | SPEC-model-config.md |

Commit order: chat-titles → model-config. Neither feature depends on the other.
Each commit includes its specification, implementation, tests, and relevant documentation.
This map belongs to the first commit. No third commit is planned.

Shared README, CLI, and CLI tests are staged by feature. Existing unrelated files,
including TODOs.md and SPEC-harness-scoped-delegation.md, remain outside these commits.
Source file contents are preserved while the existing changes are split.
