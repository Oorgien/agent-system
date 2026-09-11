#!/usr/bin/env python3
"""Тесты setup.sh: ключ проекта обязан быть общим для всех worktrees.

    python3 -m unittest discover tools/tests -v

Работают на временной копии setup.sh во временном репозитории; ни исходное дерево,
ни настоящее хранилище памяти не трогаются.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SETUP = ROOT / "setup.sh"

GIT = shutil.which("git")
BASH = shutil.which("bash")


def git(*args, cwd):
    return subprocess.run(
        [GIT, "-c", "user.email=t@local", "-c", "user.name=t", *args],
        cwd=cwd, capture_output=True, text=True, check=True)


@unittest.skipUnless(GIT and BASH, "нужны git и bash")
class TestProjectKey(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = self.tmp / "store"

    def make_repo(self, name):
        repo = self.tmp / name
        repo.mkdir()
        shutil.copy2(SETUP, repo / "setup.sh")
        shutil.copytree(ROOT / "tools", repo / "tools",
                        ignore=shutil.ignore_patterns("__pycache__"))
        git("init", "-q", "-b", "main", cwd=repo)
        git("add", "setup.sh", cwd=repo)
        git("commit", "-qm", "init", cwd=repo)
        return repo

    def run_setup(self, cwd, *args):
        env = dict(os.environ, AGENTS_MEMORY_STORE=str(self.store), HOME=str(self.tmp))
        r = subprocess.run([BASH, str(cwd / "setup.sh"), *args],
                           cwd=cwd, capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_worktrees_share_one_memory_directory(self):
        """Регрессия: ключ брался из basename текущего каталога.

        При штатной раскладке repo/ и ../repo-billing/ это давало разные каталоги
        памяти: знание, добытое в одном дереве, во втором не существовало, притом
        что симлинки были на месте и ошибки не возникало.
        """
        repo = self.make_repo("repo")
        wt = self.tmp / "repo-billing"
        git("worktree", "add", "-q", "-b", "billing", str(wt), cwd=repo)

        self.run_setup(repo)
        self.run_setup(wt)

        self.assertEqual(os.readlink(repo / ".agents" / "memory"),
                         os.readlink(wt / ".agents" / "memory"))
        self.assertEqual(Path(os.readlink(repo / ".agents" / "memory")).name, "repo-memory")

    def test_explicit_argument_wins(self):
        repo = self.make_repo("repo")
        self.run_setup(repo, "chosen-name")
        self.assertEqual(Path(os.readlink(repo / ".agents" / "memory")).name,
                         "chosen-name")

    def test_explicit_key_survives_rerun(self):
        repo = self.make_repo("repo")
        self.run_setup(repo, "custom-project")
        self.run_setup(repo)
        self.assertEqual(git("config", "--local", "--get", "agents.memoryKey",
                             cwd=repo).stdout.strip(), "custom-project")
        self.assertEqual((repo / ".agents/memory").resolve(),
                         (self.store / "custom-project").resolve())

    def test_saved_key_is_shared_with_new_worktree(self):
        repo = self.make_repo("repo")
        self.run_setup(repo, "custom-project")
        wt = self.tmp / "feature"
        git("worktree", "add", "-q", "-b", "feature", str(wt), cwd=repo)
        git("config", "extensions.worktreeConfig", "true", cwd=repo)
        git("config", "--worktree", "agents.memoryKey", "wrong-worktree-key", cwd=wt)
        self.run_setup(wt)
        self.assertEqual((wt / ".agents/memory").resolve(),
                         (self.store / "custom-project").resolve())
        self.run_setup(wt, "replacement")
        self.run_setup(repo)
        self.assertEqual((repo / ".agents/memory").resolve(),
                         (self.store / "replacement").resolve())

    def test_automatic_key_survives_repository_rename(self):
        repo = self.make_repo("repo")
        self.run_setup(repo)
        renamed = self.tmp / "renamed"
        repo.rename(renamed)
        self.run_setup(renamed)
        self.assertEqual((renamed / ".agents/memory").resolve(), (self.store / "repo-memory").resolve())

    def test_invalid_key_does_not_replace_saved_key(self):
        repo = self.make_repo("repo")
        self.run_setup(repo, "valid")
        r = subprocess.run([BASH, str(repo / "setup.sh"), "../outside"], cwd=repo,
                           capture_output=True, text=True,
                           env=dict(os.environ, AGENTS_MEMORY_STORE=str(self.store)))
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(git("config", "--local", "--get", "agents.memoryKey",
                             cwd=repo).stdout.strip(), "valid")
        self.assertEqual((repo / ".agents/memory").resolve(), (self.store / "valid").resolve())

    def run_check(self, cwd, with_env=True):
        """Валидатор внутри дерева. with_env=False — так и стартует сессия."""
        env = dict(os.environ, HOME=str(self.tmp))
        env.pop("AGENTS_MEMORY_STORE", None)
        if with_env:
            env["AGENTS_MEMORY_STORE"] = str(self.store)
        return subprocess.run([sys.executable, str(cwd / "tools" / "check_state.py")],
                              cwd=cwd, capture_output=True, text=True, env=env)

    def test_stale_worktree_link_is_detected_after_key_change(self):
        """То, ради чего проверка добавлена: смена ключа в одном дереве.

        Ссылка второго дерева остаётся на прежнем каталоге. Раньше оба дерева
        проходили проверку с кодом 0, читая при этом разную память.
        """
        repo = self.make_repo("repo")
        wt = self.tmp / "repo-billing"
        git("worktree", "add", "-q", "-b", "billing", str(wt), cwd=repo)
        shutil.copytree(ROOT / "tools", wt / "tools",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.run_setup(repo)
        self.run_setup(wt)
        self.assertEqual(self.run_check(wt).returncode, 0)

        self.run_setup(repo, "renamed-key")

        self.assertEqual(self.run_check(repo).returncode, 0)
        stale = self.run_check(wt)
        self.assertEqual(stale.returncode, 1, stale.stdout)
        self.assertIn("не ту память", stale.stdout)

        self.run_setup(wt)
        self.assertEqual(self.run_check(wt).returncode, 0)

    def test_custom_store_survives_setup_and_validator_without_env(self):
        """Нестандартное хранилище задаётся один раз и переменной больше не требует.

        Без сохранения хранилища повторный setup увёл бы память в стандартный
        каталог, а валидатор без переменной объявил бы расхождение на исправном
        дереве — ровно то, чего сохранённое состояние не допускает.
        """
        repo = self.make_repo("repo")
        self.run_setup(repo)
        linked = (repo / ".agents/memory").resolve()
        self.assertEqual(linked, (self.store / "repo-memory").resolve())

        # повторный запуск без переменной не переезжает в ~/.agents-memory
        env = dict(os.environ, HOME=str(self.tmp))
        env.pop("AGENTS_MEMORY_STORE", None)
        r = subprocess.run([BASH, str(repo / "setup.sh")], cwd=repo,
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((repo / ".agents/memory").resolve(), linked)
        self.assertFalse((self.tmp / ".agents-memory").exists())

        self.assertEqual(self.run_check(repo, with_env=False).returncode, 0)

    def test_non_git_directory_falls_back_and_warns(self):
        """Без git стабильного ключа взять неоткуда — об этом нужно сказать вслух."""
        plain = self.tmp / "plain"
        plain.mkdir()
        shutil.copy2(SETUP, plain / "setup.sh")
        out = self.run_setup(plain).stdout
        self.assertIn("ВНИМАНИЕ", out)
        self.assertEqual(Path(os.readlink(plain / ".agents" / "memory")).name, "plain-memory")

    def test_seed_commit_failure_does_not_block_memory_link(self):
        repo = self.make_repo("repo")
        env = dict(os.environ, AGENTS_MEMORY_STORE=str(self.store), HOME=str(self.tmp),
                   GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="commit.gpgsign", GIT_CONFIG_VALUE_0="true",
                   GIT_CONFIG_KEY_1="gpg.program", GIT_CONFIG_VALUE_1=str(self.tmp / "missing-gpg"))
        out = subprocess.run([BASH, str(repo / "setup.sh")], cwd=repo, env=env,
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("начальный коммит не создан", out.stdout)
        self.assertTrue((repo / ".agents/memory").is_symlink())
        self.assertTrue((self.store / "repo-memory/.git").is_dir())

    def test_existing_link_without_saved_settings_is_preserved(self):
        repo = self.make_repo("repo")
        target = self.tmp / "custom" / "old-key"
        target.mkdir(parents=True)
        (target / "fact.md").write_text("existing knowledge")
        (repo / ".agents").mkdir()
        (repo / ".agents/memory").symlink_to(target)
        env = dict(os.environ, HOME=str(self.tmp))
        env.pop("AGENTS_MEMORY_STORE", None)
        out = subprocess.run([BASH, str(repo / "setup.sh")], cwd=repo,
                             env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual((repo / ".agents/memory").resolve(), target.resolve())
        self.assertEqual((target / "fact.md").read_text(), "existing knowledge")
        self.assertTrue((target / ".git").is_dir())
        self.assertFalse((target.parent / ".git").exists())

    def test_projects_have_independent_git_histories(self):
        a, b = self.make_repo("a"), self.make_repo("b")
        self.run_setup(a)
        self.run_setup(b)
        self.assertFalse((self.store / ".git").exists())
        for name in ("a-memory", "b-memory"):
            target = self.store / name
            self.assertTrue((target / ".git").is_dir())
            self.assertEqual(Path(git("rev-parse", "--show-toplevel", cwd=target).stdout.strip()).resolve(), target.resolve())
        (self.store / "a-memory" / "fact.md").write_text("Only A")
        self.assertNotIn("fact.md", git("status", "--porcelain", cwd=self.store / "b-memory").stdout)

    def test_legacy_store_rejected_without_changes(self):
        repo = self.make_repo("repo")
        self.store.mkdir()
        git("init", "-q", cwd=self.store)
        (self.store / "old.md").write_text("legacy")
        before = (repo / ".git/config").read_bytes()
        r = subprocess.run([BASH, str(repo / "setup.sh")], cwd=repo,
                           capture_output=True, text=True,
                           env=dict(os.environ, AGENTS_MEMORY_STORE=str(self.store)))
        self.assertEqual(r.returncode, 1)
        self.assertEqual((repo / ".git/config").read_bytes(), before)
        self.assertFalse((repo / ".agents").exists())
        self.assertFalse((self.store / "repo-memory").exists())
        self.assertEqual((self.store / "old.md").read_text(), "legacy")
        self.assertTrue((self.store / ".git").is_dir())

    def test_is_idempotent(self):
        repo = self.make_repo("repo")
        self.run_setup(repo)
        out = self.run_setup(repo).stdout
        self.assertIn("уже на месте", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
