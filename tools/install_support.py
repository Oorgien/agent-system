"""Read-only installation planning primitives; no harness invocation."""
import hashlib
import json
import os
import subprocess
from pathlib import Path, PurePosixPath


class Conflict(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(root, *args, optional=False):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True)
    if result.returncode:
        if optional and result.returncode == 1:
            return None
        raise RuntimeError(result.stderr.strip() or 'git command failed')
    return result.stdout.strip()


def resolve_root(path):
    return Path(git(Path(path).resolve(), 'rev-parse', '--show-toplevel')).resolve()


def config(root, key):
    return git(root, 'config', '--local', '--get', key, optional=True)


def relative(name):
    p = PurePosixPath(name)
    if not name or p.is_absolute() or '..' in p.parts or '.' == name or '\\' in name:
        raise Conflict(f'Unsafe managed path: {name}')
    if str(p) != name:
        raise Conflict(f'Noncanonical path: {name}')
    return p


def safe_path(root, name):
    p = root / relative(name)
    for parent in p.parents:
        if parent == root:
            break
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise Conflict(f'Unsafe parent directory: {parent}')
    return p


def snapshot(path):
    if path.is_symlink():
        return {'kind': 'link', 'target': os.readlink(path)}
    if not path.exists():
        return {'kind': 'absent'}
    if path.is_file():
        return {'kind': 'file', 'sha256': digest(path.read_bytes())}
    return {'kind': 'directory'}


def tree_files(root):
    """Reject links/special files rather than copying outside the selected tree."""
    if root.is_symlink() or not root.is_dir():
        raise Conflict(f'Expected an ordinary directory: {root}')
    out = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise Conflict(f'Symlink in memory/source tree: {path}')
        if path.is_file():
            out[path.relative_to(root).as_posix()] = path.read_bytes()
        elif not path.is_dir():
            raise Conflict(f'Unsupported file: {path}')
    return out


def tree_hashes(root):
    return {name: digest(data) for name, data in tree_files(root).items()}


def dump(data):
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
