#!/usr/bin/env python3
"""Task-state validator tests.

    python3 -m unittest discover tools/tests -v

Each test builds a temporary tree and runs a copy of the script there:
check_state.py derives ROOT from its own path, so it would otherwise inspect
the real repository.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
ROOT = TOOLS.parent

TASK = """---
id: {slug}
status: {status}
branch: {branch}
created: 2026-09-10
---

# {slug}
"""


class TestCheckState(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        shutil.copytree(TOOLS, self.tmp / "tools",
                        ignore=shutil.ignore_patterns("__pycache__", "tests"))
        self.tasks = self.tmp / ".agents" / "state" / "tasks"
        self.tasks.mkdir(parents=True)

    def task(self, slug, status="active", branch="main"):
        d = self.tasks / slug
        d.mkdir()
        (d / "task.md").write_text(TASK.format(slug=slug, status=status, branch=branch),
                                   encoding="utf-8")
        (d / "journal").mkdir()
        return d

    def bind(self, sid, slug):
        d = self.tmp / ".agents" / "state" / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        (d / sid).write_text(json.dumps({"slug": slug, "harness": "claude",
                                         "bound_at": "2026-09-10T14:22:33Z"}) + "\n",
                             encoding="utf-8")

    def legacy_active(self, slug):
        (self.tmp / ".agents" / "state" / "ACTIVE").write_text(slug, encoding="utf-8")

    def run_check(self, *args, session=None):
        # The real chat identity must not leak into the test.
        env = {k: v for k, v in os.environ.items()
               if k not in ("AGENTS_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
                            "CODEX_THREAD_ID", "CODEX_SESSION_ID")}
        if session:
            env["AGENTS_SESSION_ID"] = session
        return subprocess.run(
            [sys.executable, str(self.tmp / "tools" / "check_state.py"), *args],
            cwd=self.tmp, capture_output=True, text=True, env=env)

    def test_binding_to_a_missing_task_is_an_error(self):
        """Semantic regression: an empty task directory alone does not mean "clean state".

        A binding survives both task deletion and branch switches, so inspect it
        before declaring the state clean.
        """
        self.bind("sid-1", "missing-task")
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("missing-task", r.stdout)

    def test_no_tasks_and_no_bindings_is_clean(self):
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("clean state", r.stdout)

    def test_binding_to_a_finished_task_is_an_error(self):
        self.task("finished", status="done")
        self.bind("sid-1", "finished")
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("sid-1", r.stdout)

    def test_id_mismatch_and_bad_status_are_errors(self):
        self.task("task-a")
        (self.tasks / "task-a" / "task.md").write_text(
            TASK.format(slug="other", status="almost", branch="main"), encoding="utf-8")
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("does not match the directory name", r.stdout)
        self.assertIn("invalid value", r.stdout)

    def test_gitkeep_markers_are_not_tasks_or_records(self):
        (self.tasks / ".gitkeep").touch()
        task = self.task("good")
        (task / "journal/.gitkeep").touch()
        self.bind("sid-good", "good")
        (self.tmp / ".agents/state/sessions/.gitkeep").touch()
        out = self.run_check(session="sid-good")
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("is bound to task: good", out.stdout)

    def test_corrupt_task_utf8_does_not_hide_valid_binding(self):
        bad = self.task("bad")
        (bad / "task.md").write_bytes(b"\xff")
        self.task("good")
        self.bind("sid-good", "good")
        out = self.run_check(session="sid-good")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("bad", out.stdout)
        self.assertIn("UTF-8", out.stdout)
        self.assertIn("is bound to task: good", out.stdout)

    def test_unparsable_journal_entry_name_is_an_error(self):
        d = self.task("task-a")
        (d / "journal" / "notes.md").write_text("x", encoding="utf-8")
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("cannot be parsed", r.stdout)

    def test_valid_journal_entry_name_passes(self):
        d = self.task("task-a")
        (d / "journal" / "20260910T142233Z-019a3f7c.md").write_text(
            "---\nsession: 019a3f7c\nat: 2026-09-10T14:22:33Z\n---\n\ntext\n",
            encoding="utf-8")
        (d / "journal" / "20260910T142233Z-019a3f7c-2.md").write_text("---\nsession: 019a3f7c\nat: 2026-09-10T14:22:33Z\n---\n\nsecond\n", encoding="utf-8")
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_branch_mismatch_is_only_a_warning(self):
        """Multiple tasks in one worktree are normal; the branch is only a hint."""
        self.git("init", "-q", "-b", "main", ".")
        (self.tmp / "app.txt").write_text("x", encoding="utf-8")
        self.git("add", "app.txt"), self.git("commit", "-qm", "initial")
        self.task("task-a", branch="feature-x")
        self.bind("sid-1", "task-a")
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("the hint is stale", r.stdout)

    def test_several_bindings_to_one_task_are_normal(self):
        self.task("task-a")
        self.bind("sid-1", "task-a")
        self.bind("sid-2", "task-a")
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("bound chats: 2", r.stdout)

    def test_unbound_session_is_never_bound_silently(self):
        self.task("task-a")
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("No binding is created automatically", r.stdout)
        self.assertFalse((self.tmp / ".agents/state/sessions/sid-1").exists())

    def test_several_active_tasks_are_ambiguous_not_chosen(self):
        self.task("task-a"), self.task("task-b")
        r = self.run_check(session="sid-1")
        self.assertIn("NO automatic selection is made", r.stdout)

    def test_unreadable_binding_is_an_error(self):
        self.task("task-a")
        d = self.tmp / ".agents/state/sessions"
        d.mkdir(parents=True)
        (d / "sid-1").write_text("not json", encoding="utf-8")
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 1, r.stdout)

    def test_invalid_session_id_is_refused_not_sanitized(self):
        r = self.run_check(session="../escape")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("session ID rejected", r.stdout)

    def test_legacy_active_is_offered_for_migration(self):
        self.task("task-a")
        self.legacy_active("task-a")
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("task bind task-a", r.stdout)

    def test_legacy_lock_is_reported_as_removable(self):
        self.task("task-a")
        (self.tmp / ".agents/state/LOCK").write_text("claude", encoding="utf-8")
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("LOCK", r.stdout)

    def test_legacy_journal_file_is_still_valid(self):
        d = self.task("task-a")
        shutil.rmtree(d / "journal")
        (d / "journal.md").write_text("# legacy journal\n", encoding="utf-8")
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_missing_memory_symlink_warns_but_does_not_fail(self):
        """Memory is gitignored: its absence is valid in CI and fresh clones."""
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn(".agents/memory", r.stdout)

    def git(self, *args):
        subprocess.run(["git", "-c", "user.email=t@l", "-c", "user.name=t", *args],
                       cwd=self.tmp, capture_output=True, text=True, check=True)

    def saved_memory(self, key="proj", store_name="store"):
        """Prepare a Git repository with a saved memory key and store."""
        self.git("init", "-q", "-b", "main", ".")
        store = self.tmp / store_name
        (store / key).mkdir(parents=True)
        self.git("-C", str(store / key), "init", "-q")
        self.git("config", "--local", "agents.memoryKey", key)
        self.git("config", "--local", "agents.memoryStore", str(store))
        return store

    def test_relative_link_resolves_against_its_own_directory(self):
        """Regression: resolve relative symlink targets from the link's directory.

        Resolving from the process working directory would produce <tmp>/../store/proj
        and report a mismatch in a correctly configured tree.
        """
        self.saved_memory()
        (self.tmp / ".agents" / "memory").symlink_to(Path("..") / "store" / "proj")
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_link_to_another_directory_is_an_error(self):
        """A tree where setup was not rerun after changing the key."""
        store = self.saved_memory()
        (store / "stale").mkdir()
        (self.tmp / ".agents" / "memory").symlink_to(store / "stale")
        r = self.run_check()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("the wrong memory", r.stdout)

    def test_correspondence_is_skipped_without_saved_settings(self):
        """Fresh clone: config is not copied, so no comparison is possible; this is not an error."""
        self.git("init", "-q", "-b", "main", ".")
        target = self.tmp / "external-store/somewhere"
        target.mkdir(parents=True)
        self.git("-C", str(target), "init", "-q")
        (self.tmp / ".agents" / "memory").symlink_to(target)
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_memory_must_have_its_own_repository(self):
        store = self.saved_memory()
        shutil.rmtree(store / "proj/.git")
        (self.tmp / ".agents/memory").symlink_to(store / "proj")
        out = self.run_check()
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("own Git working tree", out.stdout)

    def test_shared_store_is_not_a_valid_memory_layout(self):
        store = self.saved_memory()
        self.git("-C", str(store), "init", "-q")
        (self.tmp / ".agents/memory").symlink_to(store / "proj")
        out = self.run_check()
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("Legacy shared Git memory store", out.stdout)

    def test_broken_memory_symlink_is_reported(self):
        (self.tmp / ".agents" / "memory").symlink_to(self.tmp / "nowhere")
        r = self.run_check()
        self.assertIn("broken symlink", r.stdout)
        self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
