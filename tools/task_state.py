"""Слой хранения и разрешения состояния задач (AGENTS.md §1–§2).

Модуль читающий и пишущий, но **не решающий**: он не выбирает задачу за агента и
не чинит битые привязки. Политика — в check_state.py и cli.py, здесь только формат.

Три сущности:

    tasks/<slug>/task.md              контракт задачи, в git
    tasks/<slug>/journal/<ts>-<sid8>.md  запись журнала, в git
    sessions/<session-id>             привязка чата к задаче, gitignored

Ключевое свойство формата — **отсутствие общего счётчика**. Имя записи журнала несёт
порядок (timestamp) и уникальность (session id), поэтому два чата пишут в одну задачу
одновременно, не читая соседние файлы и не блокируя каталог.
"""
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

# fullmatch, а не match: в Python `$` совпадает и перед завершающим переводом
# строки, поэтому "sid\n" прошёл бы валидацию и создал второй файл на тот же чат.
SESSION_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ENTRY_RE = re.compile(r"(\d{8}T\d{6}Z)-([A-Za-z0-9._-]{1,8})(?:-(\d+))?\.md")

STATUSES = ("active", "paused", "done", "abandoned")
BINDABLE = ("active", "paused")

# Источники session id, по убыванию приоритета. AGENTS_SESSION_ID первым: это
# единственный способ задать идентичность там, где харнесс её не экспортирует.
SESSION_ENV = ("AGENTS_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
               "CODEX_THREAD_ID", "CODEX_SESSION_ID")
LEGACY_JOURNAL = "journal.md"


class StateError(ValueError):
    """Формат нарушен. Отдельный тип, чтобы CLI отличал его от OSError."""


# --- пути -------------------------------------------------------------------

def state_dir(root):
    return Path(root) / ".agents" / "state"


def tasks_dir(root):
    return state_dir(root) / "tasks"


def sessions_dir(root):
    return state_dir(root) / "sessions"


def task_dir(root, slug):
    return tasks_dir(root) / validate_slug(slug)


def journal_dir(root, slug):
    return task_dir(root, slug) / "journal"


def validate_slug(slug):
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise StateError(f"недопустимый slug задачи: {slug!r}")
    return slug


def validate_session(sid):
    """Отказ, а не санитизация: подчищенный чужой id склеил бы два разных чата."""
    if not isinstance(sid, str) or not SESSION_RE.fullmatch(sid):
        raise StateError(f"недопустимый session id: {sid!r} "
                         f"(ожидается {SESSION_RE.pattern})")
    return sid


# --- идентичность сессии ----------------------------------------------------

def session_id(env=None):
    """Session id из окружения либо None.

    None — законный режим «без привязки», а не ошибка. Ошибка — только значение,
    которое источник дал, но которое не проходит валидацию: молча взять следующий
    источник значило бы работать под чужой идентичностью.
    """
    env = os.environ if env is None else env
    for name in SESSION_ENV:
        raw = (env.get(name) or "").strip()
        if raw:
            try:
                return validate_session(raw)
            except StateError as e:
                raise StateError(f"{name}: {e}") from e
    return None


def harness(env=None):
    env = os.environ if env is None else env
    if env.get("AGENTS_HARNESS"):
        return env["AGENTS_HARNESS"]
    if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_SESSION_ID"):
        return "claude"
    if env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID") or env.get("CODEX_HOME"):
        return "codex"
    return "unknown"


# --- запись -----------------------------------------------------------------

def atomic_write(path, data):
    """Временный файл в том же каталоге плюс os.replace: читатель видит либо
    старое содержимое целиком, либо новое, но не половину."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".agents-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def stamp(moment):
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- привязки ---------------------------------------------------------------

def binding_path(root, sid):
    return sessions_dir(root) / validate_session(sid)


def read_binding(root, sid):
    p = binding_path(root, sid)
    if p.is_symlink() or not p.is_file():
        return None
    try:
        record = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as e:
        raise StateError(f"{p.name}: привязка не читается как JSON: {e}") from e
    if not isinstance(record, dict) or not isinstance(record.get("slug"), str):
        raise StateError(f"{p.name}: в привязке нет slug")
    validate_slug(record["slug"])
    return record


def bindings(root):
    """Обратный индекс «какие чаты на задаче X» — это перечисление каталога.

    Возвращает (записи, проблемы). Битая привязка не роняет обход: её видит тот,
    кто её создал, а остальные сессии продолжают работать.
    """
    out, problems = {}, []
    directory = sessions_dir(root)
    if not directory.is_dir():
        return out, problems
    for p in sorted(directory.iterdir()):
        if p.name.startswith("."):
            continue
        try:
            validate_session(p.name)
            record = read_binding(root, p.name)
        except StateError as e:
            problems.append(str(e))
            continue
        if record is not None:
            out[p.name] = record
    return out, problems


def bind(root, sid, slug, harness_name="unknown", force=False, moment=None):
    """Идемпотентная привязка. Возвращает 'unchanged' | 'created' | 'rebound'.

    Перепривязка на другую задачу требует force: молчаливая перезапись увела бы
    чат с задачи, к которой он привязан, без единого следа.
    """
    validate_slug(slug)
    current = read_binding(root, sid)
    if current is not None:
        if current["slug"] == slug:
            return "unchanged"
        if not force:
            raise StateError(
                f"чат уже привязан к задаче '{current['slug']}'. "
                f"Перепривязка на '{slug}' — только явной командой (--force)")
    record = {"slug": slug, "harness": harness_name,
              "bound_at": iso(moment or now_utc())}
    atomic_write(binding_path(root, sid),
                 (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode())
    return "created" if current is None else "rebound"


def unbind(root, sid):
    p = binding_path(root, sid)
    if p.is_file() or p.is_symlink():
        p.unlink()
        return True
    return False


# --- задачи -----------------------------------------------------------------

def task_meta(root, slug):
    """Фронтматтер task.md. Возвращает (meta, ошибка) — обход всех задач не должен
    падать на одной сломанной."""
    p = task_dir(root, slug) / "task.md"
    if not p.is_file():
        return None, f"{slug}: нет task.md"
    try:
        raw, _ = frontmatter.split(p.read_text(encoding="utf-8"), str(p))
        meta = frontmatter.parse(raw, str(p))
    except (frontmatter.FrontmatterError, UnicodeError) as e:
        return None, f"{slug}: фронтматтер не разобран: {e}"
    if not all(isinstance(v, str) for v in meta.values()):
        return None, f"{slug}: во фронтматтере ожидаются только скаляры"
    return meta, None


def tasks(root):
    d = tasks_dir(root)
    return sorted(p.name for p in d.iterdir()
                  if p.is_dir() and not p.name.startswith(".")) if d.is_dir() else []


def task_problems(root, slug, meta):
    out = []
    if meta.get("id") != slug:
        out.append(f"{slug}: id='{meta.get('id')}' не совпадает с именем каталога")
    if meta.get("status") not in STATUSES:
        out.append(f"{slug}: status='{meta.get('status')}' — недопустимое значение "
                   f"(ожидается одно из {', '.join(STATUSES)})")
    return out


def binding_problems(root, slug):
    """Почему по этой привязке нельзя загружать задачу. Пусто — можно."""
    try:
        validate_slug(slug)
    except StateError as e:
        return [str(e)]
    meta, err = task_meta(root, slug)
    if err:
        return [err]
    problems = task_problems(root, slug, meta)
    if meta.get("status") in STATUSES and meta.get("status") not in BINDABLE:
        problems.append(f"{slug}: status='{meta['status']}' — задача завершена, "
                        f"привязка к ней недействительна")
    return problems


def create_task(root, slug, template, branch=None, moment=None):
    """Создаёт tasks/<slug>/task.md из шаблона. Существующую задачу не трогает."""
    validate_slug(slug)
    directory = task_dir(root, slug)
    if directory.exists() or directory.is_symlink():
        raise StateError(f"задача '{slug}' уже существует: {directory}")
    moment = moment or now_utc()
    body = template
    # Пустая ветка пишется как "" : голое `branch:` строгий парсер фронтматтера
    # прочитает как начало вложенного блока и упадёт.
    for token, value in (("<slug>", slug),
                         ("<branch>", branch or '""'),
                         ("<created>", moment.astimezone(timezone.utc).strftime("%Y-%m-%d"))):
        body = body.replace(token, value)
    atomic_write(directory / "task.md", body.encode("utf-8"))
    journal_dir(root, slug).mkdir(parents=True, exist_ok=True)
    return directory


def set_status(root, slug, status):
    """Единственная операция с настоящим read-modify-write. Лок не нужен: status
    задачи меняет один агент, а не гонка сессий."""
    if status not in STATUSES:
        raise StateError(f"недопустимый status: {status!r}")
    p = task_dir(root, slug) / "task.md"
    text = p.read_text(encoding="utf-8")
    raw, body = frontmatter.split(text, str(p))
    lines, replaced = raw.split("\n"), False
    for i, line in enumerate(lines):
        if line.startswith("status:"):
            comment = line.partition("#")[1:] if "#" in line else ("", "")
            lines[i] = f"status: {status}" + (f"        #{comment[1]}" if comment[1] else "")
            replaced = True
            break
    if not replaced:
        raise StateError(f"{p}: во фронтматтере нет поля status")
    atomic_write(p, ("---\n" + "\n".join(lines) + "---\n" + body).encode("utf-8"))
    return status


# --- журнал -----------------------------------------------------------------

def entry_key(name):
    """Ключ сортировки записи либо None, если имя не парсится.

    Ключ — уточнение лексикографического порядка имён: он дополнительно ставит
    `-2` после базового имени, а не перед ним (`-` < `.` в ASCII).
    """
    m = ENTRY_RE.fullmatch(name)
    return (m.group(1), m.group(2), int(m.group(3) or 1)) if m else None


def entry_name(moment, sid, ordinal=1):
    validate_session(sid)
    suffix = "" if ordinal <= 1 else f"-{ordinal}"
    return f"{stamp(moment)}-{sid[:8]}{suffix}.md"


def journal_entries(root, slug):
    """Канонический порядок чтения: legacy journal.md, затем journal/ по ключу.

    Нераспознанные имена не отбрасываются — они идут последними и о них сообщает
    check_state. Молча пропущенная запись журнала хуже, чем запись не на месте.
    """
    directory = task_dir(root, slug)
    ordered, broken = [], []
    legacy = directory / LEGACY_JOURNAL
    if legacy.is_file():
        ordered.append(legacy)
    entries = journal_dir(root, slug)
    if entries.is_dir():
        keyed = []
        for p in entries.iterdir():
            if p.name.startswith(".") or not p.is_file():
                continue
            key = entry_key(p.name)
            (keyed if key else broken).append((key, p) if key else p)
        ordered += [p for _, p in sorted(keyed, key=lambda item: item[0])]
    return ordered + sorted(broken), sorted(p.name for p in broken)


def write_entry(root, slug, sid, text, stage=None, moment=None):
    """Чекпойнт — создание нового файла. Существующие записи не переписываются.

    Блокировка не нужна: имя уникально по session id, поэтому одновременная запись
    двух чатов в одну задачу даёт два разных файла, а не гонку за один.
    """
    validate_slug(slug)
    validate_session(sid)
    moment = moment or now_utc()
    directory = journal_dir(root, slug)
    directory.mkdir(parents=True, exist_ok=True)
    head = [f"session: {sid}", f"at: {iso(moment)}"]
    if stage:
        head.append(f"stage: {stage}")
    data = ("---\n" + "\n".join(head) + "\n---\n\n" + text.rstrip("\n") + "\n").encode("utf-8")
    # Коллизия возможна только внутри одной секунды у одной сессии: между
    # сессиями имена различаются по построению.
    for ordinal in range(1, 1000):
        p = directory / entry_name(moment, sid, ordinal)
        try:
            with open(p, "xb") as stream:
                stream.write(data)
            return p
        except FileExistsError:
            continue
    raise StateError(f"не удалось выбрать имя записи в {directory}")


# --- legacy -----------------------------------------------------------------

def legacy_pointer(root):
    """Старый ACTIVE как кандидат на привязку. Не указатель: его роль забрал sessions/."""
    p = state_dir(root) / "ACTIVE"
    if not p.is_file() or p.is_symlink():
        return None
    slug = p.read_text(encoding="utf-8").strip()
    return slug or None


def drop_legacy(root, slug=None):
    """Снимает ACTIVE и LOCK после успешной привязки: обе роли забрал sessions/.

    ACTIVE снимается, только если он указывал на ту же задачу: иначе это ещё
    не перенесённая подсказка для другого чата, и удалять её нам не за что.
    """
    removed = []
    pointer = legacy_pointer(root)
    names = ["LOCK"] + (["ACTIVE"] if pointer and (slug is None or pointer == slug) else [])
    for name in names:
        p = state_dir(root) / name
        if p.is_file() or p.is_symlink():
            p.unlink()
            removed.append(name)
    return sorted(removed)


# --- разрешение -------------------------------------------------------------

class Resolution:
    """Что видит сессия на старте. Ничего не пишет и ничего не выбирает."""

    def __init__(self, kind, sid=None, slug=None, record=None,
                 candidates=(), problems=(), legacy=None):
        self.kind = kind              # no-session | bound | invalid | suggest | none | ambiguous
        self.sid = sid
        self.slug = slug
        self.record = record
        self.candidates = list(candidates)
        self.problems = list(problems)
        self.legacy = legacy


def resolve(root, sid):
    """AGENTS.md §2: привязка → валидация; иначе discovery, но БЕЗ молчаливого выбора."""
    legacy = legacy_pointer(root)
    open_tasks = []
    for slug in tasks(root):
        meta, err = task_meta(root, slug)
        # Сломанную задачу в кандидаты не берём: предложить то, что bind тут же
        # отвергнет, хуже, чем не предложить ничего.
        if not err and meta.get("status") == "active" and not task_problems(root, slug, meta):
            open_tasks.append(slug)

    if sid is None:
        return Resolution("no-session", candidates=open_tasks, legacy=legacy)

    record = read_binding(root, sid)
    if record is not None:
        problems = binding_problems(root, record["slug"])
        kind = "bound" if not problems else "invalid"
        return Resolution(kind, sid=sid, slug=record["slug"], record=record,
                          candidates=open_tasks if problems else (),
                          problems=problems, legacy=legacy)

    if len(open_tasks) == 1:
        return Resolution("suggest", sid=sid, slug=open_tasks[0],
                          candidates=open_tasks, legacy=legacy)
    kind = "none" if not open_tasks else "ambiguous"
    return Resolution(kind, sid=sid, candidates=open_tasks, legacy=legacy)
