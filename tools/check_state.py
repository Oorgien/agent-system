#!/usr/bin/env python3
"""Проверка состояния задачи — механическая часть процедуры старта из AGENTS.md §2.

    check_state.py            проверить текущее состояние
    check_state.py --resolve  то же + показать, какую задачу выбрал бы fallback

Проверяет ровно то, что AGENTS.md требует проверять перед загрузкой задачи, и по тем же
правилам. Главное из них: при расхождении ACTIVE с веткой задача НЕ загружается молча —
загрузить чужую задачу хуже, чем не выбрать никакую.

Скрипт ничего не чинит и ничего не пишет: он только сообщает.
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".agents" / "state"
TASKS = STATE / "tasks"
ACTIVE = STATE / "ACTIVE"
MEMORY = ROOT / ".agents" / "memory"

OK, WARN, ERR = "ok  ", "warn", "ERR "


class Report:
    def __init__(self):
        self.rows, self.failed = [], False

    def add(self, level, msg):
        self.rows.append((level, msg))
        if level == ERR:
            self.failed = True

    def dump(self):
        for level, msg in self.rows:
            print(f"  [{level}] {msg}")
        return 1 if self.failed else 0


def git_branch():
    try:
        r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           cwd=ROOT, capture_output=True, text=True, timeout=5)
        b = r.stdout.strip()
        return None if r.returncode or b == "HEAD" else b
    except (OSError, subprocess.SubprocessError):
        return None


def task_meta(slug):
    """Читает frontmatter task.md. Возвращает (meta, ошибка)."""
    p = TASKS / slug / "task.md"
    if not p.is_file():
        return None, f"{p.relative_to(ROOT)} не существует"
    text = p.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None, f"{p.relative_to(ROOT)}: нет frontmatter"
    meta = {}
    for line in m.group(1).split("\n"):
        if ":" in line and not line.strip().startswith("#"):
            k, _, v = line.partition(":")
            meta[k.strip()] = v.split("#", 1)[0].strip()
    return meta, None


def all_tasks():
    return sorted(d.name for d in TASKS.iterdir() if d.is_dir()) if TASKS.is_dir() else []


def git_config(key):
    """Значение из ОБЩЕГО локального config. У worktrees он один (AGENTS.md §1)."""
    try:
        r = subprocess.run(["git", "config", "--local", "--get", key],
                           cwd=ROOT, capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def expected_memory():
    """Куда симлинк обязан вести по сохранённым настройкам, либо None.

    Ожидаемое берётся ТОЛЬКО из сохранённого setup.sh состояния и никогда не
    вычисляется из окружения валидатора: AGENTS_MEMORY_STORE задаётся ad hoc, а
    старт сессии происходит без него, и вычисленное ожидание объявило бы
    расхождением корректно настроенное дерево.
    """
    key, store = git_config("agents.memoryKey"), git_config("agents.memoryStore")
    return (Path(store) / key) if key and store else None


def memory(r):
    """Память проекта — безусловный шаг старта (AGENTS.md §2), проверяем всегда.

    Отсутствие — WARN: симлинк gitignored, в CI и в свежем клоне его законно нет.
    Ссылка на ЧУЖОЙ каталог при сохранённых настройках — ERR: это не «памяти нет»,
    а «читается не та память», и работать молча в таком дереве нельзя. Так выглядит
    worktree, в котором не повторили setup после смены ключа или хранилища.
    """
    if not MEMORY.is_symlink():
        if MEMORY.is_dir():
            r.add(WARN, ".agents/memory — каталог, а не симлинк на общее хранилище "
                        "(память не будет общей для worktrees, см. AGENTS.md §1)")
        else:
            r.add(WARN, ".agents/memory отсутствует — память проекта недоступна, "
                        "запустите ./setup.sh")
        return

    if not MEMORY.exists():
        r.add(WARN, f".agents/memory — битый симлинк на {os.readlink(MEMORY)}, "
                    f"запустите ./setup.sh")
        return

    expected = expected_memory()
    if expected is None:
        return

    # resolve() у самой ссылки, а не у os.readlink(): относительная цель
    # разрешается от каталога ссылки, а не от текущего каталога процесса.
    actual = MEMORY.resolve()
    if actual != expected.resolve():
        r.add(ERR, f".agents/memory ведёт в {actual}, а сохранённые настройки задают "
                   f"{expected}. Дерево читает не ту память: повторите ./setup.sh здесь")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--resolve", action="store_true",
                    help="показать результат discovery по ветке")
    args = ap.parse_args()

    r = Report()
    branch = git_branch()
    print(f"ветка: {branch or '(detached HEAD или не git)'}")
    print()

    slugs = all_tasks()

    # Пустой список задач сам по себе НЕ означает чистого состояния: ACTIVE
    # переживает удаление задачи и переключение ветки, а раньше проверка
    # возвращалась отсюда до того, как указатель вообще читался. Ранний выход
    # допустим только когда указателя тоже нет.
    if not slugs and not ACTIVE.is_file():
        r.add(OK, "задач нет, ACTIVE не выставлен — чистое состояние")
        memory(r)
        return r.dump()

    # --- целостность каждой задачи ----------------------------------------
    active_matching = []
    for slug in slugs:
        meta, err = task_meta(slug)
        if err:
            r.add(ERR, err)
            continue
        if meta.get("id") != slug:
            r.add(ERR, f"{slug}: id='{meta.get('id')}' не совпадает с именем каталога")
        if meta.get("status") not in ("active", "done", "abandoned"):
            r.add(ERR, f"{slug}: status='{meta.get('status')}' — недопустимое значение")
        if not (TASKS / slug / "journal.md").is_file():
            r.add(ERR, f"{slug}: нет journal.md")
        if meta.get("status") == "active" and branch and meta.get("branch") == branch:
            active_matching.append(slug)

    # --- ACTIVE ------------------------------------------------------------
    if not ACTIVE.is_file():
        r.add(WARN, "ACTIVE отсутствует — задача будет выбираться discovery по ветке")
    else:
        slug = ACTIVE.read_text(encoding="utf-8").strip()
        if "/" in slug or slug.startswith("."):
            r.add(ERR, f"ACTIVE='{slug}' — должен быть slug внутри tasks/, а не путь")
        elif slug not in slugs:
            r.add(ERR, f"ACTIVE='{slug}' — такой задачи нет")
        else:
            meta, err = task_meta(slug)
            if err:
                r.add(ERR, err)
            elif meta.get("status") != "active":
                r.add(ERR, f"ACTIVE='{slug}', но status='{meta.get('status')}'")
            elif branch and meta.get("branch") and meta["branch"] != branch:
                r.add(ERR,
                      f"ACTIVE='{slug}' привязан к ветке '{meta['branch']}', "
                      f"а мы на '{branch}'. НЕ загружать молча: перезапустить discovery")
            else:
                r.add(OK, f"активная задача: {slug}")

    # --- discovery ---------------------------------------------------------
    if args.resolve or not ACTIVE.is_file():
        if branch is None:
            r.add(WARN, "detached HEAD — автоматического совпадения по ветке нет, "
                        "задача выбирается явно")
        elif len(active_matching) == 1:
            r.add(OK, f"discovery по ветке: {active_matching[0]}")
        elif not active_matching:
            r.add(WARN, f"нет активной задачи с branch='{branch}' — выбор не делается")
        else:
            r.add(ERR, f"несколько активных задач на ветке '{branch}': "
                       f"{active_matching}. Автоматический выбор НЕ делается")

    memory(r)
    return r.dump()


if __name__ == "__main__":
    sys.exit(main())
