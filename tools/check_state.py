#!/usr/bin/env python3
"""Task-state checks: the mechanical part of startup in AGENTS.md §2.

    check_state.py            check state
    check_state.py --resolve  also show what the current chat sees

Check exactly what AGENTS.md requires before loading a task, using the same rules.
Most importantly, broken bindings are NOT repaired automatically and their tasks
are not loaded: loading another chat's task is worse than choosing none.

This script only reports; it neither repairs nor writes anything.
"""
import argparse
import model_config
import state_fs
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import task_state as ts
from memory_layout import layout_error

ROOT = Path(__file__).resolve().parent.parent
MEMORY = ROOT / ".agents" / "memory"

OK, WARN, ERR = "ok  ", "warn", "ERR "


class Report:
    def __init__(self):
        self.rows, self.failed = [], False

    def add(self, level, msg):
        self.rows.append((level, msg))
        if level == ERR:
            self.failed = True

    def dump(self):
        for level, msg in self.rows:
            print(f"  [{level}] {msg}")
        return 1 if self.failed else 0


def git_branch():
    try:
        r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           cwd=ROOT, capture_output=True, text=True, timeout=5)
        b = r.stdout.strip()
        return None if r.returncode or b == "HEAD" else b
    except (OSError, subprocess.SubprocessError):
        return None


def git_config(key):
    """Value from the SHARED local config, common to all worktrees (AGENTS.md §1)."""
    try:
        r = subprocess.run(["git", "config", "--local", "--get", key],
                           cwd=ROOT, capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def expected_memory():
    """The symlink target required by saved settings, or None.

    Derive the expected target ONLY from state saved by setup.sh, never from the
    validator's environment. AGENTS_MEMORY_STORE is set ad hoc, while session
    startup runs without it; deriving a target again would report a mismatch
    in a correctly configured tree.
    """
    key, store = git_config("agents.memoryKey"), git_config("agents.memoryStore")
    return (Path(store) / key) if key and store else None


def memory(r):
    """Project memory is an unconditional startup step (AGENTS.md §2); always check it.

    Absence is a WARN: the symlink is gitignored and may legitimately be missing
    in CI or a fresh clone. A link to the WRONG directory when settings are saved
    is an ERR: the tree reads the wrong memory, rather than having none, so work
    must not silently proceed. This happens when setup was not rerun in a worktree
    after the key or store changed.
    """
    if not MEMORY.is_symlink():
        if MEMORY.is_dir():
            r.add(WARN, ".agents/memory — a directory, not a symlink to the shared store "
                        "(memory will not be shared across worktrees; see AGENTS.md §1)")
        else:
            r.add(WARN, ".agents/memory is missing — project memory is unavailable, "
                        "run ./setup.sh")
        return

    if not MEMORY.exists():
        r.add(WARN, f".agents/memory — broken symlink to {os.readlink(MEMORY)}, "
                    f"run ./setup.sh")
        return

    actual = MEMORY.resolve()
    error = layout_error(actual.parent, actual, require_repository=True)
    if error:
        r.add(ERR, error)

    expected = expected_memory()
    if expected is None:
        return

    # Call resolve() on the link, not os.readlink(): resolve relative targets
    # from the link's directory, not the process's working directory.
    if actual != expected.resolve():
        r.add(ERR, f".agents/memory points to {actual}, but saved settings specify "
                   f"{expected}. The tree reads the wrong memory: rerun ./setup.sh here")


def gc_candidates(root):
    """Bindings eligible for future GC removal: a single scan implementation.

    Currently this only reports: another window may have finished the bound task,
    and the binding must not be silently removed without a request. Future GC can
    reuse this scan instead of reimplementing it.

    Return [(session_id, record, reason)]. This does not distinguish live chats
    from dead ones: session process information is outside our knowledge.
    """
    out = []
    records, _ = ts.bindings(root)
    for sid, record in sorted(records.items()):
        problems = ts.binding_problems(root, record["slug"])
        if problems:
            out.append((sid, record, problems[0]))
    return out


def tasks(r, root, branch):
    """Check each task for integrity. Return slugs that accept bindings."""
    bindable = []
    for slug in ts.tasks(root):
        meta, err = ts.task_meta(root, slug)
        if err:
            r.add(ERR, err)
            continue
        problems = ts.task_problems(root, slug, meta)
        for problem in problems:
            r.add(ERR, problem)
        _, broken = ts.journal_entries(root, slug)
        for name in broken:
            r.add(ERR, f"{slug}: journal entry name or content cannot be parsed: journal/{name} "
                       f"(expected a complete entry and matching filename/session/at)")
        if problems:
            continue
        if meta.get("status") in ts.BINDABLE:
            bindable.append(slug)
            # The branch is a hint: multiple tasks in one tree are now normal.
            if branch and meta.get("branch") and meta["branch"] != branch:
                r.add(WARN, f"{slug}: branch='{meta['branch']}', but the current branch is '{branch}' — "
                            f"the hint is stale; this is not an error")
    return bindable


def sessions(r, root):
    records, problems = ts.bindings(root)
    for problem in problems:
        r.add(ERR, f"binding cannot be read: {problem}")
    for sid, record, reason in gc_candidates(root):
        r.add(ERR, f"chat {sid} is bound to an ineligible task: {reason}")
    stale = {sid for sid, _, _ in gc_candidates(root)}
    by_task = {}
    for sid, record in records.items():
        if sid not in stale:            # the invalid binding was already reported above
            by_task.setdefault(record["slug"], []).append(sid)
    for slug in sorted(by_task):
        r.add(OK, f"{slug}: bound chats: {len(by_task[slug])} "
                  f"({', '.join(sorted(by_task[slug]))})")
    return records


def legacy(r, root):
    """Legacy ACTIVE and LOCK: not errors, but no longer state; sessions/ replaced them."""
    slug = ts.legacy_pointer(root)
    if slug:
        r.add(WARN, f"legacy .agents/state/ACTIVE='{slug}' remains — bind the chat "
                    f"(`agent-system task bind {slug}`), the pointer is removed afterward")
    if (ts.state_dir(root) / "LOCK").exists():
        r.add(WARN, "legacy .agents/state/LOCK remains — check its owner and stop the old session before removing it manually")


def current(r, root, resolution):
    if resolution.kind == "no-session":
        r.add(WARN, "session ID is unavailable — working without a binding. Set "
                    "AGENTS_SESSION_ID or bind the chat explicitly")
    elif resolution.kind == "bound":
        r.add(OK, f"chat {resolution.sid} is bound to task: {resolution.slug}")
    elif resolution.kind == "invalid":
        for problem in resolution.problems:
            r.add(ERR, f"binding for chat {resolution.sid} is invalid: {problem}. "
                       f"Do NOT silently load it: restart discovery")
    elif resolution.kind == "suggest":
        r.add(WARN, f"chat is unbound; the only candidate is — {resolution.slug}. "
                    f"No binding is created automatically")
    elif resolution.kind == "ambiguous":
        r.add(WARN, f"chat is unbound; multiple active tasks exist: "
                    f"{', '.join(resolution.candidates)}. NO automatic selection is made")
    else:
        r.add(WARN, "chat is unbound; no active tasks exist")


def model_settings(r, root):
    """Check independent chat records without treating them as task bindings."""
    try:
        names = state_fs.names(root, model_config.CHAT)
        project = state_fs.read(root, model_config.PROJECT)
        if project is None and not any(n != '.locks' for n in names):
            return
        roles = model_config.roles(root)
        model_config.load_project(root, roles)
        for name in names:
            if name == '.locks':
                continue
            try:
                if not name.endswith('.json'):
                    raise model_config.ConfigError('expected <session-id>.json')
                model_config.load_chat(root, name[:-5], roles)
            except (ValueError, OSError) as exc:
                r.add(ERR, f'{model_config.CHAT / name}: {exc}')
    except (ValueError, OSError) as exc:
        r.add(ERR, f'model configuration: {exc}')


def main(argv=None):
    global ROOT, MEMORY
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--resolve", action="store_true",
                    help="show what the current chat sees (also shown by default)")
    ap.add_argument("--project", type=Path, default=ROOT)
    ap.add_argument("--session-id", help="check on behalf of a specific chat")
    args = ap.parse_args(argv)
    ROOT = args.project.resolve()
    MEMORY = ROOT / ".agents" / "memory"

    r = Report()
    branch = git_branch()
    print(f"branch: {branch or '(detached HEAD or not a Git repository)'}")

    try:
        sid = args.session_id if args.session_id is not None else ts.session_id()
        if args.session_id is not None:
            ts.validate_session(sid)
    except ts.StateError as e:
        print()
        r.add(ERR, f"session ID rejected: {e}")
        memory(r)
        return r.dump()
    print(f"chat:  {sid or '(session ID unavailable)'}")
    print()

    try:
        slugs = ts.tasks(ROOT)
        bindings, binding_errors = ts.bindings(ROOT)
        if not slugs and not bindings and not binding_errors and not ts.legacy_pointer(ROOT):
            r.add(OK, "no tasks or bindings — clean state")
        tasks(r, ROOT, branch)
        sessions(r, ROOT)
        legacy(r, ROOT)
        current(r, ROOT, ts.resolve(ROOT, sid))
    except (ts.StateError, OSError) as e:
        r.add(ERR, str(e))
    model_settings(r, ROOT)
    memory(r)
    return r.dump()


if __name__ == "__main__":
    sys.exit(main())
