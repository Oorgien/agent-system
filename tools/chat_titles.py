"""Best-effort local UI titles. Read only; never copy chat messages into task state."""
import json
import os
from pathlib import Path
import re
import sqlite3
import unicodedata
from contextlib import closing


def display(value):
    """Keep terminal output on one line without control/format escape characters."""
    if not isinstance(value, str):
        return ''
    return ' '.join(''.join(' ' if unicodedata.category(c).startswith('C') else c
                            for c in value).split())


def _json_lines(path, tail_bytes=None):
    try:
        with path.open('rb') as stream:
            if tail_bytes is not None:
                size = stream.seek(0, os.SEEK_END)
                stream.seek(max(0, size - tail_bytes))
            for line in stream:
                # Title metadata only; do not decode conversation messages.
                if not any(key in line for key in (b'"thread_name"', b'"custom-title"', b'"ai-title"')):
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def _codex(home, ids):
    titles = {}
    try:
        databases = sorted((p for p in home.glob('state_*.sqlite')
                            if re.fullmatch(r'state_\d+\.sqlite', p.name)),
                           key=lambda p: int(p.stem.split('_')[1]), reverse=True)
    except OSError:
        databases = []
    for path in databases:
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.1)) as conn:
                columns = {r[1] for r in conn.execute('PRAGMA table_info(threads)')}
                # New schemas store UI name separately from first-message title.
                field = 'name' if 'name' in columns else 'title'
                for sid in sorted(ids - titles.keys()):
                    row = conn.execute(f'SELECT {field} FROM threads WHERE id = ?', (sid,)).fetchone()
                    if row and display(row[0]):
                        titles[sid] = display(row[0])
        except (OSError, sqlite3.Error):
            continue
        # The newest readable schema is authoritative; older DBs may contain stale names.
        break
    fallback = {}
    if ids - titles.keys():
        for record in _json_lines(home / 'session_index.jsonl'):
            sid = record.get('id')
            title = display(record.get('thread_name'))
            if isinstance(sid, str) and sid in ids and title:
                fallback[sid] = title
    return {**fallback, **titles}


def _claude(home, ids):
    titles = {}
    for sid in sorted(ids):
        try:
            paths = sorted((home / 'projects').glob(f'*/{sid}.jsonl'))
        except OSError:
            continue
        custom, automatic, tail_custom = None, '', None
        for path in paths:
            for record in _json_lines(path):
                if record.get('sessionId') != sid:
                    continue
                if record.get('type') == 'custom-title':
                    custom = display(record.get('customTitle'))
                elif record.get('type') == 'ai-title':
                    automatic = display(record.get('aiTitle'))
            # Claude's UI prioritizes a title in the last 64 KiB over the sidecar.
            for record in _json_lines(path, tail_bytes=65536):
                if record.get('sessionId') == sid and record.get('type') == 'custom-title':
                    tail_custom = display(record.get('customTitle'))
        if tail_custom is not None:
            custom = tail_custom
        else:
            try:
                for path in sorted((home / 'projects').glob(f'*/{sid}/custom-title.json')):
                    try:
                        data = json.loads(path.read_text(encoding='utf-8'))
                        if isinstance(data, dict):
                            custom = display(data.get('customTitle'))
                    except (OSError, ValueError):
                        continue
            except OSError:
                pass
        if custom or automatic:
            titles[sid] = custom or automatic
    return titles


def lookup(root, records, env=None):
    """Resolve only bound IDs; missing stores or names leave entries untitled."""
    env = os.environ if env is None else env
    homes = {'codex': Path(env.get('CODEX_HOME') or Path.home() / '.codex'),
             'claude': Path(env.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')}
    result = {}
    for harness, reader in (('codex', _codex), ('claude', _claude)):
        ids = {sid for sid, r in records.items() if r.get('harness') == harness
               and re.fullmatch(r'[A-Za-z0-9._-]{1,128}', sid) and sid not in ('.', '..')}
        if ids:
            result.update({(harness, sid): title for sid, title in reader(homes[harness], ids).items()})
    return result
