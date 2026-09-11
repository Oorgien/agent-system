"""Memory selection and migration validation, performed before installation writes."""
import os
import hashlib
from pathlib import Path
import json

from memory_layout import layout_error

from install_support import Conflict, config, git, safe_path, snapshot, tree_files, tree_hashes


def plan_memory(root, key_arg=None, prepared=None):
    link = safe_path(root, '.agents/memory')
    common = Path(git(root, 'rev-parse', '--git-common-dir'))
    if not common.is_absolute():
        common = root / common
    common = common.resolve()
    saved_key = config(root, 'agents.memoryKey')
    saved_store = config(root, 'agents.memoryStore')
    old_target = link.resolve() if link.is_symlink() else None
    key = key_arg or saved_key or (old_target.name if old_target else common.parent.name + '-memory')
    if not key or key in ('.', '..') or any(c in key for c in '/\\\n\r'):
        raise Conflict('Memory key must be a single directory name')
    store_arg = os.environ.get('AGENTS_MEMORY_STORE')
    store = Path(store_arg or saved_store or (str(old_target.parent) if old_target else '~/.agents-memory')).expanduser().resolve()
    target = store / key
    if store == root or root in store.parents:
        raise Conflict('Memory store must be outside the target working tree')
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise Conflict(f'Memory target is not an ordinary directory: {target}')
    error = layout_error(store, target)
    if error:
        raise Conflict(error)
    if old_target is not None and old_target != target:
        raise Conflict(f'Memory link points to {old_target}, expected {target}; resolve explicitly before installing')
    if link.exists() and not link.is_dir():
        raise Conflict(f'Memory is not a directory: {link}')
    migration = None
    source_hashes = None
    if link.is_dir() and not link.is_symlink():
        if not prepared:
            raise Conflict('Existing memory requires migration. Read the bundled skills/migrate-memory/SKILL.md; then use init --memory-from PATH')
        package = Path(prepared).resolve()
        if package == link or link in package.parents:
            raise Conflict('Migration package must be outside source memory')
        sources = tree_hashes(link)
        meta = json.loads((package / 'migration.json').read_text())
        if meta.get('schema') != 1 or meta.get('sources') != sources:
            raise Conflict('Source memory changed or migration snapshot is invalid')
        facts = tree_files(package / 'facts')
        if not facts or any('/' in n or not n.endswith('.md') for n in facts):
            raise Conflict('Prepared facts must be a nonempty flat directory of Markdown files')
        if meta.get('facts') != {n: hashlib.sha256(b).hexdigest() for n,b in facts.items()}:
            raise Conflict('Prepared facts changed after migration package was finalized')
        report = (package / 'report.md').read_bytes()
        if not report.strip():
            raise Conflict('Migration report is empty')
        mapping = meta.get('mapping', {})
        if set(mapping) != set(sources):
            raise Conflict('Migration report must map every source file')
        for name, outputs in mapping.items():
            if not isinstance(outputs, list) or not outputs or any(o not in facts for o in outputs):
                raise Conflict(f'Unresolved migration mapping: {name}')
        for name, data in facts.items():
            dest = safe_path(target, name)
            if dest.is_symlink() or (dest.exists() and (not dest.is_file() or dest.read_bytes() != data)):
                raise Conflict(f'Memory destination conflict: {dest}')
        source_hashes = sources
        migration = {'facts': facts, 'report': report, 'metadata': (package / 'migration.json').read_bytes()}
    elif prepared:
        raise Conflict('--memory-from requires an existing ordinary .agents/memory directory')
    return dict(link=link, target=target, store=store, key=key, common=common,
                saved_key=saved_key, saved_store=saved_store, before=snapshot(link),
                sources=source_hashes, migration=migration)


def recheck_memory(root, plan):
    error = layout_error(plan['store'], plan['target'])
    if error:
        raise Conflict(error)
    if snapshot(plan['link']) != plan['before']:
        raise Conflict('Memory link changed during planning')
    if config(root, 'agents.memoryKey') != plan['saved_key'] or config(root, 'agents.memoryStore') != plan['saved_store']:
        raise Conflict('Memory config changed during planning')
    if plan['sources'] is not None and tree_hashes(plan['link']) != plan['sources']:
        raise Conflict('Source memory changed during planning')
    if plan['migration']:
        for name, data in plan['migration']['facts'].items():
            dest = plan['target'] / name
            if dest.is_symlink() or (dest.exists() and (not dest.is_file() or dest.read_bytes() != data)):
                raise Conflict(f'Memory destination changed: {dest}')
