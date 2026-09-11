"""User-facing entrypoint. Network-free and dependency-free."""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import check_state
import gen_agents
import task_state as ts
from adapters import RenderError, Inexpressible
from frontmatter import FrontmatterError
from install_support import Conflict, digest, resolve_root, safe_path, snapshot
from installer import (MANIFEST, apply_plan, bundle, has_changes, load_manifest,
                       make_plan, pending_path, verify_owned)

SOURCE = Path(__file__).resolve().parent.parent


def doctor(root):
    marker = pending_path(root)
    if marker.exists() or marker.is_symlink():
        raise Conflict(f'Interrupted installation; inspect {marker} and its backup')
    m = load_manifest(root)
    if not m:
        raise Conflict('No installation manifest. Run agent-system init')
    verify_owned(root, m)
    errors = False
    for name in m['links']:
        p = safe_path(root, name)
        if not p.is_symlink() or not p.exists():
            print(f'ERR: missing/broken link {name}; run agent-system init')
            errors = True
    skills = root/'.claude/skills'
    if not skills.exists():
        print('ERR: Claude skills unavailable; run agent-system init')
        errors = True
    elif skills.is_symlink() and skills.resolve() != (root/'.agents/skills').resolve():
        print('ERR: Claude skill root points elsewhere')
        errors = True
    names = {Path(n).stem for n in m["files"] if n.startswith(".agents/agents/")}
    generated, warnings = gen_agents.build(root, names=names)
    for name, content in generated.items():
        if str(name) not in m['files']:
            continue
        p = safe_path(root, str(name))
        if snapshot(p) != {'kind':'file','sha256':digest(content.encode())}:
            print(f'ERR: generated drift {name}')
            errors = True
    for w in warnings:
        print(f'WARN: {w}')
    _, current, _ = bundle(SOURCE)
    if current != m['bundle']:
        print('WARN: tool bundle differs; agent-system update is available')
    result = check_state.main(['--project', str(root)])
    print('Harness execution is not checked by doctor; run acceptance scenarios separately.')
    return 1 if errors or result else 0


def task_template(root):
    """Шаблон задачи берётся из проекта; для свежего дерева — из исходников."""
    local = ts.state_dir(root) / "templates" / "task.md"
    source = SOURCE / ".agents/state/templates/task.md"
    return (local if local.is_file() else source).read_text(encoding="utf-8")


def describe(root, slug, records):
    meta, err = ts.task_meta(root, slug)
    if err:
        return f'{slug:<24} ERR  {err}'
    chats = sorted(sid for sid, r in records.items() if r["slug"] == slug)
    entries, broken = ts.journal_entries(root, slug)
    tail = f', чаты: {", ".join(chats)}' if chats else ''
    tail += f', НЕРАЗБОРНЫХ ЗАПИСЕЙ: {len(broken)}' if broken else ''
    return (f'{slug:<24} {meta.get("status", "?"):<10} '
            f'branch={meta.get("branch") or "-"}, записей: {len(entries)}{tail}')


def task_list(root):
    records, problems = ts.bindings(root)
    for problem in problems:
        print(f'WARN: привязка не читается: {problem}')
    slugs = ts.tasks(root)
    if not slugs:
        print('Задач нет')
    for slug in slugs:
        print(describe(root, slug, records))
    orphan = {sid: r for sid, r in records.items() if r["slug"] not in slugs}
    for sid, record in sorted(orphan.items()):
        print(f'WARN: чат {sid} привязан к несуществующей задаче {record["slug"]!r}')
    return 0


def task_status(root, sid):
    """Ничего не пишет: показывает то же, что видит сессия на старте (AGENTS.md §2)."""
    r = ts.resolve(root, sid)
    print(f'Проект: {root}')
    print(f'Чат:    {sid or "(session id не определён)"}')
    if r.legacy:
        print(f'LEGACY: .agents/state/ACTIVE={r.legacy} — кандидат на привязку; '
              f'снимается после `task bind`')
    if r.kind == 'no-session':
        print('Привязки нет: харнесс не сообщил session id. Задайте AGENTS_SESSION_ID '
              'или используйте `task bind --session-id <id>`')
    elif r.kind == 'bound':
        print(f'Задача:  {r.slug} (привязано {r.record.get("bound_at")}, '
              f'харнесс {r.record.get("harness")})')
    elif r.kind == 'invalid':
        print(f'Привязка к {r.slug!r} НЕДЕЙСТВИТЕЛЬНА и автоматически не чинится:')
        for problem in r.problems:
            print(f'  - {problem}')
    elif r.kind == 'suggest':
        print(f'Привязки нет. Единственный кандидат: {r.slug} — '
              f'`task bind {r.slug}` (молча не привязывается)')
    elif r.kind == 'ambiguous':
        print('Привязки нет, активных задач несколько — выбор за вами:')
        for slug in r.candidates:
            print(f'  - {slug}')
    else:
        print('Привязки нет, активных задач нет — `task new <slug>`')
    if r.kind in ('invalid', 'ambiguous') and r.candidates:
        print('Кандидаты: ' + ', '.join(r.candidates))
    return 1 if r.kind == 'invalid' else 0


def task_bind(root, sid, slug, force, harness_name):
    if sid is None:
        raise Conflict('session id не определён: задайте AGENTS_SESSION_ID или '
                       'передайте --session-id')
    if slug is None:
        r = ts.resolve(root, sid)
        # Legacy-указатель важнее discovery: он и есть то, чем этот чат жил раньше.
        slug = r.legacy or (r.slug if r.kind in ('bound', 'suggest') else None)
        if slug is None:
            raise Conflict('Задача не указана, и однозначного кандидата нет: ' +
                           (', '.join(r.candidates) if r.candidates else 'активных задач нет'))
    problems = ts.binding_problems(root, slug)
    if problems:
        raise Conflict('; '.join(problems))
    result = ts.bind(root, sid, slug, harness_name=harness_name, force=force)
    print({'created': f'Привязано: {sid} -> {slug}',
           'unchanged': f'Уже привязано: {sid} -> {slug}',
           'rebound': f'Перепривязано: {sid} -> {slug}'}[result])
    for name in ts.drop_legacy(root, slug):
        print(f'Удалён устаревший .agents/state/{name}: его роль забрал sessions/')
    return 0


def task_unbind(root, sid):
    if sid is None:
        raise Conflict('session id не определён: задайте AGENTS_SESSION_ID или '
                       'передайте --session-id')
    print(f'Привязка снята: {sid}' if ts.unbind(root, sid) else f'Привязок не было: {sid}')
    return 0


def task_new(root, slug, branch):
    directory = ts.create_task(root, slug, task_template(root), branch=branch)
    print(f'Создано: {directory.relative_to(root)}/task.md')
    print(f'Журнал:  {ts.journal_dir(root, slug).relative_to(root)}/ (записи появятся '
          f'при первом checkpoint)')
    print(f'Привязать текущий чат: agent-system task bind {slug}')
    return 0


def task_checkpoint(root, sid, slug, stage, message):
    """Новая запись журнала. Имя и фронтматтер собирает слой хранения: составленное
    руками имя — это и есть тот самый гоночный счётчик, от которого мы ушли."""
    if sid is None:
        raise Conflict('session id не определён: задайте AGENTS_SESSION_ID или '
                       'передайте --session-id')
    if slug is None:
        r = ts.resolve(root, sid)
        if r.kind != 'bound':
            raise Conflict('Чат не привязан к задаче: укажите её явно или '
                           '`task bind <slug>`')
        slug = r.slug
    text = message if message is not None else sys.stdin.read()
    if not text.strip():
        raise Conflict('Пустая запись не сохраняется')
    print(ts.write_entry(root, slug, sid, text, stage=stage).relative_to(root))
    return 0


def task_set_status(root, slug, status):
    """Смена status — единственная операция с настоящим read-modify-write (AGENTS.md §8)."""
    ts.set_status(root, slug, status)
    print(f'{slug}: status -> {status}')
    if status not in ts.BINDABLE:
        records, _ = ts.bindings(root)
        stale = sorted(sid for sid, r in records.items() if r["slug"] == slug)
        for sid in stale:
            print(f'WARN: чат {sid} остаётся привязан к завершённой задаче — '
                  f'`task unbind --session-id {sid}`')
    return 0


def add_task_commands(sub):
    # --project и --session-id принимаются и до, и после подкоманды: иначе
    # `task status --project .` молчаливо ломается на разборе аргументов.
    # SUPPRESS обязателен: без него разбор подкоманды затирает значение,
    # заданное до неё, дефолтным None.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--project', type=Path, default=argparse.SUPPRESS)
    common.add_argument('--session-id', default=argparse.SUPPRESS,
                        help='действовать от имени конкретного чата')
    p = sub.add_parser('task', parents=[common], help='задачи и привязки чатов')
    inner = p.add_subparsers(dest='task_command', required=True)
    for name in ('list', 'status', 'unbind'):
        inner.add_parser(name, parents=[common])
    new = inner.add_parser('new', parents=[common])
    new.add_argument('slug')
    new.add_argument('--branch', default=None)
    bind = inner.add_parser('bind', parents=[common])
    bind.add_argument('slug', nargs='?')
    bind.add_argument('--force', action='store_true',
                      help='перепривязать чат, уже привязанный к другой задаче')
    point = inner.add_parser('checkpoint', parents=[common])
    point.add_argument('slug', nargs='?')
    point.add_argument('--stage', default=None)
    point.add_argument('--message', default=None,
                       help='текст записи; без него читается со stdin')
    status = inner.add_parser('set-status', parents=[common])
    status.add_argument('slug')
    status.add_argument('status', choices=ts.STATUSES)
    return p


def run_task(args, root):
    explicit = getattr(args, 'session_id', None)
    sid = explicit or ts.session_id()
    if explicit:
        ts.validate_session(sid)
    if args.task_command == 'list':
        return task_list(root)
    if args.task_command == 'status':
        return task_status(root, sid)
    if args.task_command == 'new':
        return task_new(root, args.slug, args.branch)
    if args.task_command == 'bind':
        return task_bind(root, sid, args.slug, args.force, ts.harness())
    if args.task_command == 'unbind':
        return task_unbind(root, sid)
    if args.task_command == 'checkpoint':
        return task_checkpoint(root, sid, args.slug, args.stage, args.message)
    return task_set_status(root, args.slug, args.status)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='agent-system')
    parser.add_argument('--version', action='version', version='agent-system 0.1.0')
    parser.add_argument('--project', dest='global_project', type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    add_task_commands(sub)
    for command in ('init', 'update', 'doctor'):
        p = sub.add_parser(command)
        p.add_argument('--project', type=Path)
        if command != 'doctor':
            p.add_argument('--dry-run', action='store_true')
            p.add_argument('--memory-key')
        if command == 'init':
            p.add_argument('--memory-from', type=Path)
    args = parser.parse_args(argv)
    try:
        for executable in ('git', 'bash'):
            if not shutil.which(executable):
                raise RuntimeError(f'Missing required executable: {executable}')
        root = resolve_root(getattr(args, 'project', None) or args.global_project or Path.cwd())
        # Команды задач работают и в самом чекауте инструмента: у него своё
        # состояние задач, и запрещать их здесь незачем.
        if args.command == 'task':
            return run_task(args, root)
        if root == SOURCE.resolve():
            raise Conflict('This is the tool checkout; use its developer commands, not project installation')
        if args.command == 'doctor':
            return doctor(root)
        plan = make_plan(root, SOURCE, args.command, args.memory_key, getattr(args, 'memory_from', None))
        print(f'Project: {root}')
        for warning in plan['warnings']:
            print(f'WARN: {warning}')
        for name in sorted(plan['writes']):
            print(f'WRITE {name}')
        for name in plan['removals']:
            print(f'REMOVE {name}')
        for name, target in plan['links'].items():
            if plan['before'][name]['kind'] == 'absent':
                print(f'LINK {name} -> {target}')
        mem = plan['memory']
        print(f'Memory: {mem["target"]}')
        if mem['migration']:
            print('MIGRATE reviewed facts; preserve legacy memory and report in Git common-dir backup')
        if not has_changes(plan):
            print('Already installed; no changes')
        elif args.dry_run:
            print('Dry run: no files, directories, or Git settings changed')
        else:
            apply_plan(plan)
            print('Installation complete')
        return 0
    except Conflict as e:
        print(f'CONFLICT: {e}', file=sys.stderr)
        if 'migration' in str(e).lower():
            print(f'Migration skill: {SOURCE / "skills/migrate-memory/SKILL.md"}', file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError, TypeError, KeyError,
            RenderError, Inexpressible, FrontmatterError, subprocess.SubprocessError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
