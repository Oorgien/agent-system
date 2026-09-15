"""Managed files and blocks. All conflicts are checked before external effects."""
import json
import re
import tomllib
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import gen_agents
from install_support import Conflict, config, digest, dump, git, relative, safe_path, snapshot, tree_files, tree_hashes
from install_memory import plan_memory, recheck_memory

MANIFEST = '.agents/agent-system.json'
CONTRACT = '.agents/agent-system/contract.md'
BEGIN = '<!-- agent-system:begin -->'
END = '<!-- agent-system:end -->'
GBEGIN = '# agent-system:begin'
GEND = '# agent-system:end'


def blocks():
    instruction = f'Read `{CONTRACT}` and follow its operational rules for this project.'
    return {
        'AGENTS.md': f'{BEGIN}\n{instruction}\n{END}\n'.encode(),
        'CLAUDE.md': f'{BEGIN}\n{instruction}\nAlso read `AGENTS.md` for the existing project rules.\n{END}\n'.encode(),
        '.gitignore': f'{GBEGIN}\n/.agents/memory\n/.agents/state/sessions/\n/.agents/state/ACTIVE\n/.agents/state/LOCK\n/.agents/runs.jsonl\n{GEND}\n'.encode(),
    }


def split_block(data, name):
    begin, end = (GBEGIN, GEND) if name == '.gitignore' else (BEGIN, END)
    begin, end = begin.encode(), end.encode()
    if data.count(begin) != 1 or data.count(end) != 1:
        raise Conflict(f'Missing or ambiguous managed block: {name}')
    start, finish = data.index(begin), data.index(end) + len(end)
    if finish < start:
        raise Conflict(f'Malformed block: {name}')
    if data[finish:finish+1] == b'\n':
        finish += 1
    return data[:start], data[start:finish], data[finish:]


def bundle(source):
    files = {}
    # Package sources live at the repo top level; .agents/ here is the agents' own
    # workspace and is never shipped. Installed layout keeps everything under .agents/.
    for directory, installed in [('agents', '.agents/agents'), ('templates', '.agents/state/templates')]:
        for name, data in tree_files(source / directory).items():
            files[f'{installed}/{name}'] = data
    for name, data in tree_files(source / 'skills').items():
        if '.DS_Store' not in Path(name).parts:
            files[f'.agents/skills/{name}'] = data
    generated, warnings = gen_agents.build(source / 'agents', source_dir=gen_agents.INSTALLED_CANON)
    files.update({str(p): t.encode() for p,t in generated.items()})
    files[CONTRACT] = (source / 'resources/contract.md').read_bytes()
    fp = digest(dump({n: digest(b) for n,b in sorted({**files, **blocks()}.items())}))
    return files, fp, warnings


def load_manifest(root):
    p = safe_path(root, MANIFEST)
    if p.is_symlink():
        raise Conflict('Manifest must not be a symlink')
    if not p.exists():
        return None
    try:
        m = json.loads(p.read_text())
        if m['schema'] != 1 or not isinstance(m['files'], dict) or not isinstance(m['blocks'], dict) or not isinstance(m['links'], dict):
            raise ValueError('unsupported schema')
        for name, record in m['files'].items():
            relative(name)
            if not (name.startswith(('.agents/agents/', '.agents/skills/', '.agents/state/templates/', '.claude/agents/', '.codex/agents/')) or name == CONTRACT):
                raise ValueError(f'unsupported managed path {name}')
            if not isinstance(record, str) or len(record) != 64:
                raise ValueError('invalid hash')
        if set(m['blocks']) != set(blocks()):
            raise ValueError('unexpected block paths')
        for name, target in m['links'].items():
            relative(name)
            if name != '.claude/skills' and not name.startswith('.claude/skills/'):
                raise ValueError('unexpected link path')
            if not isinstance(target, str):
                raise ValueError('invalid link target')
        return m
    except (KeyError, ValueError, TypeError) as e:
        raise Conflict(f'Invalid manifest: {e}') from e


def pending_path(root):
    return Path(git(root, 'rev-parse', '--absolute-git-dir')) / 'agent-system-pending.json'


def verify_owned(root, m):
    if m is None:
        return
    for name, sha in m['files'].items():
        p = safe_path(root, name)
        if snapshot(p) != {'kind': 'file', 'sha256': sha}:
            raise Conflict(f'Installed file locally changed or missing: {name}')
    for name, sha in m['blocks'].items():
        p = safe_path(root, name)
        if p.is_symlink() or not p.is_file():
            raise Conflict(f'Changed shared file: {name}')
        if digest(split_block(p.read_bytes(), name)[1]) != sha:
            raise Conflict(f'Installed block locally changed: {name}')
    for name, target in m['links'].items():
        p = safe_path(root, name)
        # Links may be absent in a new checkout. Init recreates only owned links.
        if snapshot(p) not in ({'kind': 'absent'}, {'kind': 'link', 'target': target}):
            raise Conflict(f'Installed link changed: {name}')


def make_plan(root, source, command, memory_key=None, memory_from=None):
    marker = pending_path(root)
    if marker.exists() or marker.is_symlink():
        raise Conflict(f'Interrupted installation: inspect {marker}; restore its backup before retrying')
    old = load_manifest(root)
    if command == 'update' and old is None:
        raise Conflict('Project is not initialized; use agent-system init')
    verify_owned(root, old)
    files, fingerprint, warnings = bundle(source)
    if command == 'init' and old and old.get('bundle') != fingerprint:
        raise Conflict('Installed bundle differs; use agent-system update')
    # Runtime identity comes from name metadata, not necessarily the filename.
    role_names = {Path(n).stem for n in files if n.startswith('.agents/agents/')}
    skill_names = {n.split('/')[2] for n in files if n.startswith('.agents/skills/')}
    for directory, pattern, wanted in [('.claude/agents', '*.md', role_names),
                                        ('.codex/agents', '*.toml', role_names),
                                        ('.agents/skills', '*/SKILL.md', skill_names),
                                        ('.claude/skills', '*/SKILL.md', skill_names)]:
        base = safe_path(root, directory)
        if base.is_symlink() and directory != '.claude/skills':
            raise Conflict(f'Symlink at install directory: {directory}')
        if directory == '.claude/skills' and base.is_symlink():
            continue  # checked below; canonical skill directory is checked separately
        for path in base.glob(pattern):
            rel = path.relative_to(root).as_posix()
            if old and (rel in old['files'] or
                        any(rel.startswith(n + '/') for n in old['links'])):
                continue
            if path.is_symlink() or path.parent.is_symlink():
                # Foreign links (skill managers install them) are never dereferenced:
                # the discovery name is the link name itself.
                name = path.parent.name if pattern.startswith('*/') else path.stem
            else:
                try:
                    text = path.read_text()
                    if path.suffix == '.toml':
                        name = tomllib.loads(text).get('name')
                    else:
                        fm = text.split('---', 2)
                        match = re.search(r'^name:\s*([^\n]+)', fm[1], re.M) if len(fm) == 3 else None
                        name = match.group(1).strip().strip("\"'") if match else None
                except (ValueError, UnicodeError):
                    continue  # unrelated invalid definitions are not ours to repair
            if name in wanted:
                raise Conflict(f'Existing runtime name {name!r}: {rel}')

    writes, removals, links, before = {}, [], {}, {}
    for name, data in files.items():
        p = safe_path(root, name)
        current = snapshot(p)
        if old is None or name not in old['files']:
            if current['kind'] != 'absent':
                raise Conflict(f'Existing unmanaged file: {name}')
        if current != {'kind': 'file', 'sha256': digest(data)}:
            writes[name] = data
        before[name] = current
    # An existing skill directory is a name collision even if SKILL.md is absent.
    names = sorted({n.split('/')[2] for n in files if n.startswith('.agents/skills/')})
    for name in names:
        prefix = f'.agents/skills/{name}/'
        if not old or not any(n.startswith(prefix) for n in old['files']):
            p = safe_path(root, prefix[:-1])
            if p.exists() or p.is_symlink():
                raise Conflict(f'Existing unmanaged skill directory: {p}')
    if old:
        removals = sorted(set(old['files']) - set(files))
        for name in removals:
            before[name] = snapshot(safe_path(root, name))
    for name, content in blocks().items():
        p = safe_path(root, name)
        before[name] = snapshot(p)
        if p.is_symlink() or (p.exists() and not p.is_file()):
            raise Conflict(f'Cannot modify shared file: {name}')
        original = p.read_bytes() if p.exists() else b''
        if old:
            left, _, right = split_block(original, name)
            wanted = left + content + right
        else:
            for token in (BEGIN, END, GBEGIN, GEND):
                if token.encode() in original:
                    raise Conflict(f'Unowned agent-system block: {name}')
            wanted = original + (b'\n\n' if original else b'') + content
            if original and name != '.gitignore':
                warnings.append(f'Review existing {name} for semantic conflicts with the operational contract')
        if wanted != original:
            writes[name] = wanted
    skill_root = safe_path(root, '.claude/skills')
    before['.claude/skills'] = snapshot(skill_root)
    if skill_root.is_symlink():
        if skill_root.resolve() != (root / '.agents/skills').resolve():
            raise Conflict('Existing .claude/skills link points elsewhere')
        if old and '.claude/skills' in old['links']:
            links['.claude/skills'] = old['links']['.claude/skills']
    elif not skill_root.exists():
        # Retain per-skill layout when recreating a folder in a fresh checkout.
        if old and '.claude/skills' not in old['links']:
            links.update({f'.claude/skills/{n}': f'../../.agents/skills/{n}' for n in names})
        else:
            links['.claude/skills'] = '../.agents/skills'
    elif skill_root.is_dir():
        links.update({f'.claude/skills/{n}': f'../../.agents/skills/{n}' for n in names})
    else:
        raise Conflict('.claude/skills must be a directory or matching symlink')
    for name, target in links.items():
        p = safe_path(root, name)
        current = snapshot(p)
        if current['kind'] != 'absent' and (not old or name not in old['links']):
            raise Conflict(f'Existing unmanaged Claude skill: {name}')
        before[name] = current
    if old:
        for name in set(old['links']) - set(links):
            # An adopted pre-existing whole-root link is never recorded or deleted.
            removals.append(name)
            before[name] = snapshot(safe_path(root, name))
    before[MANIFEST] = snapshot(safe_path(root, MANIFEST))
    memory = plan_memory(root, memory_key, memory_from)
    m = dict(schema=1, bundle=fingerprint, files={n: digest(b) for n,b in files.items()},
             blocks={n: digest(b) for n,b in blocks().items()}, links=links)
    return dict(root=root, source=source, writes=writes, removals=removals, links=links,
                before=before, manifest=m, memory=memory, warnings=warnings, marker=marker)


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.agent-system-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_file() and not path.is_symlink():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def has_changes(plan):
    mem = plan['memory']
    return bool(plan['writes'] or plan['removals'] or
                any(plan['before'][n]['kind'] == 'absent' for n in plan['links']) or
                mem['before']['kind'] == 'absent' or mem['migration'] or
                mem['saved_key'] != mem['key'] or mem['saved_store'] != str(mem['store']) or
                not (mem['target'] / '.git').is_dir() or
                snapshot(plan['root']/MANIFEST) != {'kind':'file','sha256':digest(dump(plan['manifest']))})


def apply_plan(plan):
    root, mem = plan['root'], plan['memory']
    for name, state in plan['before'].items():
        if snapshot(safe_path(root, name)) != state:
            raise Conflict(f'Path changed during planning: {name}')
    recheck_memory(root, mem)
    if not has_changes(plan):
        return
    backup = mem['common'] / 'agent-system-backups' / uuid.uuid4().hex
    backup.mkdir(parents=True)
    record = {'root': str(root), 'backup': str(backup), 'before': plan['before'],
              'memory_config': {'agents.memoryKey':mem['saved_key'], 'agents.memoryStore':mem['saved_store']},
              'memory_before': mem['before'], 'memory_target': str(mem['target'])}
    # Exclusive marker doubles as an installer lock; preserve it on every failure.
    with plan['marker'].open('x') as f:
        json.dump(record, f, indent=2)
    try:
        for name, state in plan['before'].items():
            if state['kind'] == 'file':
                dest = backup / 'files' / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root/name, dest)
        atomic_write(backup/'record.json', dump(record))
        if mem['migration']:
            shutil.copytree(mem['link'], backup/'legacy-memory')
            if tree_hashes(backup/'legacy-memory') != mem['sources']:
                raise Conflict('Legacy backup verification failed')
            atomic_write(backup/'migration-report.md', mem['migration']['report'])
            atomic_write(backup/'migration.json', mem['migration']['metadata'])
        env = dict(os.environ, AGENTS_MEMORY_STORE=str(mem['store']))
        subprocess.run(['bash', str(plan['source']/'setup.sh'), '--project', str(root),
                        '--storage-only', '--no-commit', mem['key']], env=env, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if mem['migration']:
            for name, data in mem['migration']['facts'].items():
                dest = mem['target']/name
                if not dest.exists():
                    atomic_write(dest, data)
                if dest.read_bytes() != data:
                    raise Conflict(f'Memory copy verification failed: {name}')
            if tree_hashes(mem['link']) != mem['sources']:
                raise Conflict('Original memory changed during migration')
            # Rename intact original out of the checkout; no recursive deletion.
            shutil.move(str(mem['link']), str(backup/'legacy-original'))
        if not mem['link'].is_symlink():
            mem['link'].parent.mkdir(parents=True, exist_ok=True)
            mem['link'].symlink_to(mem['target'], target_is_directory=True)
        for name, data in plan['writes'].items():
            atomic_write(safe_path(root, name), data)
        for name in plan['removals']:
            p = safe_path(root, name)
            if p.exists() or p.is_symlink():
                p.unlink()
        for name, target in plan['links'].items():
            p = safe_path(root, name)
            if not p.is_symlink():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.symlink_to(target, target_is_directory=True)
        atomic_write(root/MANIFEST, dump(plan['manifest']))
        plan['marker'].unlink()
        print(f'Backup: {backup}')
    except BaseException:
        print(f'Installation incomplete. Backups and recovery record: {backup}; marker: {plan["marker"]}')
        raise
