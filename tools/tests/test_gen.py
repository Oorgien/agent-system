#!/usr/bin/env python3
"""Тесты генератора и парсера. Стандартная библиотека, без зависимостей.

    python3 -m unittest discover tools/tests -v
"""
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import frontmatter                                    # noqa: E402
import gen_agents                                     # noqa: E402
from adapters import Inexpressible, RenderError       # noqa: E402
from adapters import claude as claude_adapter         # noqa: E402
from adapters import codex as codex_adapter           # noqa: E402


def agent(**kw):
    a = {
        "name": "probe",
        "description": "Probe agent used by tests.",
        "role": "explore",
        "models": {"claude": "sonnet", "codex": "gpt-5-codex"},
        "effort": "high",
        "capabilities": ["filesystem-read", "code-search"],
        "overrides": {},
        "body": "Do the thing. " * 10,
    }
    a.update(kw)
    return a


class TestFrontmatter(unittest.TestCase):
    def test_folded_block_joins_lines(self):
        d = frontmatter.parse("description: >\n  one two\n  three\n")
        self.assertEqual(d["description"], "one two three")

    def test_nested_map_and_list(self):
        d = frontmatter.parse("models:\n  claude: sonnet\n  codex: x\ncaps:\n  - a\n  - b\n")
        self.assertEqual(d["models"], {"claude": "sonnet", "codex": "x"})
        self.assertEqual(d["caps"], ["a", "b"])

    def test_empty_inline_map_is_a_map(self):
        """Регрессия: '{}' во вложенной карте разбиралось в строку."""
        d = frontmatter.parse("overrides:\n  claude: {}\n  codex: {}\n")
        self.assertEqual(d["overrides"], {"claude": {}, "codex": {}})

    def test_trailing_comment_stripped(self):
        d = frontmatter.parse("models:\n  codex: gpt-5  # VERIFY\n")
        self.assertEqual(d["models"]["codex"], "gpt-5")

    def test_rejects_tabs(self):
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.parse("key:\n\t- a\n")

    def test_rejects_inline_list(self):
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.parse("caps: [a, b]\n")

    def test_requires_closing_delimiter(self):
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.split("---\nname: x\n")


class TestClaudeAdapter(unittest.TestCase):
    def test_read_only_agent_gets_no_write_tools(self):
        text, _ = claude_adapter.render(agent())
        line = next(l for l in text.splitlines() if l.startswith("tools:"))
        for forbidden in ("Edit", "Write", "Bash"):
            self.assertNotIn(forbidden, line)

    def test_shell_without_write_is_inexpressible(self):
        """Главный инвариант: молчаливого расширения прав быть не должно."""
        with self.assertRaises(Inexpressible) as cm:
            claude_adapter.render(agent(capabilities=["filesystem-read", "shell"]))
        self.assertIn("невыразим", str(cm.exception))

    def test_vcs_without_write_is_also_inexpressible(self):
        with self.assertRaises(Inexpressible):
            claude_adapter.render(agent(capabilities=["filesystem-read", "vcs"]))

    def test_shell_with_write_is_fine(self):
        text, _ = claude_adapter.render(
            agent(capabilities=["filesystem-read", "filesystem-write", "shell"]))
        self.assertIn("Bash", text)

    def test_effort_is_carried_into_frontmatter(self):
        """Регрессия: адаптер утверждал, что носителя нет, и выбрасывал значение.

        Носитель есть — поле `effort` во frontmatter сабагента. Пока значение
        выбрасывалось, канон обещал усилие, а сессия наследовала своё.
        """
        text, warns = claude_adapter.render(agent(effort="low"))
        self.assertIn("\neffort: low\n", text)
        self.assertEqual(warns, [])

    def test_effort_unsupported_by_model_is_render_error(self):
        """Харнесс понизил бы уровень молча — ловим до записи файла."""
        claude_adapter.MODEL_EFFORT["probe-model"] = {"low", "medium"}
        try:
            with self.assertRaises(RenderError) as cm:
                claude_adapter.render(
                    agent(effort="high", models={"claude": "probe-model", "codex": "x"}))
            self.assertIn("не поддерживает", str(cm.exception))
        finally:
            del claude_adapter.MODEL_EFFORT["probe-model"]

    def test_unknown_model_warns_but_carries_effort(self):
        """Модель вне таблицы — неизвестность, а не несовместимость."""
        text, warns = claude_adapter.render(
            agent(effort="high", models={"claude": "some-future-model", "codex": "x"}))
        self.assertIn("\neffort: high\n", text)
        self.assertTrue(any("не описана" in w for w in warns))


class TestCodexAdapter(unittest.TestCase):
    def test_output_is_valid_toml(self):
        text, _ = codex_adapter.render(agent())
        tomllib.loads(text)

    def test_sandbox_follows_write_capability(self):
        ro, _ = codex_adapter.render(agent())
        rw, _ = codex_adapter.render(
            agent(capabilities=["filesystem-read", "filesystem-write"]))
        self.assertEqual(tomllib.loads(ro)["sandbox_mode"], "read-only")
        self.assertEqual(tomllib.loads(rw)["sandbox_mode"], "workspace-write")

    def test_shell_without_write_stays_read_only(self):
        """Расхождение с Claude: песочница ограничивает процесс, поэтому это выразимо."""
        text, _ = codex_adapter.render(agent(capabilities=["filesystem-read", "shell"]))
        self.assertEqual(tomllib.loads(text)["sandbox_mode"], "read-only")

    def test_effort_is_carried(self):
        text, _ = codex_adapter.render(agent(effort="medium"))
        self.assertEqual(tomllib.loads(text)["model_reasoning_effort"], "medium")

    def test_developer_instructions_is_the_schema_field(self):
        """Регрессия: писалось `instructions`, а обязательный ключ — другой.

        tomllib.loads() этого не ловил: он проверяет синтаксис TOML, а не схему
        агента, поэтому неправильное имя поля жило незамеченным.
        """
        data = tomllib.loads(codex_adapter.render(agent())[0])
        self.assertIn("developer_instructions", data)
        self.assertNotIn("instructions", data)

    def test_prompt_survives_serialization(self):
        """Экранирование — ответственность адаптера, а не автора промпта.

        Без него `\\bword\\b` после разбора превращался в управляющие символы,
        а `C:\\temp` — в табуляцию: текст менялся молча.
        """
        body = (
            'Use \\bword\\b to match. Path C:\\temp\\new stays literal. '
            'A """ fence and a trailing quote "'
        )
        text, _ = codex_adapter.render(agent(body=body))
        self.assertEqual(tomllib.loads(text)["developer_instructions"].strip(), body)

    def test_missing_required_key_is_render_error(self):
        broken = 'name = "probe"\ndescription = "d"\n'
        with self.assertRaises(RenderError) as cm:
            codex_adapter._check(broken, "probe", "d", "body")
        self.assertIn("developer_instructions", str(cm.exception))


class TestSchema(unittest.TestCase):
    def _write(self, d, text, name="probe.md"):
        p = d / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_rejects_missing_field(self):
        with tempfile.TemporaryDirectory() as t:
            p = self._write(Path(t), "---\nname: probe\n---\n\n" + "body " * 30)
            with self.assertRaises(gen_agents.SchemaError):
                gen_agents.load(p)

    def test_rejects_unknown_capability(self):
        with self.assertRaises(gen_agents.SchemaError):
            gen_agents.validate(agent(capabilities=["telepathy"]), Path("probe.md"))

    def test_rejects_name_not_matching_filename(self):
        with self.assertRaises(gen_agents.SchemaError):
            gen_agents.validate(agent(name="other"), Path("probe.md"))

    def test_rejects_partial_models(self):
        with self.assertRaises(gen_agents.SchemaError):
            gen_agents.validate(agent(models={"claude": "sonnet"}), Path("probe.md"))

    def test_rejects_unimplemented_override(self):
        with self.assertRaises(gen_agents.SchemaError) as cm:
            gen_agents.validate(agent(overrides={"claude": "x"}), Path("probe.md"))
        self.assertIn("не реализована", str(cm.exception))


class TestGeneratorEndToEnd(unittest.TestCase):
    def run_gen(self, *args):
        return subprocess.run([sys.executable, str(TOOLS / "gen_agents.py"), *args],
                              cwd=ROOT, capture_output=True, text=True)

    def test_check_passes_on_committed_state(self):
        r = self.run_gen("--check")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_generation_is_deterministic(self):
        files1, _ = gen_agents.build()
        files2, _ = gen_agents.build()
        self.assertEqual(files1, files2)

    def test_check_detects_drift(self):
        canon = ROOT / "agents" / "reviewer.md"
        backup = canon.read_text(encoding="utf-8")
        try:
            canon.write_text(backup.replace("effort: high", "effort: low"), encoding="utf-8")
            r = self.run_gen("--check")
            self.assertEqual(r.returncode, 1)
            self.assertIn("РАСХОЖДЕНИЕ", r.stderr)
        finally:
            canon.write_text(backup, encoding="utf-8")

    def test_orphan_cleanup_spares_unmarked_files(self):
        """Файл без нашего маркера мы не создавали и удалять не имеем права."""
        foreign = ROOT / ".claude" / "agents" / "handwritten.md"
        foreign.write_text("---\nname: handwritten\n---\n\nНе генератором создан.\n",
                           encoding="utf-8")
        try:
            files, _ = gen_agents.build()
            self.assertNotIn(foreign.relative_to(ROOT), gen_agents.orphans(set(files)))
        finally:
            foreign.unlink()

    def test_orphan_cleanup_finds_marked_files(self):
        stale = ROOT / ".claude" / "agents" / "removed.md"
        stale.write_text(f"---\nname: removed\n---\n\n<!-- {claude_adapter.MARKER} -->\n",
                         encoding="utf-8")
        try:
            files, _ = gen_agents.build()
            self.assertIn(stale.relative_to(ROOT), gen_agents.orphans(set(files)))
        finally:
            stale.unlink()


if __name__ == "__main__":
    unittest.main(verbosity=2)
