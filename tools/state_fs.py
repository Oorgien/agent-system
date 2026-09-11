"""POSIX state I/O anchored to the project directory; never follow state symlinks.

Directory descriptors pin parents across replacement of a path component. Locks
are per record and are released by the kernel when the process exits.
"""
from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import stat
import uuid


class StateError(ValueError):
    pass


def parts(relative):
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(p in ('.', '..') for p in path.parts):
        raise StateError(f'Unsafe state path: {relative}')
    return path.parts


@contextmanager
def directory(root, relative=None, create=False):
    fd = os.open(Path(root).resolve(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in parts(relative) if relative else ():
            if create:
                try:
                    os.mkdir(name, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise StateError(f'Not an ordinary state directory: {relative}') from e
                raise
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def regular(fd, name):
    try:
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(st.st_mode):
        raise StateError(f'Not an ordinary state file: {name}')
    return True


def check(root, relative):
    """Preflight an existing path without creating missing components."""
    path = Path(relative)
    try:
        with directory(root, path.parent) as fd:
            try:
                st = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                raise StateError(f'Not an ordinary state path: {relative}')
    except FileNotFoundError:
        pass


def read(root, relative):
    path = Path(relative)
    try:
        with directory(root, path.parent) as parent:
            if not regular(parent, path.name):
                return None
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise StateError(f'Not an ordinary state file: {relative}')
                return stream.read()
    except FileNotFoundError:
        return None


def names(root, relative):
    try:
        with directory(root, relative) as fd:
            return sorted(os.listdir(fd))
    except FileNotFoundError:
        return []


def mkdir(root, relative, exclusive=False):
    path = Path(relative)
    with directory(root, path.parent, create=True) as fd:
        try:
            os.mkdir(path.name, dir_fd=fd)
        except FileExistsError:
            if exclusive:
                raise StateError(f'State directory already exists: {relative}')
        with directory(root, relative):
            pass


def write(root, relative, data, candidates=None):
    """Write/fsync once, then replace a regular file or publish without overwrite.

    candidates contains leaf names only. Hard-link publication is atomic and fails
    if the destination exists; unlike replace it cannot clobber an earlier entry.
    """
    path = Path(relative)
    with directory(root, path.parent, create=True) as fd:
        regular(fd, path.name)
        tmp = '.agents-' + uuid.uuid4().hex
        handle = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fd)
        try:
            with os.fdopen(handle, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if candidates is None:
                regular(fd, path.name)
                os.replace(tmp, path.name, src_dir_fd=fd, dst_dir_fd=fd)
                result = path.name
            else:
                result = None
                for name in candidates:
                    if len(parts(name)) != 1:
                        raise StateError(f'Invalid entry name: {name}')
                    regular(fd, name)
                    try:
                        os.link(tmp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                        result = name
                        break
                    except FileExistsError:
                        continue
                if result is None:
                    raise StateError(f'No free entry name in {path.parent}')
            os.fsync(fd)
            return Path(root) / path.parent / result
        finally:
            try:
                os.unlink(tmp, dir_fd=fd)
            except FileNotFoundError:
                pass


def unlink(root, relative):
    path = Path(relative)
    try:
        with directory(root, path.parent) as fd:
            if not regular(fd, path.name):
                return False
            os.unlink(path.name, dir_fd=fd)
            return True
    except FileNotFoundError:
        return False


@contextmanager
def record_lock(root, relative):
    """Persistent lock inode: never unlink it, which would split waiting writers."""
    path = Path(relative)
    with directory(root, path.parent, create=True) as parent:
        regular(parent, path.name)
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(path.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        except FileExistsError:
            # Open the same inode created by the winner; don't recreate/replace it.
            fd = os.open(path.name, flags, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise StateError(f'Invalid lock file: {relative}')
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)
