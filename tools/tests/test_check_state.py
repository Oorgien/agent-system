#!/usr/bin/env python3
"""Тесты валидатора состояния задач.

    python3 -m unittest discover tools/tests -v

Каждый тест собирает временное дерево и запускает копию скрипта в нём: check_state.py
выводит ROOT из собственного пути, поэтому иначе он проверял бы настоящий репозиторий.
"""
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
        (d / "journal.md").write_text("# journal\n", encoding="utf-8")

    def active(self, slug):
        (self.tmp / ".agents" / "state" / "ACTIVE").write_text(slug, encoding="utf-8")

    def run_check(self, *args):
        return subprocess.run(
            [sys.executable, str(self.tmp / "tools" / "check_state.py"), *args],
            cwd=self.tmp, capture_output=True, text=True)

    def test_dangling_active_without_tasks_is_an_error(self):
        """Регрессия: пустой каталог задач объявлялся чистым состоянием.

        Проверка возвращалась до того, как ACTIVE вообще читался, поэтому
        ACTIVE='missing-task' после удаления задачи или смены ветки давал
        «задач нет — чистое состояние» и код возврата 0.
        """
        self.active("missing-task")
        r = self.run_check()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("missing-task", r.stdout)

    def test_no_tasks_and_no_active_is_clean(self):
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("чистое состояние", r.stdout)

    def test_dangling_active_with_other_tasks_is_an_error(self):
        self.task("real-task")
        self.active("missing-task")
        r = self.run_check()
        self.assertEqual(r.returncode, 1, r.stdout)

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
        target = self.tmp / "somewhere"
        target.mkdir()
        (self.tmp / ".agents" / "memory").symlink_to(target)
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_broken_memory_symlink_is_reported(self):
        (self.tmp / ".agents" / "memory").symlink_to(self.tmp / "nowhere")
        r = self.run_check()
        self.assertIn("битый симлинк", r.stdout)
        self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
