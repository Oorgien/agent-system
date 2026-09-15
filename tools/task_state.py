"""Task-state storage and resolution layer (AGENTS.md §1–§2).

This module reads and writes, but does **not decide**: it neither chooses a task
for the agent nor repairs broken bindings. Policy lives in check_state.py and
cli.py; this module only defines the format.

Three entities:

    tasks/<slug>/task.md              task contract, versioned in Git
    tasks/<slug>/journal/<ts>-<hash>-<ordinal>.md  journal entry, versioned in Git
    sessions/<session-id>             chat-to-task binding, gitignored

The key format property is **no shared counter**. A journal filename carries
its display order (timestamp) and a session token, so two chats can write to one
task concurrently without reading neighboring files or locking the directory.
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

# Use fullmatch, not match: Python's `$` also matches before a trailing newline,
# so "sid\n" would pass validation and create a second file for the same chat.
SESSION_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ENTRY_RE = re.compile(r"(\d{8}T\d{6}Z)-([0-9a-f]{32})-([0-9]{6})\.md")
STAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
TEMP_RE = re.compile(r"\.agents-[0-9a-f]{32}")


STATUSES = ("active", "paused", "done", "abandoned")
BINDABLE = ("active", "paused")

# Session ID sources, in descending priority. AGENTS_SESSION_ID comes first:
# it is the only way to supply an identity when the harness does not export one.
SESSION_ENV = ("AGENTS_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
               "CODEX_THREAD_ID", "CODEX_SESSION_ID")
LEGACY_JOURNAL = "journal.md"


# --- paths -------------------------------------------------------------------

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
        raise StateError(f"invalid task slug: {slug!r}")
    return slug


def validate_session(sid):
    """Reject rather than sanitize: cleaning up an ID could merge two distinct chats."""
    if (not isinstance(sid, str) or not SESSION_RE.fullmatch(sid)
            or sid in (".", "..", ".locks", ".gitkeep", ".DS_Store") or TEMP_RE.fullmatch(sid)):
        raise StateError(f"invalid session ID: {sid!r} "
                         f"(expected {SESSION_RE.pattern})")
    return sid


# --- session identity ----------------------------------------------------

def session_id(env=None):
    """Session ID from the environment, or None.

    None is a valid "unbound" mode, not an error. Only a supplied value that fails
    validation is an error: silently falling back to the next source could mean
    working under another chat's identity.
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


# --- writing -----------------------------------------------------------------

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


# --- bindings ---------------------------------------------------------------

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
        raise StateError(f"{p.name}: binding cannot be parsed as JSON: {e}") from e

    if not isinstance(record, dict) or not isinstance(record.get("slug"), str):
        raise StateError(f"{p.name}: binding has no slug")
    validate_slug(record["slug"])
    if not isinstance(record.get("harness"), str) or not record["harness"]:
        raise StateError(f"{p.name}: missing harness")
    parse_time(record.get("bound_at"))
    return record


def bindings(root):
    """The reverse index of "which chats are on task X" is a directory listing.

    Return (records, problems). A broken binding does not abort the scan: its
    creator can see the error while other sessions continue working.
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
    """Idempotent binding. Return 'unchanged' | 'created' | 'rebound'.

    Rebinding to a different task requires force: silently overwriting the binding
    would move the chat away from its task without leaving a trace.
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
                raise StateError(f"chat is already bound to task '{current['slug']}'. "
                                 f"Rebinding to '{slug}' — requires explicit --force")
        record = {"slug": slug, "harness": harness_name,
                  "bound_at": iso(moment or now_utc())}
        atomic_write(root, binding_path(root, sid),
                     (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode())
        return "created" if current is None else "rebound"


def unbind(root, sid):
    # Validate before taking the lock, so missing/foreign paths aren't removed.
    with session_lock(root, sid):
        return fs.unlink(root, binding_path(root, sid).relative_to(root))


# --- tasks -----------------------------------------------------------------

def task_meta(root, slug):
    """task.md frontmatter. Return (meta, error): one broken task must not
    abort a scan of all tasks."""
    p = task_dir(root, slug) / "task.md"
    try:
        text = read_text(root, p)
        if text is None:
            return None, f"{slug}: missing task.md"
        raw, _ = frontmatter.split(text, str(p))
        meta = frontmatter.parse(raw, str(p))
    except (StateError, frontmatter.FrontmatterError, UnicodeError) as e:
        return None, f"{slug}: task.md could not be parsed: {e}"
    if not all(isinstance(v, str) for v in meta.values()):
        return None, f"{slug}: frontmatter must contain only scalars"
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
        out.append(f"{slug}: id='{meta.get('id')}' does not match the directory name")
    if meta.get("status") not in STATUSES:
        out.append(f"{slug}: status='{meta.get('status')}' — invalid value "
                   f"(expected one of {', '.join(STATUSES)})")
    return out


def binding_problems(root, slug):
    """Reasons this binding cannot load its task. An empty list means it can."""
    try:
        validate_slug(slug)
    except StateError as e:
        return [str(e)]
    meta, err = task_meta(root, slug)
    if err:
        return [err]
    problems = task_problems(root, slug, meta)
    if meta.get("status") in STATUSES and meta.get("status") not in BINDABLE:
        problems.append(f"{slug}: status='{meta['status']}' — task is finished, "
                        f"its binding is invalid")
    return problems


def create_task(root, slug, template, branch=None, moment=None):
    """Create tasks/<slug>/task.md from a template. Leave existing tasks untouched."""
    validate_slug(slug)
    directory = task_dir(root, slug)
    if directory.exists() or directory.is_symlink():
        raise StateError(f"task '{slug}' already exists: {directory}")
    moment = moment or now_utc()
    body = template
    # Write an empty branch as "": the strict frontmatter parser would treat
    # a bare `branch:` as the start of a nested block and fail.
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
    """The only operation with a real read-modify-write. No lock is needed:
    task status is changed by one agent, not competing sessions."""
    if status not in STATUSES:
        raise StateError(f"invalid status: {status!r}")
    meta, err = task_meta(root, slug)
    problems = task_problems(root, slug, meta) if not err else [err]
    if problems:
        raise StateError("; ".join(problems))
    p = task_dir(root, slug) / "task.md"
    text = read_text(root, p)
    if text is None:
        raise StateError(f"{slug}: missing task.md")
    raw, body = frontmatter.split(text, str(p))
    lines, replaced = raw.split("\n"), False
    for i, line in enumerate(lines):
        if line.startswith("status:"):
            comment = line.partition("#")[1:] if "#" in line else ("", "")
            lines[i] = f"status: {status}" + (f"        #{comment[1]}" if comment[1] else "")
            replaced = True
            break
    if not replaced:
        raise StateError(f"{p}: frontmatter has no status field")
    atomic_write(root, p, ("---\n" + "\n".join(lines) + "---\n" + body).encode("utf-8"))
    return status


# --- journal -----------------------------------------------------------------

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
    """Legacy ACTIVE is a binding candidate. sessions/ has replaced it as a pointer."""
    p = state_dir(root) / "ACTIVE"
    text = read_text(root, p)
    if text is None:
        return None
    slug = text.strip()
    return slug or None


def drop_legacy(root, slug=None):
    """Remove matching ACTIVE after binding. Preserve another session's LOCK.

    Remove ACTIVE only if it points to the same task. Otherwise it is an unmigrated
    hint for another chat, and there is no reason for us to delete it.
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


# --- resolution -------------------------------------------------------------

class Resolution:
    """What a session sees at startup. Neither writes nor chooses anything."""

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
    """AGENTS.md §2: binding -> validation; otherwise discovery, with NO silent choice."""
    legacy = legacy_pointer(root)
    open_tasks = []
    for slug in tasks(root):
        meta, err = task_meta(root, slug)
        # Exclude broken tasks from candidates: suggesting one that bind would
        # immediately reject is worse than suggesting none.
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
