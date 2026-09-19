"""Local title lookup and grouped task output, using isolated harness stores."""
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import chat_titles
import cli
import task_state as ts


class ChatTitlesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.codex = self.root / 'codex'
        self.claude = self.root / 'claude'
        self.codex.mkdir()
        self.claude.mkdir()
        self.env = {'CODEX_HOME': str(self.codex), 'CLAUDE_CONFIG_DIR': str(self.claude)}

    def test_codex_database_rename_is_reflected_without_writes(self):
        db = self.codex / 'state_5.sqlite'
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute('CREATE TABLE threads (id TEXT, title TEXT, name TEXT)')
            conn.execute('INSERT INTO threads VALUES (?, ?, ?)', ('c1', 'Generated title', 'UI name'))
        records = {'c1': {'harness': 'codex'}}
        self.assertEqual(chat_titles.lookup(self.root, records, self.env)[('codex', 'c1')], 'UI name')
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute('UPDATE threads SET name = ?', ('Renamed',))
        before = db.read_bytes()
        self.assertEqual(chat_titles.lookup(self.root, records, self.env)[('codex', 'c1')], 'Renamed')
        self.assertEqual(db.read_bytes(), before)

    def test_codex_index_fallback_and_missing_title(self):
        (self.codex / 'session_index.jsonl').write_text(
            json.dumps({'id': 'c1', 'thread_name': 'Old'}) + '\ninvalid\n' +
            json.dumps({'id': 'c1', 'thread_name': 'New'}) + '\n')
        (self.codex / 'state_5.sqlite').write_text('unreadable database')
        titles = chat_titles.lookup(self.root, {'c1': {'harness': 'codex'}, 'c2': {'harness': 'codex'}}, self.env)
        self.assertEqual(titles[('codex', 'c1')], 'New')
        self.assertNotIn(('codex', 'c2'), titles)

    def test_grouped_task_output_and_fallback(self):
        template = (Path(__file__).resolve().parents[2] / 'templates/task.md').read_text()
        ts.create_task(self.root, 'task-a', template, branch='main')
        for sid, harness in [('c1', 'codex'), ('c2', 'codex'), ('a1', 'claude')]:
            ts.bind(self.root, sid, 'task-a', harness_name=harness)
        ts.write_entry(self.root, 'task-a', 'c1', 'One checkpoint.', stage='test')
        out = io.StringIO()
        with patch('cli.chat_titles.lookup', return_value={('codex', 'c1'): 'Named chat'}), redirect_stdout(out):
            self.assertEqual(cli.task_list(self.root), 0)
        text = out.getvalue()
        self.assertIn('branch=main, entries: 1\n', text)
        self.assertIn('    codex:\n        chats:\n            Named chat: c1\n', text)
        self.assertIn('            Untitled: c2\n', text)
        self.assertIn('    claude:\n        chats:\n            Untitled: a1\n', text)

    def test_claude_titles_follow_rename_and_ignore_forked_history(self):
        project = self.claude / 'projects' / 'any-project'
        project.mkdir(parents=True)
        transcript = project / 'a1.jsonl'
        events = [
            {'type': 'ai-title', 'sessionId': 'a1', 'aiTitle': 'Automatic'},
            {'type': 'custom-title', 'sessionId': 'a1', 'customTitle': 'Old'},
            {'type': 'custom-title', 'sessionId': 'a1', 'customTitle': 'New'},
            {'type': 'custom-title', 'sessionId': 'another-chat', 'customTitle': 'Wrong'},
        ]
        transcript.write_text(''.join(json.dumps(e) + '\n' for e in events))
        records = {'a1': {'harness': 'claude'}}
        self.assertEqual(chat_titles.lookup(self.root, records, self.env)[('claude', 'a1')], 'New')
        with transcript.open('a') as stream:
            stream.write(json.dumps({'type': 'custom-title', 'sessionId': 'a1', 'customTitle': ''}) + '\n')
        self.assertEqual(chat_titles.lookup(self.root, records, self.env)[('claude', 'a1')], 'Automatic')

    def test_claude_sidecar_and_terminal_safe_names(self):
        folder = self.claude / 'projects' / 'a-project' / 'a1'
        folder.mkdir(parents=True)
        (folder / 'custom-title.json').write_text(json.dumps({'customTitle': 'A\nB\x1b[31m'}))
        titles = chat_titles.lookup(self.root, {'a1': {'harness': 'claude'}}, self.env)
        self.assertEqual(titles[('claude', 'a1')], 'A B [31m')

    def test_missing_stores_unknown_harness_and_no_bindings(self):
        self.assertEqual(chat_titles.lookup(self.root, {}, self.env), {})
        self.assertEqual(chat_titles.lookup(self.root, {'a1': {'harness': 'unknown'}}, self.env), {})
        self.assertEqual(chat_titles.lookup(self.root, {'a1': {'harness': 'claude'}}, self.env), {})

    def test_claude_tail_then_sidecar_then_earlier_title(self):
        project = self.claude / 'projects' / 'test-project'
        folder = project / 'a1'
        folder.mkdir(parents=True)
        transcript = project / 'a1.jsonl'
        old = {'type': 'custom-title', 'sessionId': 'a1', 'customTitle': 'Old'}
        transcript.write_text(json.dumps(old) + '\n' + json.dumps({'message': 'x' * 70000}) + '\n')
        sidecar = folder / 'custom-title.json'
        sidecar.write_text(json.dumps({'customTitle': 'Current'}))
        records = {'a1': {'harness': 'claude'}}
        self.assertEqual(chat_titles.lookup(self.root, records, self.env)[('claude', 'a1')], 'Current')
        with transcript.open('a') as stream:
            stream.write(json.dumps({**old, 'customTitle': 'Newest'}) + '\n')
        self.assertEqual(chat_titles.lookup(self.root, records, self.env)[('claude', 'a1')], 'Newest')


if __name__ == '__main__':
    unittest.main()
