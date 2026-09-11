"""Validate the per-project repository layout without changing memory or Git config."""
import subprocess
from pathlib import Path


def layout_error(store, target, require_repository=False):
    store, target = Path(store), Path(target)
    metadata = store / '.git'
    if metadata.exists() or metadata.is_symlink() or ((store / 'HEAD').is_file() and (store / 'objects').is_dir()):
        return f'Legacy shared Git memory store: {store}. Preserve its history and migrate explicitly to separate project repositories.'
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        return f'Memory target must be an ordinary directory: {target}'
    metadata = target / '.git'
    if metadata.is_symlink() or (metadata.exists() and not metadata.is_dir()):
        return f'Unsupported project memory Git metadata: {metadata}'
    if metadata.is_dir():
        result = subprocess.run(['git', '-C', str(target), 'rev-parse', '--show-toplevel'],
                                capture_output=True, text=True)
        if result.returncode or Path(result.stdout.strip()).resolve() != target.resolve():
            return f'Invalid project memory repository: {target}'
    elif require_repository or ((target / 'HEAD').is_file() and (target / 'objects').is_dir()):
        return f'Memory needs its own Git working tree at {target}; initialize project memory with setup/init.'
    keep = target / '.gitkeep'
    if keep.is_symlink() or (keep.exists() and not keep.is_file()):
        return f'Unsafe memory initialization file: {keep}'
    return None
