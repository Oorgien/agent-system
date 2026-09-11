#!/usr/bin/env python3
"""Тесты валидатора состояния задач.

    python3 -m unittest discover tools/tests -v

Каждый тест собирает временное дерево и запускает копию скрипта в нём: check_state.py
выводит ROOT из собственного пути, поэтому иначе он проверял бы настоящий репозиторий.
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
        # Идентичность настоящего чата не должна протекать в тест.
        env = {k: v for k, v in os.environ.items()
               if k not in ("AGENTS_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
                            "CODEX_THREAD_ID", "CODEX_SESSION_ID")}
        if session:
            env["AGENTS_SESSION_ID"] = session
        return subprocess.run(
            [sys.executable, str(self.tmp / "tools" / "check_state.py"), *args],
            cwd=self.tmp, capture_output=True, text=True, env=env)

    def test_binding_to_a_missing_task_is_an_error(self):
        """Регрессия смысла: пустой каталог задач сам по себе не «чистое состояние».

        Привязка переживает и удаление задачи, и переключение ветки, поэтому
        разбирать её нужно раньше, чем объявлять состояние чистым.
        """
        self.bind("sid-1", "missing-task")
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("missing-task", r.stdout)

    def test_no_tasks_and_no_bindings_is_clean(self):
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("чистое состояние", r.stdout)

    def test_binding_to_a_finished_task_is_an_error(self):
        self.task("finished", status="done")
        self.bind("sid-1", "finished")
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("sid-1", r.stdout)

    def test_id_mismatch_and_bad_status_are_errors(self):
        self.task("task-a")
        (self.tasks / "task-a" / "task.md").write_text(
            TASK.format(slug="other", status="почти", branch="main"), encoding="utf-8")
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("не совпадает с именем каталога", r.stdout)
        self.assertIn("недопустимое значение", r.stdout)

    def test_gitkeep_markers_are_not_tasks_or_records(self):
        (self.tasks / ".gitkeep").touch()
        task = self.task("good")
        (task / "journal/.gitkeep").touch()
        self.bind("sid-good", "good")
        (self.tmp / ".agents/state/sessions/.gitkeep").touch()
        out = self.run_check(session="sid-good")
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("привязан к задаче: good", out.stdout)

    def test_corrupt_task_utf8_does_not_hide_valid_binding(self):
        bad = self.task("bad")
        (bad / "task.md").write_bytes(b"\xff")
        self.task("good")
        self.bind("sid-good", "good")
        out = self.run_check(session="sid-good")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("bad", out.stdout)
        self.assertIn("UTF-8", out.stdout)
        self.assertIn("привязан к задаче: good", out.stdout)

    def test_unparsable_journal_entry_name_is_an_error(self):
        d = self.task("task-a")
        (d / "journal" / "notes.md").write_text("x", encoding="utf-8")
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("не парсится", r.stdout)

    def test_valid_journal_entry_name_passes(self):
        d = self.task("task-a")
        (d / "journal" / "20260910T142233Z-019a3f7c.md").write_text(
            "---\nsession: 019a3f7c\nat: 2026-09-10T14:22:33Z\n---\n\nтекст\n",
            encoding="utf-8")
        (d / "journal" / "20260910T142233Z-019a3f7c-2.md").write_text("---\nsession: 019a3f7c\nat: 2026-09-10T14:22:33Z\n---\n\nsecond\n", encoding="utf-8")
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_branch_mismatch_is_only_a_warning(self):
        """Несколько задач в одном рабочем дереве — норма, ветка лишь подсказка."""
        self.git("init", "-q", "-b", "main", ".")
        (self.tmp / "app.txt").write_text("x", encoding="utf-8")
        self.git("add", "app.txt"), self.git("commit", "-qm", "initial")
        self.task("task-a", branch="feature-x")
        self.bind("sid-1", "task-a")
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("подсказка устарела", r.stdout)

    def test_several_bindings_to_one_task_are_normal(self):
        self.task("task-a")
        self.bind("sid-1", "task-a")
        self.bind("sid-2", "task-a")
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("привязанных чатов — 2", r.stdout)

    def test_unbound_session_is_never_bound_silently(self):
        self.task("task-a")
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("Привязка не делается автоматически", r.stdout)
        self.assertFalse((self.tmp / ".agents/state/sessions/sid-1").exists())

    def test_several_active_tasks_are_ambiguous_not_chosen(self):
        self.task("task-a"), self.task("task-b")
        r = self.run_check(session="sid-1")
        self.assertIn("Автоматический выбор НЕ делается", r.stdout)

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
        self.assertIn("session id не принят", r.stdout)

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
        (d / "journal.md").write_text("# старый журнал\n", encoding="utf-8")
        r = self.run_check(session="sid-1")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_missing_memory_symlink_warns_but_does_not_fail(self):
        """Память gitignored: в CI и свежем клоне её законно нет."""
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn(".agents/memory", r.stdout)

    def git(self, *args):
        subprocess.run(["git", "-c", "user.email=t@l", "-c", "user.name=t", *args],
                       cwd=self.tmp, capture_output=True, text=True, check=True)

    def saved_memory(self, key="proj", store_name="store"):
        """Готовит git-репозиторий с сохранёнными ключом и хранилищем."""
        self.git("init", "-q", "-b", "main", ".")
        store = self.tmp / store_name
        (store / key).mkdir(parents=True)
        self.git("-C", str(store / key), "init", "-q")
        self.git("config", "--local", "agents.memoryKey", key)
        self.git("config", "--local", "agents.memoryStore", str(store))
        return store

    def test_relative_link_resolves_against_its_own_directory(self):
        """Регрессия: цель относительной ссылки разрешается от каталога ссылки.

        Разрешение от текущего каталога процесса дало бы здесь <tmp>/../store/proj
        и объявило бы расхождением корректно настроенное дерево.
        """
        self.saved_memory()
        (self.tmp / ".agents" / "memory").symlink_to(Path("..") / "store" / "proj")
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_link_to_another_directory_is_an_error(self):
        """Дерево, в котором не повторили setup после смены ключа."""
        store = self.saved_memory()
        (store / "stale").mkdir()
        (self.tmp / ".agents" / "memory").symlink_to(store / "stale")
        r = self.run_check()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("не ту память", r.stdout)

    def test_correspondence_is_skipped_without_saved_settings(self):
        """Свежий клон: config не переносится, сверять не с чем — но и не ошибка."""
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
        self.assertIn("битый симлинк", r.stdout)
        self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
