#!/usr/bin/env python3
"""Generate native agent definitions from canonical sources.

    agents/*.md          ->  .claude/agents/*.md      in this repository
    .agents/agents/*.md  ->  .codex/agents/*.toml     in an installed project

Usage:
    gen_agents.py            generate and write files
    gen_agents.py --check    change nothing; fail if files on disk differ
                             from canonical output (CI drift check)

Properties:
    * deterministic: identical sources produce byte-for-byte identical output;
    * orphan cleanup deletes ONLY files with our marker; other files stay intact;
    * an inexpressible access boundary fails generation instead of widening access.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import frontmatter                                    # noqa: E402
from adapters import Inexpressible, RenderError       # noqa: E402
from adapters import claude as claude_adapter         # noqa: E402
from adapters import codex as codex_adapter           # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
# The package's canonical definitions live in this repository's sources. In an
# installed project, the installer copies them to .agents/agents/ for `update`.
CANON_DIR = ROOT / "agents"
INSTALLED_CANON = Path(".agents/agents")
ADAPTERS = [claude_adapter, codex_adapter]

REQUIRED = ["name", "description", "role", "capabilities"]
KNOWN_CAPS = {"filesystem-read", "filesystem-write", "code-search", "shell", "vcs", "web"}
KNOWN_EFFORT = {"high", "medium", "low"}
HARNESSES = {"claude", "codex"}


class SchemaError(ValueError):
    pass


def load(path):
    raw, body = frontmatter.split(path.read_text(encoding="utf-8"), str(path))
    agent = frontmatter.parse(raw, str(path))
    agent["body"] = body
    validate(agent, path)
    return agent


def validate(a, path):
    for f in REQUIRED:
        if f not in a:
            raise SchemaError(f"{path}: missing required field '{f}'")

    if a["name"] != path.stem:
        raise SchemaError(f"{path}: name='{a['name']}' does not match the filename")

    if "models" in a and (not isinstance(a["models"], dict) or set(a["models"]) != HARNESSES):
        raise SchemaError(
            f"{path}: 'models' must define exactly {sorted(HARNESSES)}, "
            f"got {sorted(a['models']) if isinstance(a['models'], dict) else a['models']}")

    if "effort" in a and a["effort"] not in KNOWN_EFFORT:
        raise SchemaError(f"{path}: effort='{a['effort']}', allowed values: {sorted(KNOWN_EFFORT)}")

    if not isinstance(a["capabilities"], list) or not a["capabilities"]:
        raise SchemaError(f"{path}: 'capabilities' must be a nonempty list")

    unknown = set(a["capabilities"]) - KNOWN_CAPS
    if unknown:
        raise SchemaError(f"{path}: unknown capabilities: {sorted(unknown)}")

    if len(a["body"].strip()) < 50:
        raise SchemaError(f"{path}: prompt body is empty or suspiciously short")

    for h, ov in (a.get("overrides") or {}).items():
        if h not in HARNESSES:
            raise SchemaError(f"{path}: overrides for unknown harness '{h}'")
        if ov not in ({}, "", None):
            raise SchemaError(
                f"{path}: nonempty override for '{h}'. Delta support is not implemented: "
                f"by design, deltas are added only for an OBSERVED difference in "
                f"behavior, together with a regression test explaining why they exist.")


def build(canon_dir=None, names=None, source_dir=None):
    """Return ({path: content}, [warnings]).

    `canon_dir` is the canonical source directory: package sources (`agents/`)
    by default, or `<root>/.agents/agents` for an installed project. The caller
    supplies it explicitly; the generator does not infer it from the root path.
    `source_dir` is the canonical path written in the GENERATED header. It defaults
    to the path corresponding to `canon_dir`; the installer reads package sources
    but writes to a project with `.agents/agents/`, passing INSTALLED_CANON explicitly.
    """
    files, warnings = {}, []
    canon_dir = CANON_DIR if canon_dir is None else Path(canon_dir)
    if source_dir is None:
        source_dir = Path("agents") if canon_dir == CANON_DIR else INSTALLED_CANON
    canon = sorted(canon_dir.glob("*.md"))
    if names is not None:
        canon = [p for p in canon if p.stem in names]
    if not canon:
        raise SchemaError(f"{canon_dir}: no canonical agents found")

    for path in canon:
        agent = load(path)
        for ad in ADAPTERS:
            text, warns = ad.render(agent, source_dir=source_dir)
            files[Path(ad.TARGET_DIR) / f"{agent['name']}{ad.EXT}"] = text
            warnings.extend(warns)
    return files, warnings


def orphans(expected, root=None):
    root = Path(root) if root is not None else ROOT
    """Previously generated files that canonical sources no longer produce.

    The marker inside each file proves ownership. A file without the marker was
    not created by us and must not be deleted, even inside the output directory.
    """
    found = []
    for ad in ADAPTERS:
        d = root / ad.TARGET_DIR
        if not d.is_dir():
            continue
        for f in sorted(d.glob(f"*{ad.EXT}")):
            rel = f.relative_to(root)
            if rel in expected:
                continue
            if ad.MARKER in f.read_text(encoding="utf-8"):
                found.append(rel)
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="leave files unchanged; exit with code 1 on drift")
    ap.add_argument("--project", type=Path, default=None,
                    help="installed project: read canonical definitions from its .agents/agents/")
    args = ap.parse_args()
    root = ROOT if args.project is None else args.project.resolve()
    canon_dir = CANON_DIR if args.project is None else root / INSTALLED_CANON

    try:
        files, warnings = build(canon_dir)
    except (SchemaError, Inexpressible, RenderError,
            frontmatter.FrontmatterError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)

    stale = orphans(set(files), root)
    drift = []

    for rel, text in sorted(files.items()):
        p = root / rel
        current = p.read_text(encoding="utf-8") if p.exists() else None
        if current == text:
            continue
        drift.append((rel, "missing" if current is None else "outdated"))
        if not args.check:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")

    if args.check:
        if not drift and not stale:
            print(f"canonical and generated files match ({len(files)} files)")
            return 0
        for rel, why in drift:
            print(f"DRIFT: {rel} — {why}", file=sys.stderr)
        for rel in stale:
            print(f"ORPHAN: {rel} — canonical sources no longer produce this file", file=sys.stderr)
        print("\nRun tools/gen_agents.py and commit the result.", file=sys.stderr)
        return 1

    for rel, why in drift:
        print(f"written: {rel} ({why})")
    for rel in stale:
        (root / rel).unlink()
        print(f"removed orphan: {rel}")
    if not drift and not stale:
        print(f"unchanged ({len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
