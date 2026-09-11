#!/usr/bin/env python3
"""Проверка состояния задач — механическая часть процедуры старта из AGENTS.md §2.

    check_state.py            проверить состояние
    check_state.py --resolve  то же + показать, что видит текущий чат

Проверяет ровно то, что AGENTS.md требует проверять перед загрузкой задачи, и по тем же
правилам. Главное из них: битая привязка НЕ чинится автоматически и задача по ней не
загружается — загрузить чужую задачу хуже, чем не выбрать никакую.

Скрипт ничего не чинит и ничего не пишет: он только сообщает.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import task_state as ts
from memory_layout import layout_error

ROOT = Path(__file__).resolve().parent.parent
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

    actual = MEMORY.resolve()
    error = layout_error(actual.parent, actual, require_repository=True)
    if error:
        r.add(ERR, error)

    expected = expected_memory()
    if expected is None:
        return

    # resolve() у самой ссылки, а не у os.readlink(): относительная цель
    # разрешается от каталога ссылки, а не от текущего каталога процесса.
    if actual != expected.resolve():
        r.add(ERR, f".agents/memory ведёт в {actual}, а сохранённые настройки задают "
                   f"{expected}. Дерево читает не ту память: повторите ./setup.sh здесь")


def gc_candidates(root):
    """Привязки, которые будущий GC вправе удалить, — единая точка обхода.

    Сейчас функция только сообщает: чат мог быть привязан к задаче, которую завершили
    из другого окна, и удалять его файл молча нельзя, пока никто не спросил. Когда GC
    появится, он получит готовый обход и не будет переписан заново.

    Возвращает [(session_id, record, причина)]. Живые чаты от мёртвых здесь не
    отличаются: список процессов сессии — не наше знание.
    """
    out = []
    records, _ = ts.bindings(root)
    for sid, record in sorted(records.items()):
        problems = ts.binding_problems(root, record["slug"])
        if problems:
            out.append((sid, record, problems[0]))
    return out


def tasks(r, root, branch):
    """Целостность каждой задачи. Возвращает slug'и, к которым можно привязываться."""
    bindable = []
    for slug in ts.tasks(root):
        meta, err = ts.task_meta(root, slug)
        if err:
            r.add(ERR, err)
            continue
        problems = ts.task_problems(root, slug, meta)
        for problem in problems:
            r.add(ERR, problem)
        _, broken = ts.journal_entries(root, slug)
        for name in broken:
            r.add(ERR, f"{slug}: имя записи журнала не парсится: journal/{name} "
                       f"(ожидается <YYYYMMDDThhmmssZ>-<sid8>[-N].md)")
        if problems:
            continue
        if meta.get("status") in ts.BINDABLE:
            bindable.append(slug)
            # Ветка — подсказка: несколько задач в одном дереве теперь норма.
            if branch and meta.get("branch") and meta["branch"] != branch:
                r.add(WARN, f"{slug}: branch='{meta['branch']}', а мы на '{branch}' — "
                            f"подсказка устарела, это не ошибка")
    return bindable


def sessions(r, root):
    records, problems = ts.bindings(root)
    for problem in problems:
        r.add(ERR, f"привязка не читается: {problem}")
    for sid, record, reason in gc_candidates(root):
        r.add(ERR, f"чат {sid} привязан к задаче, к которой привязываться нельзя: {reason}")
    stale = {sid for sid, _, _ in gc_candidates(root)}
    by_task = {}
    for sid, record in records.items():
        if sid not in stale:            # о негодной привязке уже сообщили выше
            by_task.setdefault(record["slug"], []).append(sid)
    for slug in sorted(by_task):
        r.add(OK, f"{slug}: привязанных чатов — {len(by_task[slug])} "
                  f"({', '.join(sorted(by_task[slug]))})")
    return records


def legacy(r, root):
    """Старые ACTIVE и LOCK: не ошибка, но и не состояние — их роль забрал sessions/."""
    slug = ts.legacy_pointer(root)
    if slug:
        r.add(WARN, f"остался .agents/state/ACTIVE='{slug}' — привяжите чат "
                    f"(`agent-system task bind {slug}`), после этого указатель удаляется")
    if (ts.state_dir(root) / "LOCK").exists():
        r.add(WARN, "остался .agents/state/LOCK — его роль забрал sessions/, файл можно удалить")


def current(r, root, resolution):
    if resolution.kind == "no-session":
        r.add(WARN, "session id не определён — работа без привязки. Задайте "
                    "AGENTS_SESSION_ID или привяжите чат явно")
    elif resolution.kind == "bound":
        r.add(OK, f"чат {resolution.sid} привязан к задаче: {resolution.slug}")
    elif resolution.kind == "invalid":
        for problem in resolution.problems:
            r.add(ERR, f"привязка чата {resolution.sid} недействительна: {problem}. "
                       f"НЕ загружать молча: перезапустить discovery")
    elif resolution.kind == "suggest":
        r.add(WARN, f"чат не привязан; единственный кандидат — {resolution.slug}. "
                    f"Привязка не делается автоматически")
    elif resolution.kind == "ambiguous":
        r.add(WARN, f"чат не привязан, активных задач несколько: "
                    f"{', '.join(resolution.candidates)}. Автоматический выбор НЕ делается")
    else:
        r.add(WARN, "чат не привязан, активных задач нет")


def main(argv=None):
    global ROOT, MEMORY
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--resolve", action="store_true",
                    help="показать, что видит текущий чат (по умолчанию тоже показывается)")
    ap.add_argument("--project", type=Path, default=ROOT)
    ap.add_argument("--session-id", help="проверить от имени конкретного чата")
    args = ap.parse_args(argv)
    ROOT = args.project.resolve()
    MEMORY = ROOT / ".agents" / "memory"

    r = Report()
    branch = git_branch()
    print(f"ветка: {branch or '(detached HEAD или не git)'}")

    try:
        sid = args.session_id or ts.session_id()
        if args.session_id:
            ts.validate_session(sid)
    except ts.StateError as e:
        print()
        r.add(ERR, f"session id не принят: {e}")
        memory(r)
        return r.dump()
    print(f"чат:   {sid or '(session id не определён)'}")
    print()

    slugs = ts.tasks(ROOT)
    bindings, _ = ts.bindings(ROOT)
    if not slugs and not bindings and not ts.legacy_pointer(ROOT):
        r.add(OK, "задач нет, привязок нет — чистое состояние")
        memory(r)
        return r.dump()

    tasks(r, ROOT, branch)
    sessions(r, ROOT)
    legacy(r, ROOT)
    try:
        current(r, ROOT, ts.resolve(ROOT, sid))
    except ts.StateError as e:
        r.add(ERR, str(e))
    memory(r)
    return r.dump()


if __name__ == "__main__":
    sys.exit(main())
