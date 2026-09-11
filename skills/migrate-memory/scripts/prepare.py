#!/usr/bin/env python3
"""Snapshot source memory before editing; seal reviewed facts afterwards."""
import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'tools'))
from install_support import Conflict, dump, tree_hashes

p = argparse.ArgumentParser()
p.add_argument('mode', choices=['snapshot', 'seal'])
p.add_argument('source', type=Path)
p.add_argument('package', type=Path)
a = p.parse_args()
try:
    source, package = a.source.resolve(), a.package.resolve()
    if source == package or source in package.parents:
        raise Conflict('Package must be outside source memory')
    if a.mode == 'snapshot':
        hashes = tree_hashes(source)
        package.mkdir(parents=True, exist_ok=False)
        (package/'facts').mkdir()
        (package/'migration.json').write_bytes(dump({'schema':1, 'sources':hashes}))
    else:
        meta = json.loads((package/'migration.json').read_text())
        if tree_hashes(source) != meta['sources']:
            raise Conflict('Source changed; restart preparation')
        meta['facts'] = tree_hashes(package/'facts')
        mapping = json.loads((package/'mapping.json').read_text())
        if set(mapping) != set(meta['sources']):
            raise Conflict('Map every original file')
        if not meta['facts'] or any('/' in n or not n.endswith('.md') for n in meta['facts']):
            raise Conflict('Use a flat facts directory of Markdown files')
        for outputs in mapping.values():
            if not isinstance(outputs,list) or not outputs or any(n not in meta['facts'] for n in outputs):
                raise Conflict('Every source must map to existing facts; resolve outstanding questions first')
        if not (package/'report.md').read_text().strip():
            raise Conflict('Write a review report')
        meta['mapping'] = mapping
        (package/'migration.json').write_bytes(dump(meta))
    print(package)
except (Conflict, OSError, ValueError, KeyError) as e:
    sys.exit(str(e))
