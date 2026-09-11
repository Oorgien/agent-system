"""State safety regressions. Every fixture and subprocess writes only temp roots."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))
import cli
import state_fs as fs
import task_state as ts

MOMENT = datetime(2026, 9, 10, 14, 22, 33, tzinfo=timezone.utc)
TEMPLATE = "---\nid: <slug>\nstatus: active\nbranch: <branch>\ncreated: <created>\n---\n\nTask body.\n"


def snapshot(root):
    result = {}
    for path in root.rglob("*"):
        key = str(path.relative_to(root))
        if path.is_symlink():
            result[key] = ("link", os.readlink(path))
        elif path.is_file():
            result[key] = ("file", path.read_bytes())
        else:
            result[key] = ("directory",)
    return result


def hold_session_lock(root, ready):
    with ts.session_lock(Path(root), "shared-session"):
        ready.send("locked")
        ready.close()
        # The test kills this process instead of unwinding the context manager.
        signal.pause()


class SafetyCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()

    def task(self, slug="alpha", root=None, status="active"):
        root = root or self.root
        directory = root / ".agents/state/tasks" / slug
        directory.mkdir(parents=True)
        text = TEMPLATE.replace("<slug>", slug).replace("<branch>", "main")
        text = text.replace("<created>", "2026-09-10").replace("status: active", f"status: {status}")
        (directory / "task.md").write_text(text, encoding="utf-8")
        return directory

    def launch(self, functions):
        results, errors = [], []

        def run(index, function):
            try:
                results.append((index, function()))
            except Exception as error:
                errors.append((index, error))

        threads = [threading.Thread(target=run, args=(index, fn), daemon=True)
                   for index, fn in enumerate(functions)]
        for thread in threads:
            thread.start()
        return threads, results, errors

    def joined(self, threads):
        deadline = time.monotonic() + 10
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        self.assertFalse(any(thread.is_alive() for thread in threads), "worker did not finish")

    def checkpoint(self, root=None, text="complete body"):
        return ts.write_entry(root or self.root, "alpha", "sid", text, moment=MOMENT)


class TestSymlinkSafety(SafetyCase):
    def test_state_parent_symlinks_reject_all_mutations(self):
        operations = {
            "checkpoint": lambda root: self.checkpoint(root),
            "bind": lambda root: ts.bind(root, "sid", "alpha"),
            "unbind": lambda root: ts.unbind(root, "sid"),
            "status": lambda root: ts.set_status(root, "alpha", "done"),
            "create": lambda root: ts.create_task(root, "new-task", TEMPLATE),
        }
        for operation, call in operations.items():
            parents = [".agents", ".agents/state"]
            parents += ([".agents/state/sessions"] if operation in ("bind", "unbind")
                        else [".agents/state/tasks"])
            if operation in ("checkpoint", "status"):
                parents.append(".agents/state/tasks/alpha")
            if operation == "checkpoint":
                parents.append(".agents/state/tasks/alpha/journal")
            for index, relative in enumerate(parents):
                with self.subTest(operation=operation, parent=relative):
                    root = self.base / f"{operation}-{index}"
                    root.mkdir()
                    directory = self.task(root=root)
                    (directory / "journal").mkdir()
                    ts.bind(root, "sid", "alpha")
                    source = root / relative
                    outside = self.base / f"outside-{operation}-{index}"
                    source.rename(outside)
                    source.symlink_to(outside, target_is_directory=True)
                    before = snapshot(outside)
                    with self.assertRaises(ts.StateError):
                        call(root)
                    self.assertTrue(source.is_symlink())
                    self.assertEqual(snapshot(outside), before)

    def test_canonical_leaf_symlinks_are_preserved_and_rejected(self):
        for operation in ("checkpoint", "bind", "unbind", "status", "create"):
            with self.subTest(operation=operation):
                root = self.base / operation
                root.mkdir()
                directory = self.task(root=root)
                outside = self.base / f"outside-{operation}"
                if operation == "create":
                    outside.mkdir()
                    (outside / "keep").write_text("untouched")
                    leaf = root / ".agents/state/tasks/new-task"
                    call = lambda: ts.create_task(root, "new-task", TEMPLATE)
                else:
                    outside.write_text((directory / "task.md").read_text(), encoding="utf-8")
                    if operation == "checkpoint":
                        leaf = directory / "journal" / ts.entry_name(MOMENT, "sid")
                        call = lambda: self.checkpoint(root)
                    elif operation == "status":
                        leaf = directory / "task.md"
                        leaf.unlink()
                        call = lambda: ts.set_status(root, "alpha", "done")
                    else:
                        leaf = root / ".agents/state/sessions/sid"
                        call = (lambda: ts.bind(root, "sid", "alpha")) if operation == "bind" else (lambda: ts.unbind(root, "sid"))
                leaf.parent.mkdir(parents=True, exist_ok=True)
                leaf.symlink_to(outside, target_is_directory=operation == "create")
                before = snapshot(outside) if outside.is_dir() else outside.read_bytes()
                with self.assertRaises(ts.StateError):
                    call()
                self.assertTrue(leaf.is_symlink())
                self.assertEqual(snapshot(outside) if outside.is_dir() else outside.read_bytes(), before)

    def test_create_rejects_task_file_symlink_inserted_after_reservation(self):
        outside = self.base / "outside.md"
        outside.write_text("must survive")
        real_write = ts.atomic_write

        def insert_symlink(root, path, data):
            path.symlink_to(outside)
            return real_write(root, path, data)

        with patch.object(ts, "atomic_write", side_effect=insert_symlink):
            with self.assertRaises(ts.StateError):
                ts.create_task(self.root, "alpha", TEMPLATE)
        self.assertEqual(outside.read_text(), "must survive")

    def test_publication_stays_in_opened_directory_after_parent_swap(self):
        directory = self.task() / "journal"
        directory.mkdir()
        pinned = directory.with_name("pinned-journal")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep").write_text("untouched")
        before = snapshot(outside)
        real_link = fs.os.link

        def swap_parent(source, destination, **kwargs):
            directory.rename(pinned)
            directory.symlink_to(outside, target_is_directory=True)
            return real_link(source, destination, **kwargs)

        with patch.object(fs.os, "link", side_effect=swap_parent):
            self.checkpoint()
        self.assertEqual(snapshot(outside), before)
        entries = list(pinned.iterdir())
        self.assertEqual(len(entries), 1)
        self.assertIn("complete body", entries[0].read_text())

    def test_replacement_stays_in_opened_directory_after_parent_swap(self):
        self.task()
        ts.bind(self.root, "sid", "alpha")
        directory = self.root / ".agents/state/sessions"
        pinned = directory.with_name("pinned-sessions")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "sid").write_text("untouched")
        before = snapshot(outside)
        real_replace = fs.os.replace

        def swap_parent(source, destination, **kwargs):
            directory.rename(pinned)
            directory.symlink_to(outside, target_is_directory=True)
            return real_replace(source, destination, **kwargs)

        self.task("beta")
        with patch.object(fs.os, "replace", side_effect=swap_parent):
            ts.bind(self.root, "sid", "beta", force=True)
        self.assertEqual(snapshot(outside), before)
        self.assertIn('"slug": "beta"', (pinned / "sid").read_text())


class TestAtomicPublication(SafetyCase):
    def test_file_fsync_failure_leaves_no_canonical_or_temporary_entry(self):
        self.task()
        with patch.object(fs.os, "fsync", side_effect=OSError("injected fsync failure")):
            with self.assertRaisesRegex(OSError, "injected fsync failure"):
                self.checkpoint()
        self.assertEqual(list(ts.journal_dir(self.root, "alpha").iterdir()), [])
        self.assertEqual(ts.read_journal(self.root, "alpha"), "")

    def test_partial_temporary_write_failure_never_publishes(self):
        self.task()
        real_fdopen = fs.os.fdopen

        class FailingWriter:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def write(self, data):
                self.stream.write(data[:12])
                self.stream.flush()
                raise OSError("injected partial write")

        def fdopen(handle, mode):
            stream = real_fdopen(handle, mode)
            return FailingWriter(stream) if mode == "wb" else stream

        with patch.object(fs.os, "fdopen", side_effect=fdopen):
            with self.assertRaisesRegex(OSError, "injected partial write"):
                self.checkpoint()
        self.assertEqual(list(ts.journal_dir(self.root, "alpha").iterdir()), [])
        self.assertEqual(ts.read_journal(self.root, "alpha"), "")

    def test_readers_see_nothing_until_complete_publication(self):
        self.task()
        ready, publish = threading.Event(), threading.Event()
        real_link = fs.os.link

        def delayed_link(*args, **kwargs):
            ready.set()
            if not publish.wait(timeout=5):
                raise TimeoutError("publication gate not released")
            return real_link(*args, **kwargs)

        with patch.object(fs.os, "link", side_effect=delayed_link):
            threads, results, errors = self.launch([self.checkpoint])
            try:
                self.assertTrue(ready.wait(timeout=5))
                self.assertEqual(ts.journal_entries(self.root, "alpha"), ([], []))
                self.assertEqual(ts.read_journal(self.root, "alpha"), "")
            finally:
                publish.set()
                self.joined(threads)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(len(ts.journal_entries(self.root, "alpha")[0]), 1)
        self.assertIn("\n\ncomplete body\n", ts.read_journal(self.root, "alpha"))

    def test_publication_collision_cannot_clobber_an_existing_entry(self):
        self.task()
        first = self.checkpoint(text="first body")
        original = first.read_bytes()
        second = self.checkpoint(text="second body")
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), original)
        self.assertIn("second body", second.read_text())

    def test_same_session_same_second_concurrent_checkpoints_remain_unique(self):
        self.task()
        count = 24
        start = threading.Barrier(count)

        def write(index):
            start.wait(timeout=5)
            return self.checkpoint(text=f"checkpoint-{index:02d}")

        threads, results, errors = self.launch([lambda i=i: write(i) for i in range(count)])
        self.joined(threads)
        self.assertEqual(errors, [])
        paths = [path for _, path in results]
        self.assertEqual(len(set(paths)), count)
        ordered, broken = ts.journal_entries(self.root, "alpha")
        self.assertEqual(broken, [])
        self.assertEqual(set(ordered), set(paths))
        self.assertEqual([ts.entry_key(path.name)[2] for path in ordered], list(range(1, count + 1)))
        bodies = {path.read_text().rsplit("---\n", 1)[1].strip() for path in ordered}
        self.assertEqual(bodies, {f"checkpoint-{index:02d}" for index in range(count)})


class TestConcurrentOwnership(SafetyCase):
    def test_simultaneous_create_has_one_winner_without_clobbering(self):
        ready = threading.Barrier(2)
        real_mkdir = fs.mkdir

        def reserve(root, relative, exclusive=False):
            if exclusive:
                ready.wait(timeout=5)
            return real_mkdir(root, relative, exclusive=exclusive)

        with patch.object(fs, "mkdir", side_effect=reserve):
            threads, results, errors = self.launch([
                lambda: ts.create_task(self.root, "alpha", TEMPLATE, branch="first"),
                lambda: ts.create_task(self.root, "alpha", TEMPLATE, branch="second"),
            ])
            self.joined(threads)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0][1], ts.StateError)
        winner = ("first", "second")[results[0][0]]
        task_file = self.root / ".agents/state/tasks/alpha/task.md"
        original = task_file.read_bytes()
        self.assertEqual(ts.task_meta(self.root, "alpha")[0]["branch"], winner)
        with self.assertRaises(ts.StateError):
            ts.create_task(self.root, "alpha", TEMPLATE, branch="late")
        self.assertEqual(task_file.read_bytes(), original)

    def test_simultaneous_different_task_binding_has_one_winner(self):
        self.task()
        self.task("beta")
        start = threading.Barrier(2)

        def bind(slug):
            start.wait(timeout=5)
            return ts.bind(self.root, "shared-session", slug)

        threads, results, errors = self.launch([lambda: bind("alpha"), lambda: bind("beta")])
        self.joined(threads)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][1], "created")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0][1], ts.StateError)
        winner = ("alpha", "beta")[results[0][0]]
        self.assertEqual(ts.read_binding(self.root, "shared-session")["slug"], winner)

    def test_simultaneous_same_task_binding_is_idempotent(self):
        self.task()
        start = threading.Barrier(2)

        def bind():
            start.wait(timeout=5)
            return ts.bind(self.root, "shared-session", "alpha")

        threads, results, errors = self.launch([bind, bind])
        self.joined(threads)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(value for _, value in results), ["created", "unchanged"])
        path = ts.binding_path(self.root, "shared-session")
        original = path.read_bytes()
        self.assertEqual(ts.bind(self.root, "shared-session", "alpha", harness_name="different"), "unchanged")
        self.assertEqual(path.read_bytes(), original)

    @unittest.skipUnless(hasattr(signal, "SIGKILL") and "fork" in multiprocessing.get_all_start_methods(), "requires POSIX process locks")
    def test_session_lock_is_released_by_kernel_after_process_crash(self):
        self.task()
        context = multiprocessing.get_context("fork")
        receiver, sender = context.Pipe(duplex=False)
        child = context.Process(target=hold_session_lock, args=(str(self.root), sender))
        child.start()
        sender.close()
        try:
            self.assertTrue(receiver.poll(5), "child did not acquire session lock")
            self.assertEqual(receiver.recv(), "locked")
            lock_name = hashlib.sha256(b"shared-session").hexdigest()
            lock_path = self.root / ".agents/state/sessions/.locks" / lock_name
            with lock_path.open("rb") as lock:
                with self.assertRaises(BlockingIOError):
                    fs.fcntl.flock(lock.fileno(), fs.fcntl.LOCK_EX | fs.fcntl.LOCK_NB)
            os.kill(child.pid, signal.SIGKILL)
            child.join(timeout=5)
            self.assertFalse(child.is_alive())
            script = "import sys; from pathlib import Path; import task_state as ts; print(ts.bind(Path(sys.argv[1]), 'shared-session', 'alpha'))"
            result = subprocess.run([sys.executable, "-B", "-c", script, str(self.root)],
                                    env=dict(os.environ, PYTHONPATH=str(TOOLS), PYTHONDONTWRITEBYTECODE="1"),
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "created")
            self.assertTrue(lock_path.is_file())
        finally:
            receiver.close()
            if child.is_alive():
                child.kill()
            child.join(timeout=5)
            child.close()


class TestJournalValidation(SafetyCase):
    def test_new_names_use_full_fixed_width_hash_and_numeric_ordinal(self):
        self.task()
        sid = "short-2"
        paths = [ts.write_entry(self.root, "alpha", sid, f"body {index}", moment=MOMENT)
                 for index in range(10)]
        digest = hashlib.sha256(sid.encode()).hexdigest()[:32]
        self.assertEqual(paths[0].name, f"20260910T142233Z-{digest}-000001.md")
        self.assertEqual(paths[-1].name, f"20260910T142233Z-{digest}-000010.md")
        self.assertTrue(all(ts.ENTRY_RE.fullmatch(path.name) for path in paths))
        self.assertEqual(ts.journal_entries(self.root, "alpha"), (paths, []))

    def test_distinct_sessions_with_same_legacy_prefix_have_distinct_hashes(self):
        self.task()
        sessions = ("abcdefgh-first", "abcdefgh-second")
        paths = [ts.write_entry(self.root, "alpha", sid, sid, moment=MOMENT) for sid in sessions]
        self.assertNotEqual(paths[0].name, paths[1].name)
        for sid, path in zip(sessions, paths):
            key = ts.entry_key(path.name)
            self.assertEqual(key[1], hashlib.sha256(sid.encode()).hexdigest()[:32])
            self.assertEqual(key[2], 1)
        ordered, broken = ts.journal_entries(self.root, "alpha")
        self.assertEqual(broken, [])
        self.assertEqual(set(ordered), set(paths))

    def test_legacy_short_and_suffix_like_session_ids_decode_from_metadata(self):
        for index, sid in enumerate(("s", "sid-2", "sid-10")):
            with self.subTest(session=sid):
                slug = f"task-{index}"
                directory = self.task(slug) / "journal"
                directory.mkdir()
                expected = []
                for ordinal in (1, 2, 10):
                    suffix = "" if ordinal == 1 else f"-{ordinal}"
                    path = directory / f"20260910T142233Z-{sid}{suffix}.md"
                    path.write_text(f"---\nsession: {sid}\nat: 2026-09-10T14:22:33Z\n---\n\nbody-{ordinal}\n")
                    expected.append(path)
                (directory.parent / "journal.md").write_text("legacy monolithic body\n")
                self.assertEqual(ts.journal_entries(self.root, slug), ([directory.parent / "journal.md"] + expected, []))
                text = ts.read_journal(self.root, slug)
                self.assertLess(text.index("legacy monolithic body"), text.index("body-1"))
                self.assertLess(text.index("body-1"), text.index("body-2"))
                self.assertLess(text.index("body-2"), text.index("body-10"))

    def test_whitespace_environment_identity_is_rejected_without_fallback(self):
        for variable in ts.SESSION_ENV:
            for invalid in (" ", "\t", "\n", " sid", "sid "):
                with self.subTest(variable=variable, value=repr(invalid)):
                    with self.assertRaises(ts.StateError):
                        ts.session_id({variable: invalid})
        with self.assertRaises(ts.StateError):
            ts.session_id({"AGENTS_SESSION_ID": " ", "CODEX_THREAD_ID": "valid"})

    def test_cli_stage_injection_is_rejected_before_creating_journal(self):
        directory = self.task()
        before = snapshot(self.root)
        for stage in ("review\nother: injected", "review#ignored", "review:injected", "review\rnext"):
            with self.subTest(stage=repr(stage)):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(cli, "resolve_root", return_value=self.root), redirect_stdout(stdout), redirect_stderr(stderr):
                    result = cli.main(["task", "checkpoint", "alpha", "--session-id", "sid", "--stage", stage, "--message", "body"])
                self.assertEqual(result, 2, stdout.getvalue() + stderr.getvalue())
                self.assertFalse((directory / "journal").exists())
                self.assertEqual(snapshot(self.root), before)

    def test_missing_and_finished_tasks_reject_checkpoints_without_writes(self):
        for slug, status in (("missing", None), ("finished", "done"), ("abandoned", "abandoned")):
            with self.subTest(slug=slug):
                if status:
                    self.task(slug, status=status)
                before = snapshot(self.root)
                with self.assertRaises(ts.StateError):
                    ts.write_entry(self.root, slug, "sid", "body", moment=MOMENT)
                self.assertEqual(snapshot(self.root), before)

    def test_corrupt_entry_metadata_is_reported_and_reader_fails(self):
        directory = self.task() / "journal"
        directory.mkdir()
        valid = "---\nsession: sid\nat: 2026-09-10T14:22:33Z\n---\n\nbody\n"
        mutations = {
            "missing-session": valid.replace("session: sid\n", ""),
            "invalid-session": valid.replace("session: sid", "session: ../outside"),
            "missing-time": valid.replace("at: 2026-09-10T14:22:33Z\n", ""),
            "invalid-time": valid.replace("2026-09-10", "2026-02-30"),
            "name-session-mismatch": valid.replace("session: sid", "session: other"),
            "name-time-mismatch": valid.replace("14:22:33Z", "14:22:34Z"),
            "duplicate-session": valid.replace("session: sid", "session: sid\nsession: other"),
            "invalid-stage": valid.replace("at: ", "stage: a:b\nat: "),
            "empty-body": valid.replace("body", ""),
            "missing-frontmatter": "body only\n",
        }
        path = directory / ts.entry_name(MOMENT, "sid")
        for description, content in mutations.items():
            with self.subTest(corruption=description):
                path.write_text(content)
                ordered, broken = ts.journal_entries(self.root, "alpha")
                self.assertEqual(ordered, [path])
                self.assertEqual(broken, [path.name])
                with self.assertRaises(ts.StateError):
                    ts.read_journal(self.root, "alpha")

    def test_corrupt_utf8_entry_is_not_silently_dropped(self):
        directory = self.task() / "journal"
        directory.mkdir()
        path = directory / ts.entry_name(MOMENT, "sid")
        path.write_bytes(b"\xff\xfe")
        self.assertEqual(ts.journal_entries(self.root, "alpha"), ([path], [path.name]))
        with self.assertRaises(ts.StateError):
            ts.read_journal(self.root, "alpha")

    def test_corrupt_task_metadata_blocks_journal_reader(self):
        directory = self.task()
        (directory / "task.md").write_text("---\nid: alpha\nstatus: invented\n---\nbody\n")
        with self.assertRaises(ts.StateError):
            ts.read_journal(self.root, "alpha")


if __name__ == "__main__":
    unittest.main()
