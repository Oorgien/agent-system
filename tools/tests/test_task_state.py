#!/usr/bin/env python3
"""Тесты слоя хранения и разрешения состояния задач.

    python3 -m unittest discover tools/tests -v

Главное, что здесь проверяется, — отсутствие гонки: два «чата» пишут чекпойнты
в одну задачу одновременно, имена не совпадают, порядок чтения стабилен.
"""
import json
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import task_state as ts

TASK = """---
id: {slug}
status: {status}
branch: {branch}
created: 2026-09-10
---

# {slug}
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        ts.tasks_dir(self.root).mkdir(parents=True)

    def task(self, slug, status="active", branch="main"):
        d = ts.task_dir(self.root, slug)
        d.mkdir(parents=True)
        (d / "task.md").write_text(TASK.format(slug=slug, status=status, branch=branch),
                                   encoding="utf-8")
        return d


class TestSessionIdentity(Base):
    def test_priority_order(self):
        env = {"CLAUDE_CODE_SESSION_ID": "claude-1", "CODEX_THREAD_ID": "codex-1"}
        self.assertEqual(ts.session_id(env), "claude-1")
        self.assertEqual(ts.session_id({"CODEX_THREAD_ID": "codex-1"}), "codex-1")
        self.assertEqual(ts.session_id({"AGENTS_SESSION_ID": "explicit",
                                        "CLAUDE_CODE_SESSION_ID": "claude-1"}), "explicit")

    def test_absent_identity_is_a_mode_not_an_error(self):
        self.assertIsNone(ts.session_id({}))
        self.assertIsNone(ts.session_id({"CLAUDE_CODE_SESSION_ID": ""}))

    def test_invalid_identity_is_refused_not_sanitized(self):
        """Подчищенный чужой id склеил бы два разных чата в одну привязку."""
        for bad in ("../escape", "a/b", "a b", "", "x" * 129, "sid\n",
                    ".", "..", ".locks", ".gitkeep", ".DS_Store", ".agents-" + "a" * 32):
            with self.assertRaises(ts.StateError):
                ts.validate_session(bad)
        with self.assertRaises(ts.StateError):
            ts.session_id({"CLAUDE_CODE_SESSION_ID": "../../etc/passwd"})

    def test_invalid_source_does_not_fall_through_to_the_next_one(self):
        """Иначе сессия молча работала бы под идентичностью другого харнесса."""
        with self.assertRaises(ts.StateError):
            ts.session_id({"AGENTS_SESSION_ID": "bad/id", "CLAUDE_CODE_SESSION_ID": "ok"})

    def test_harness_detection(self):
        self.assertEqual(ts.harness({"CLAUDECODE": "1"}), "claude")
        self.assertEqual(ts.harness({"CODEX_THREAD_ID": "t"}), "codex")
        self.assertEqual(ts.harness({}), "unknown")


class TestBinding(Base):
    def test_bind_is_idempotent(self):
        self.task("task-a")
        self.assertEqual(ts.bind(self.root, "sid-1", "task-a"), "created")
        self.assertEqual(ts.bind(self.root, "sid-1", "task-a"), "unchanged")

    def test_rebinding_requires_an_explicit_command(self):
        self.task("task-a"), self.task("task-b")
        ts.bind(self.root, "sid-1", "task-a")
        with self.assertRaises(ts.StateError):
            ts.bind(self.root, "sid-1", "task-b")
        self.assertEqual(ts.read_binding(self.root, "sid-1")["slug"], "task-a")
        self.assertEqual(ts.bind(self.root, "sid-1", "task-b", force=True), "rebound")
        self.assertEqual(ts.read_binding(self.root, "sid-1")["slug"], "task-b")

    def test_record_shape(self):
        self.task("task-a")
        ts.bind(self.root, "sid-1", "task-a", harness_name="claude")
        raw = (ts.sessions_dir(self.root) / "sid-1").read_text(encoding="utf-8")
        self.assertEqual(len(raw.strip().splitlines()), 1)
        record = json.loads(raw)
        self.assertEqual(record["slug"], "task-a")
        self.assertEqual(record["harness"], "claude")
        self.assertRegex(record["bound_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_reverse_index_is_directory_enumeration(self):
        self.task("task-a"), self.task("task-b")
        ts.bind(self.root, "sid-1", "task-a")
        ts.bind(self.root, "sid-2", "task-a")
        ts.bind(self.root, "sid-3", "task-b")
        records, problems = ts.bindings(self.root)
        self.assertEqual(problems, [])
        self.assertEqual({s for s, r in records.items() if r["slug"] == "task-a"},
                         {"sid-1", "sid-2"})

    def test_broken_binding_does_not_break_the_sweep(self):
        self.task("task-a")
        ts.bind(self.root, "sid-1", "task-a")
        (ts.sessions_dir(self.root) / "sid-2").write_text("not json", encoding="utf-8")
        records, problems = ts.bindings(self.root)
        self.assertEqual(list(records), ["sid-1"])
        self.assertEqual(len(problems), 1)

    def test_unbind(self):
        self.task("task-a")
        ts.bind(self.root, "sid-1", "task-a")
        self.assertTrue(ts.unbind(self.root, "sid-1"))
        self.assertFalse(ts.unbind(self.root, "sid-1"))


class TestJournal(Base):
    def moment(self, second=33):
        return datetime(2026, 9, 10, 14, 22, second, tzinfo=timezone.utc)

    def test_entry_name_carries_order_and_identity(self):
        name = ts.entry_name(self.moment(), "019a3f7c-1111-2222")
        self.assertEqual(name, "20260910T142233Z-97ad7ed94aea19c18593d891b75c9a5b-000001.md")

    def test_collision_within_one_second_of_one_session(self):
        self.task("task-a")
        a = ts.write_entry(self.root, "task-a", "019a3f7c", "first", moment=self.moment())
        b = ts.write_entry(self.root, "task-a", "019a3f7c", "second", moment=self.moment())
        self.assertNotEqual(a.name, b.name)
        self.assertEqual(b.name, "20260910T142233Z-28817f69995e67078924a719174afa2f-000002.md")
        self.assertIn("first", a.read_text(encoding="utf-8"))

    def test_ordinal_sorts_after_the_base_name(self):
        """Голая лексикографика ставит '-2' перед '.md': '-' < '.' в ASCII."""
        keys = [ts.entry_key("20260910T142233Z-0123456789abcdef0123456789abcdef-000001.md"),
                ts.entry_key("20260910T142233Z-0123456789abcdef0123456789abcdef-000002.md")]
        self.assertEqual(keys, sorted(keys))

    def test_entry_has_frontmatter(self):
        self.task("task-a")
        p = ts.write_entry(self.root, "task-a", "sid-1", "тело", stage="implementer",
                           moment=self.moment())
        text = p.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("session: sid-1\n", text)
        self.assertIn("at: 2026-09-10T14:22:33Z\n", text)
        self.assertIn("stage: implementer\n", text)
        self.assertTrue(text.endswith("тело\n"))

    def test_canonical_order_puts_legacy_journal_first(self):
        d = self.task("task-a")
        (d / "journal.md").write_text("# legacy\n", encoding="utf-8")
        ts.write_entry(self.root, "task-a", "sid-1", "новое", moment=self.moment())
        ordered, broken = ts.journal_entries(self.root, "task-a")
        self.assertEqual(broken, [])
        self.assertEqual([p.name for p in ordered],
                         ["journal.md", "20260910T142233Z-500350f230ef17d0de44182d1a0889f5-000001.md"])

    def test_unparsable_entry_is_reported_not_dropped(self):
        self.task("task-a")
        ts.write_entry(self.root, "task-a", "sid-1", "новое", moment=self.moment())
        (ts.journal_dir(self.root, "task-a") / "notes.md").write_text("x", encoding="utf-8")
        ordered, broken = ts.journal_entries(self.root, "task-a")
        self.assertEqual(broken, ["notes.md"])
        self.assertEqual([p.name for p in ordered][-1], "notes.md")

    def test_existing_entries_are_never_rewritten(self):
        self.task("task-a")
        first = ts.write_entry(self.root, "task-a", "sid-1", "первое", moment=self.moment())
        ts.write_entry(self.root, "task-a", "sid-1", "второе", moment=self.moment(40))
        self.assertEqual(first.read_text(encoding="utf-8").split("---\n")[2].strip(), "первое")


class TestConcurrentSessions(Base):
    """Два чата пишут в одну задачу одновременно — состояние обоих сохраняется."""

    def test_parallel_checkpoints_do_not_collide(self):
        self.task("task-a")
        moment = datetime(2026, 9, 10, 14, 22, 33, tzinfo=timezone.utc)
        sessions = [f"sid-{i:04d}" for i in range(12)]
        start, written, errors = threading.Barrier(len(sessions)), [], []

        def worker(sid):
            try:
                start.wait(timeout=10)
                for n in range(5):
                    written.append(ts.write_entry(self.root, "task-a", sid,
                                                  f"{sid}/{n}", moment=moment))
            except Exception as e:                        # noqa: BLE001 — репортим в тест
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(sid,)) for sid in sessions]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)

        self.assertEqual(errors, [])
        self.assertEqual(len(written), len(sessions) * 5)
        self.assertEqual(len({p.name for p in written}), len(written))
        ordered, broken = ts.journal_entries(self.root, "task-a")
        self.assertEqual(broken, [])
        self.assertEqual(len(ordered), len(written))
        # Порядок чтения детерминирован и не зависит от порядка записи.
        self.assertEqual([p.name for p in ordered],
                         [p.name for p in ts.journal_entries(self.root, "task-a")[0]])
        bodies = {p.read_text(encoding="utf-8").rsplit("---\n", 1)[1].strip() for p in ordered}
        self.assertEqual(len(bodies), len(written))

    def test_parallel_bind_of_different_sessions_to_one_task(self):
        self.task("task-a")
        errors = []

        def worker(sid):
            try:
                ts.bind(self.root, sid, "task-a", harness_name="claude")
            except Exception as e:                        # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(f"sid-{i}",)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        records, problems = ts.bindings(self.root)
        self.assertEqual((errors, problems), ([], []))
        self.assertEqual(len(records), 16)


class TestResolution(Base):
    def test_without_session_id_there_is_no_binding(self):
        self.task("task-a")
        r = ts.resolve(self.root, None)
        self.assertEqual((r.kind, r.slug), ("no-session", None))
        self.assertEqual(r.candidates, ["task-a"])

    def test_binding_wins_over_discovery(self):
        self.task("task-a"), self.task("task-b")
        ts.bind(self.root, "sid-1", "task-b")
        r = ts.resolve(self.root, "sid-1")
        self.assertEqual((r.kind, r.slug, r.problems), ("bound", "task-b", []))

    def test_branch_mismatch_is_not_a_binding_problem(self):
        """Несколько задач в одном дереве — норма, ветка лишь подсказка."""
        self.task("task-a", branch="other-branch")
        ts.bind(self.root, "sid-1", "task-a")
        self.assertEqual(ts.resolve(self.root, "sid-1").kind, "bound")

    def test_paused_task_stays_bindable(self):
        self.task("task-a", status="paused")
        ts.bind(self.root, "sid-1", "task-a")
        r = ts.resolve(self.root, "sid-1")
        self.assertEqual(r.kind, "bound")
        self.assertEqual(r.candidates, [])          # discovery предлагает только active

    def test_binding_to_a_finished_task_is_invalid(self):
        self.task("task-a")
        self.task("task-b")
        ts.bind(self.root, "sid-1", "task-a")
        ts.set_status(self.root, "task-a", "done")
        r = ts.resolve(self.root, "sid-1")
        self.assertEqual(r.kind, "invalid")
        self.assertTrue(r.problems)
        self.assertEqual(r.candidates, ["task-b"])   # уходим в discovery, но не молча

    def test_binding_to_a_missing_task_is_invalid(self):
        self.task("task-a")
        ts.bind(self.root, "sid-1", "task-a")
        shutil.rmtree(ts.task_dir(self.root, "task-a"))
        self.assertEqual(ts.resolve(self.root, "sid-1").kind, "invalid")

    def test_id_mismatch_is_invalid(self):
        d = self.task("task-a")
        ts.bind(self.root, "sid-1", "task-a")
        (d / "task.md").write_text(TASK.format(slug="other", status="active", branch="main"),
                                   encoding="utf-8")
        self.assertEqual(ts.resolve(self.root, "unbound").kind, "none")
        self.assertEqual(ts.resolve(self.root, "sid-1").kind, "invalid")

    def test_single_active_task_is_suggested_not_bound(self):
        self.task("task-a")
        r = ts.resolve(self.root, "sid-1")
        self.assertEqual((r.kind, r.slug), ("suggest", "task-a"))
        self.assertIsNone(ts.read_binding(self.root, "sid-1"))

    def test_several_active_tasks_are_ambiguous(self):
        self.task("task-a"), self.task("task-b")
        r = ts.resolve(self.root, "sid-1")
        self.assertEqual((r.kind, r.candidates), ("ambiguous", ["task-a", "task-b"]))

    def test_no_open_tasks(self):
        self.task("task-a", status="done")
        self.assertEqual(ts.resolve(self.root, "sid-1").kind, "none")

    def test_legacy_active_is_offered_as_a_candidate(self):
        self.task("task-a")
        (ts.state_dir(self.root) / "ACTIVE").write_text("task-a\n", encoding="utf-8")
        self.assertEqual(ts.resolve(self.root, "sid-1").legacy, "task-a")
        # Указатель на ДРУГУЮ задачу — ещё не перенесённая подсказка чужого чата.
        self.task("task-b")
        self.assertEqual(ts.drop_legacy(self.root, "task-b"), [])
        self.assertEqual(ts.resolve(self.root, "sid-1").legacy, "task-a")
        self.assertEqual(ts.drop_legacy(self.root, "task-a"), ["ACTIVE"])
        self.assertIsNone(ts.resolve(self.root, "sid-1").legacy)


class TestTaskFile(Base):
    def test_create_task_from_template(self):
        template = (TOOLS.parent / ".agents/state/templates/task.md").read_text(encoding="utf-8")
        ts.create_task(self.root, "task-new", template, branch="feature-x",
                       moment=datetime(2026, 9, 10, tzinfo=timezone.utc))
        meta, err = ts.task_meta(self.root, "task-new")
        self.assertIsNone(err)
        self.assertEqual(meta["id"], "task-new")
        self.assertEqual(meta["status"], "active")
        self.assertEqual(meta["branch"], "feature-x")
        self.assertEqual(meta["created"], "2026-09-10")
        self.assertTrue(ts.journal_dir(self.root, "task-new").is_dir())

    def test_create_task_does_not_clobber(self):
        self.task("task-a")
        with self.assertRaises(ts.StateError):
            ts.create_task(self.root, "task-a", "---\nid: <slug>\n---\n")

    def test_set_status_preserves_the_body(self):
        d = self.task("task-a")
        (d / "task.md").write_text(
            "---\nid: task-a\nstatus: active        # active | paused | done | abandoned\n"
            "branch: main\n---\n\n# Заголовок\n\nТекст.\n", encoding="utf-8")
        ts.set_status(self.root, "task-a", "done")
        meta, err = ts.task_meta(self.root, "task-a")
        self.assertIsNone(err)
        self.assertEqual(meta["status"], "done")
        self.assertIn("# Заголовок", (d / "task.md").read_text(encoding="utf-8"))

    def test_set_status_refuses_a_corrupt_task_contract(self):
        d = self.task("task-a")
        text = (d / "task.md").read_text().replace("id: task-a", "id: different")
        (d / "task.md").write_text(text)
        with self.assertRaises(ts.StateError):
            ts.set_status(self.root, "task-a", "done")
        self.assertEqual((d / "task.md").read_text(), text)

    def test_status_must_be_known(self):
        self.task("task-a")
        with self.assertRaises(ts.StateError):
            ts.set_status(self.root, "task-a", "почти-готово")

    def test_slug_cannot_escape_the_tasks_directory(self):
        for bad in ("../evil", "a/b", ".hidden", ""):
            with self.assertRaises(ts.StateError):
                ts.task_dir(self.root, bad)


if __name__ == "__main__":
    unittest.main()
