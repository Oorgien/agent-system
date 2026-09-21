"""Per-role project defaults and per-chat launch advice; never launches agents.

Model identifiers are intentionally not a catalog. Runtime availability and the
actual application of these parameters remain the orchestrator's responsibility.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import tomllib

import state_fs as fs
from task_state import validate_session

ConfigError = fs.StateError
PROJECT = Path('.agents/config.toml')
CHAT = Path('.agents/state/chat-config')
EFFORTS = frozenset(('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra', 'inherit'))
ROLE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]*')


def roles(root, *, allow_empty=False):
    """Discover canonical definitions without following directory or file links.

    Installers can allow an empty destination before adding bundled roles.
    Unsafe paths and invalid role definitions still fail in that mode.
    """
    root = Path(root)
    source = Path('.agents/agents')
    # Only this module's own source checkout may use root definitions. An
    # unrelated project's tools/installer.py does not identify a tool checkout.
    if not os.path.lexists(root / source) and root.resolve() == Path(__file__).resolve().parent.parent:
        source = Path('agents')
    found = []
    for name in fs.names(root, source):
        if not name.endswith('.md'):
            continue
        role = name[:-3]
        if not ROLE.fullmatch(role) or role == 'defaults':
            raise ConfigError(f'Invalid canonical role: {name}')
        fs.read(root, source / name)
        found.append(role)
    if not found and not allow_empty:
        raise ConfigError(f'No canonical agent roles found in {source}')
    return found


def _table(value, path, allowed):
    if not isinstance(value, dict):
        raise ConfigError(f'{path} must be a table')
    unknown = set(value) - set(allowed)
    if unknown:
        raise ConfigError(f'{path}: unknown keys: {", ".join(sorted(unknown))}')


def _value(key, value):
    if key == 'effort.claude':
        raise ConfigError('Claude effort is inherited from the session; use /effort')
    if key not in ('models.claude', 'models.codex', 'effort.codex'):
        raise ConfigError(f'Unknown config key: {key}')
    if not isinstance(value, str) or not value or not value.isprintable() or any(c.isspace() for c in value):
        raise ConfigError(f'{key} must be a nonempty printable identifier without whitespace')
    if key == 'effort.codex' and value not in EFFORTS:
        raise ConfigError(f'Unsupported Codex effort: {value}')


def _settings(value, path):
    _table(value, path, ('models', 'effort'))
    for group, entries in value.items():
        _table(entries, f'{path}.{group}', ('claude', 'codex'))
        for harness, setting in entries.items():
            _value(f'{group}.{harness}', setting)


def _validate(data, known_roles, chat=False):
    _table(data, 'config', ('agents', 'defaults') if chat else ('agents',))
    if 'defaults' in data:
        _settings(data['defaults'], 'defaults')
    agents = data.get('agents', {})
    _table(agents, 'agents', known_roles)
    for role, settings in agents.items():
        _settings(settings, f'agents.{role}')
    return data


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ConfigError(f'Duplicate config key: {key}')
        out[key] = value
    return out


def load_project(root, roles):
    raw = fs.read(root, PROJECT)
    if raw is None:
        return {}
    try:
        data = tomllib.loads(raw.decode('utf-8'))
        return _validate(data, roles)
    except (ValueError, UnicodeError) as exc:
        raise ConfigError(f'{Path(root) / PROJECT}: {exc}') from exc


def load_chat(root, sid, roles):
    validate_session(sid)
    path = CHAT / f'{sid}.json'
    raw = fs.read(root, path)
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs)
        return _validate(data, roles, chat=True)
    except (ValueError, UnicodeError) as exc:
        raise ConfigError(f'{Path(root) / path}: {exc}') from exc


def resolve(root, sid=None):
    known = roles(root)
    project = load_project(root, known)
    chat = load_chat(root, sid, known) if sid is not None else {}
    resolved = {}
    for role in known:
        settings = {'models': {}, 'effort': {'claude': {'value': 'inherit', 'source': 'session'}}}
        layers = (
            ('chat.role', chat.get('agents', {}).get(role, {})),
            ('chat.defaults', chat.get('defaults', {})),
            ('project.role', project.get('agents', {}).get(role, {})),
        )
        for group, harness in (('models', 'claude'), ('models', 'codex'), ('effort', 'codex')):
            selected = {'value': 'inherit', 'source': 'inherit'}
            for source, layer in layers:
                if harness in layer.get(group, {}):
                    selected = {'value': layer[group][harness], 'source': source}
                    break
            settings[group][harness] = selected
        resolved[role] = settings
    return {'application': 'advisory; orchestrator-applied', 'runtime_verified': False, 'session': sid, 'roles': resolved}


def _toml(data):
    # The schema consists solely of nested tables and validated string values.
    lines = []
    for role, settings in sorted(data.get('agents', {}).items()):
        for group, entries in sorted(settings.items()):
            if entries:
                lines.append(f'[agents.{role}.{group}]')
                lines.extend(f'{key} = {json.dumps(value, ensure_ascii=False)}' for key, value in sorted(entries.items()))
                lines.append('')
    return '\n'.join(lines).encode('utf-8')


def change(root, role, key, value=None, *, chat=False, sid=None, unset=False):
    """Update one field under a per-config lock; invalid input never writes state.

    Project writes normalize TOML formatting; all schema values are preserved.
    An explicit inherit value masks lower layers, while unset removes the override.
    """
    known = roles(root)
    if role not in known and not (chat and role == 'defaults'):
        raise ConfigError(f'Unknown role: {role}; defaults is allowed only with --chat')
    _value(key, 'inherit' if unset else value)
    if chat:
        if sid is None:
            raise ConfigError('Chat configuration requires a session ID')
        validate_session(sid)
    path = CHAT / f'{sid}.json' if chat else PROJECT
    lock_name = hashlib.sha256(sid.encode()).hexdigest() if chat else 'project'
    lock = CHAT / '.locks' / lock_name
    loader = (lambda: load_chat(root, sid, known)) if chat else (lambda: load_project(root, known))
    loader()  # Reject malformed files before creating lock directories.
    fs.check(root, lock)
    with fs.record_lock(root, lock):
        data = loader()  # Re-read after serialization with other writers.
        parent = data if role == 'defaults' else data.setdefault('agents', {})
        leaf = role
        group, harness = key.split('.')
        if unset:
            settings = parent.get(leaf, {})
            entries = settings.get(group, {})
            entries.pop(harness, None)
            if not entries:
                settings.pop(group, None)
            if not settings:
                parent.pop(leaf, None)
            if not data.get('agents'):
                data.pop('agents', None)
        else:
            parent.setdefault(leaf, {}).setdefault(group, {})[harness] = value
        _validate(data, known, chat)
        raw = (json.dumps(data, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8') if chat else _toml(data)
        fs.write(root, path, raw)
    return data
