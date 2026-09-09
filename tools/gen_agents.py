#!/usr/bin/env python3
"""Генератор нативных определений агентов из канона.

    .agents/agents/*.md  ->  .claude/agents/*.md
                             .codex/agents/*.toml

Использование:
    gen_agents.py            сгенерировать и записать
    gen_agents.py --check    ничего не менять; упасть, если на диске не то,
                             что даёт канон (drift-проверка для CI)

Свойства:
    * детерминированность — один и тот же канон даёт байт в байт тот же вывод;
    * orphan cleanup удаляет ТОЛЬКО файлы с нашим маркером; чужое не трогает;
    * невыразимая граница доступа = ошибка генерации, а не тихое расширение прав.
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
CANON_DIR = ROOT / ".agents" / "agents"
ADAPTERS = [claude_adapter, codex_adapter]

REQUIRED = ["name", "description", "role", "models", "effort", "capabilities"]
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
            raise SchemaError(f"{path}: отсутствует обязательное поле '{f}'")

    if a["name"] != path.stem:
        raise SchemaError(f"{path}: name='{a['name']}' не совпадает с именем файла")

    if not isinstance(a["models"], dict) or set(a["models"]) != HARNESSES:
        raise SchemaError(
            f"{path}: 'models' должен задавать ровно {sorted(HARNESSES)}, "
            f"получено {sorted(a['models']) if isinstance(a['models'], dict) else a['models']}")

    if a["effort"] not in KNOWN_EFFORT:
        raise SchemaError(f"{path}: effort='{a['effort']}', допустимо {sorted(KNOWN_EFFORT)}")

    if not isinstance(a["capabilities"], list) or not a["capabilities"]:
        raise SchemaError(f"{path}: 'capabilities' должен быть непустым списком")

    unknown = set(a["capabilities"]) - KNOWN_CAPS
    if unknown:
        raise SchemaError(f"{path}: неизвестные capabilities: {sorted(unknown)}")

    if len(a["body"].strip()) < 50:
        raise SchemaError(f"{path}: тело промпта пустое или подозрительно короткое")

    for h, ov in (a.get("overrides") or {}).items():
        if h not in HARNESSES:
            raise SchemaError(f"{path}: overrides для неизвестного харнесса '{h}'")
        if ov not in ({}, "", None):
            raise SchemaError(
                f"{path}: непустой override для '{h}'. Поддержка дельт не реализована: "
                f"по дизайну они добавляются только при НАБЛЮДАЕМОМ расхождении "
                f"поведения, и вместе с ними — регрессия, объясняющая их существование.")


def build():
    """Возвращает ({путь: содержимое}, [предупреждения])."""
    files, warnings = {}, []
    canon = sorted(CANON_DIR.glob("*.md"))
    if not canon:
        raise SchemaError(f"{CANON_DIR}: канонических агентов не найдено")

    for path in canon:
        agent = load(path)
        for ad in ADAPTERS:
            text, warns = ad.render(agent)
            files[Path(ad.TARGET_DIR) / f"{agent['name']}{ad.EXT}"] = text
            warnings.extend(warns)
    return files, warnings


def orphans(expected):
    """Сгенерированные ранее файлы, которых канон больше не порождает.

    Владение подтверждается маркером внутри файла: файл без маркера мы не создавали
    и удалять не имеем права, даже если он лежит в целевом каталоге.
    """
    found = []
    for ad in ADAPTERS:
        d = ROOT / ad.TARGET_DIR
        if not d.is_dir():
            continue
        for f in sorted(d.glob(f"*{ad.EXT}")):
            rel = f.relative_to(ROOT)
            if rel in expected:
                continue
            if ad.MARKER in f.read_text(encoding="utf-8"):
                found.append(rel)
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="не менять файлы; выйти с кодом 1 при расхождении")
    args = ap.parse_args()

    try:
        files, warnings = build()
    except (SchemaError, Inexpressible, RenderError,
            frontmatter.FrontmatterError) as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 2

    for w in warnings:
        print(f"предупреждение: {w}", file=sys.stderr)

    stale = orphans(set(files))
    drift = []

    for rel, text in sorted(files.items()):
        p = ROOT / rel
        current = p.read_text(encoding="utf-8") if p.exists() else None
        if current == text:
            continue
        drift.append((rel, "отсутствует" if current is None else "устарел"))
        if not args.check:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")

    if args.check:
        if not drift and not stale:
            print(f"канон и сгенерированные файлы совпадают ({len(files)} файлов)")
            return 0
        for rel, why in drift:
            print(f"РАСХОЖДЕНИЕ: {rel} — {why}", file=sys.stderr)
        for rel in stale:
            print(f"ОСИРОТЕЛ: {rel} — канон его больше не порождает", file=sys.stderr)
        print("\nЗапустите tools/gen_agents.py и закоммитьте результат.", file=sys.stderr)
        return 1

    for rel, why in drift:
        print(f"записан: {rel} ({why})")
    for rel in stale:
        (ROOT / rel).unlink()
        print(f"удалён осиротевший: {rel}")
    if not drift and not stale:
        print(f"без изменений ({len(files)} файлов)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
