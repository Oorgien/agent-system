"""Слой хранения и разрешения состояния задач (AGENTS.md §1–§2).

Модуль читающий и пишущий, но **не решающий**: он не выбирает задачу за агента и
не чинит битые привязки. Политика — в check_state.py и cli.py, здесь только формат.

Три сущности:

    tasks/<slug>/task.md              контракт задачи, в git
    tasks/<slug>/journal/<ts>-<hash>-<ordinal>.md  запись журнала, в git
    sessions/<session-id>             привязка чата к задаче, gitignored

Ключевое свойство формата — **отсутствие общего счётчика**. Имя записи журнала несёт
порядок отображения (timestamp) и токен сессии, поэтому два чата пишут в одну задачу
одновременно, не читая соседние файлы и не блокируя каталог.
"""
import json
import os
import re
import hashlib
import state_fs as fs
from state_fs import StateError
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

# fullmatch, а не match: в Python `$` совпадает и перед завершающим переводом
# строки, поэтому "sid\n" прошёл бы валидацию и создал второй файл на тот же чат.
SESSION_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ENTRY_RE = re.compile(r"(\d{8}T\d{6}Z)-([0-9a-f]{32})-([0-9]{6})\.md")
STAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
TEMP_RE = re.compile(r"\.agents-[0-9a-f]{32}")


STATUSES = ("active", "paused", "done", "abandoned")
BINDABLE = ("active", "paused")

# Источники session id, по убыванию приоритета. AGENTS_SESSION_ID первым: это
# единственный способ задать идентичность там, где харнесс её не экспортирует.
SESSION_ENV = ("AGENTS_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
               "CODEX_THREAD_ID", "CODEX_SESSION_ID")
LEGACY_JOURNAL = "journal.md"


# --- пути -------------------------------------------------------------------

def state_dir(root):
    fs.check(root, ".agents/state")
    return Path(root) / ".agents" / "state"


def tasks_dir(root):
    path = state_dir(root) / "tasks"
    fs.check(root, path.relative_to(root))
    return path


def sessions_dir(root):
    path = state_dir(root) / "sessions"
    fs.check(root, path.relative_to(root))
    return path


def task_dir(root, slug):
    path = tasks_dir(root) / validate_slug(slug)
    fs.check(root, path.relative_to(root))
    return path


def journal_dir(root, slug):
    path = task_dir(root, slug) / "journal"
    fs.check(root, path.relative_to(root))
    return path


def validate_slug(slug):
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise StateError(f"недопустимый slug задачи: {slug!r}")
    return slug


def validate_session(sid):
    """Отказ, а не санитизация: подчищенный чужой id склеил бы два разных чата."""
    if (not isinstance(sid, str) or not SESSION_RE.fullmatch(sid)
            or sid in (".", "..", ".locks", ".gitkeep", ".DS_Store") or TEMP_RE.fullmatch(sid)):
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
        raw = env.get(name)
        if raw is not None and raw != "":
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

def atomic_write(root, path, data):
    return fs.write(root, path.relative_to(root), data)


def read_text(root, path):
    data = fs.read(root, path.relative_to(root))
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeError as e:
        raise StateError(f"{path}: invalid UTF-8") from e


def session_lock(root, sid):
    validate_session(sid)
    name = hashlib.sha256(sid.encode()).hexdigest()
    return fs.record_lock(root, Path(".agents/state/sessions/.locks") / name)


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
    text = read_text(root, p)
    if text is None:
        return None
    try:
        record = json.loads(text)
    except ValueError as e:
        raise StateError(f"{p.name}: привязка не читается как JSON: {e}") from e

    if not isinstance(record, dict) or not isinstance(record.get("slug"), str):
        raise StateError(f"{p.name}: в привязке нет slug")
    validate_slug(record["slug"])
    if not isinstance(record.get("harness"), str) or not record["harness"]:
        raise StateError(f"{p.name}: missing harness")
    parse_time(record.get("bound_at"))
    return record


def bindings(root):
    """Обратный индекс «какие чаты на задаче X» — это перечисление каталога.

    Возвращает (записи, проблемы). Битая привязка не роняет обход: её видит тот,
    кто её создал, а остальные сессии продолжают работать.
    """
    out, problems = {}, []
    directory = sessions_dir(root)
    for name in fs.names(root, directory.relative_to(root)):
        p = directory / name
        if name in (".locks", ".gitkeep", ".DS_Store") or TEMP_RE.fullmatch(name):
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
    with session_lock(root, sid):
        problems = binding_problems(root, slug)
        if problems:
            raise StateError("; ".join(problems))
        current = read_binding(root, sid)
        if current is not None:
            if current["slug"] == slug:
                return "unchanged"
            if not force:
                raise StateError(f"чат уже привязан к задаче '{current['slug']}'. "
                                 f"Перепривязка на '{slug}' — только явно (--force)")
        record = {"slug": slug, "harness": harness_name,
                  "bound_at": iso(moment or now_utc())}
        atomic_write(root, binding_path(root, sid),
                     (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode())
        return "created" if current is None else "rebound"


def unbind(root, sid):
    # Validate before taking the lock, so missing/foreign paths aren't removed.
    with session_lock(root, sid):
        return fs.unlink(root, binding_path(root, sid).relative_to(root))


# --- задачи -----------------------------------------------------------------

def task_meta(root, slug):
    """Фронтматтер task.md. Возвращает (meta, ошибка) — обход всех задач не должен
    падать на одной сломанной."""
    p = task_dir(root, slug) / "task.md"
    try:
        text = read_text(root, p)
        if text is None:
            return None, f"{slug}: нет task.md"
        raw, _ = frontmatter.split(text, str(p))
        meta = frontmatter.parse(raw, str(p))
    except (StateError, frontmatter.FrontmatterError, UnicodeError) as e:
        return None, f"{slug}: task.md не разобран: {e}"
    if not all(isinstance(v, str) for v in meta.values()):
        return None, f"{slug}: во фронтматтере ожидаются только скаляры"
    return meta, None


def tasks(root):
    d = tasks_dir(root)
    out = []
    for name in fs.names(root, d.relative_to(root)):
        if name in (".DS_Store", ".gitkeep") or TEMP_RE.fullmatch(name):
            continue
        validate_slug(name)
        with fs.directory(root, (d / name).relative_to(root)):
            pass
        out.append(name)
    return out


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
    if branch is not None and (not isinstance(branch, str) or any(ord(c) < 32 for c in branch)):
        raise StateError("branch must be a single line")
    raw, _ = frontmatter.split(body, str(directory / "task.md"))
    meta = frontmatter.parse(raw, str(directory / "task.md"))
    problems = task_problems(root, slug, meta)
    if problems:
        raise StateError("; ".join(problems))
    fs.mkdir(root, directory.relative_to(root), exclusive=True)
    # A crashed creation leaves a reserved, visibly incomplete task. Never adopt it.
    fs.mkdir(root, journal_dir(root, slug).relative_to(root))
    atomic_write(root, directory / "task.md", body.encode("utf-8"))
    return directory


def set_status(root, slug, status):
    """Единственная операция с настоящим read-modify-write. Лок не нужен: status
    задачи меняет один агент, а не гонка сессий."""
    if status not in STATUSES:
        raise StateError(f"недопустимый status: {status!r}")
    meta, err = task_meta(root, slug)
    problems = task_problems(root, slug, meta) if not err else [err]
    if problems:
        raise StateError("; ".join(problems))
    p = task_dir(root, slug) / "task.md"
    text = read_text(root, p)
    if text is None:
        raise StateError(f"{slug}: нет task.md")
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
    atomic_write(root, p, ("---\n" + "\n".join(lines) + "---\n" + body).encode("utf-8"))
    return status


# --- журнал -----------------------------------------------------------------

def parse_time(value):
    if not isinstance(value, str):
        raise StateError("Missing UTC timestamp")
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise StateError(f"Invalid UTC timestamp: {value!r}") from e
    if iso(dt) != value:
        raise StateError(f"Invalid UTC timestamp: {value!r}")
    return dt


def session_hash(sid):
    return hashlib.sha256(validate_session(sid).encode()).hexdigest()[:32]


def entry_key(name, meta=None):
    """New fixed format, or legacy name disambiguated by its full session metadata."""
    m = ENTRY_RE.fullmatch(name)
    if m:
        ts, digest, ordinal = m.groups()
        if int(ordinal) < 1:
            return None
        if meta and (digest != session_hash(meta["session"]) or ts != stamp(parse_time(meta["at"]))):
            return None
        return ts, digest, int(ordinal)
    if meta is not None:
        ts = stamp(parse_time(meta["at"]))
        base = f"{ts}-{meta['session'][:8]}"
        match = re.fullmatch(re.escape(base) + r"(?:-([2-9]|[1-9][0-9]+))?\.md", name)
        if match:
            return ts, session_hash(meta["session"]), int(match.group(1) or 1)
    return None


def entry_name(moment, sid, ordinal=1):
    if not 1 <= ordinal <= 999999:
        raise StateError("Entry ordinal out of range")
    return f"{stamp(moment)}-{session_hash(sid)}-{ordinal:06d}.md"


def entry_meta(text, path):
    raw, body = frontmatter.split(text, str(path))
    keys = re.findall(r"^([^ :]+):", raw, re.M)
    if len(keys) != len(set(keys)):
        raise StateError(f"{path}: duplicate metadata fields")
    meta = frontmatter.parse(raw, str(path))
    validate_session(meta.get("session"))
    parse_time(meta.get("at"))
    if "stage" in meta and (not isinstance(meta["stage"], str) or not STAGE_RE.fullmatch(meta["stage"])):
        raise StateError(f"{path}: invalid stage")
    if not body.strip():
        raise StateError(f"{path}: empty checkpoint")
    return meta


def journal_entries(root, slug):
    """Legacy file first, then validated entry keys; report every damaged record."""
    directory = task_dir(root, slug)
    ordered, broken, keyed = [], [], []
    legacy = directory / LEGACY_JOURNAL
    if read_text(root, legacy) is not None:
        ordered.append(legacy)
    entries = journal_dir(root, slug)
    for name in fs.names(root, entries.relative_to(root)):
        if TEMP_RE.fullmatch(name) or name in (".DS_Store", ".gitkeep"):
            continue
        p = entries / name
        try:
            text = read_text(root, p)
            if text is None:
                raise StateError(f"Entry disappeared: {p}")
            meta = entry_meta(text, p)
            key = entry_key(name, meta)
            if key is None:
                raise StateError(f"Entry name disagrees with metadata: {p}")
            keyed.append((key, name, p))
        except (StateError, frontmatter.FrontmatterError):
            broken.append(name)
    ordered += [p for _, _, p in sorted(keyed)]
    return ordered + [entries / n for n in sorted(broken)], sorted(broken)


def read_journal(root, slug):
    meta, err = task_meta(root, slug)
    if err or task_problems(root, slug, meta):
        raise StateError(err or "; ".join(task_problems(root, slug, meta)))
    ordered, broken = journal_entries(root, slug)
    if broken:
        raise StateError(f"{slug}: damaged journal entries: {', '.join(broken)}")
    # Collect before emitting anything; do not print a partial successful history.
    chunks = []
    for path in ordered:
        text = read_text(root, path)
        if text is None:
            raise StateError(f"Entry disappeared: {path}")
        if path.name != LEGACY_JOURNAL:
            entry_meta(text, path)
        chunks.append(f"# {path.name}\n\n{text.rstrip()}\n")
    return "\n".join(chunks)


def write_entry(root, slug, sid, text, stage=None, moment=None):
    """Publish a complete checkpoint exclusively; no partial canonical files."""
    validate_slug(slug)
    validate_session(sid)
    problems = binding_problems(root, slug)
    if problems:
        raise StateError("; ".join(problems))
    if not isinstance(text, str) or not text.strip():
        raise StateError("Empty checkpoint")
    if stage is not None and (not isinstance(stage, str) or not STAGE_RE.fullmatch(stage)):
        raise StateError("stage must be 1..64 characters: alphanumeric, dot, underscore, hyphen")
    moment = moment or now_utc()
    directory = journal_dir(root, slug)
    head = [f"session: {sid}", f"at: {iso(moment)}"]
    if stage is not None:
        head.append(f"stage: {stage}")
    data = ("---\n" + "\n".join(head) + "\n---\n\n" + text.rstrip("\n") + "\n").encode("utf-8")
    names = (entry_name(moment, sid, n) for n in range(1, 1000000))
    return fs.write(root, (directory / entry_name(moment, sid)).relative_to(root), data, candidates=names)


# --- legacy -----------------------------------------------------------------

def legacy_pointer(root):
    """Старый ACTIVE как кандидат на привязку. Не указатель: его роль забрал sessions/."""
    p = state_dir(root) / "ACTIVE"
    text = read_text(root, p)
    if text is None:
        return None
    slug = text.strip()
    return slug or None


def drop_legacy(root, slug=None):
    """Снимает совпадающий ACTIVE после привязки. Чужой LOCK сохраняется.

    ACTIVE снимается, только если он указывал на ту же задачу: иначе это ещё
    не перенесённая подсказка для другого чата, и удалять её нам не за что.
    """
    removed = []
    pointer = legacy_pointer(root)
    if read_text(root, state_dir(root) / "LOCK") is not None:
        return []
    # LOCK may belong to a still-running legacy session: never evict it here.
    names = ["ACTIVE"] if pointer and slug is not None and pointer == slug else []
    for name in names:
        p = state_dir(root) / name
        if fs.unlink(root, p.relative_to(root)):
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
