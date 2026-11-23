"""Descriptor-based observations of files in an approved external store."""

from contextlib import ExitStack, contextmanager
import hashlib
import os
from pathlib import Path
import stat


CHUNK_SIZE = 1024 * 1024


def absolute_path(raw):
    if (not isinstance(raw, str) or not raw.startswith("/") or "\0" in raw
            or any(part in {"", ".", ".."} for part in raw.split("/")[1:])):
        raise ValueError("STORE paths must be canonical absolute paths without traversal")
    return Path(raw)


def identity(info):
    return (info.st_dev, info.st_ino)


def stamp(info):
    return (*identity(info), info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def walk(path, root, declaration, guard, *, directory=False):
    """Pin each component, with policy checks before its descriptor is opened."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with ExitStack() as stack:
        parent = os.open("/", flags | os.O_DIRECTORY)
        stack.callback(os.close, parent)
        current = Path("/")
        opened = []
        for part in path.parts[1:]:
            current /= part
            guard(current)
            is_directory = directory or current != path
            expected = None
            if not is_directory:
                expected = os.stat(part, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(expected.st_mode) or expected.st_nlink != 1:
                    raise ValueError(f"STORE file must be regular and have one link: {current}")
            parent = os.open(part, flags | (os.O_DIRECTORY if is_directory else 0), dir_fd=parent)
            stack.callback(os.close, parent)
            info = os.fstat(parent)
            if current == root and identity(info) != (declaration["device"], declaration["inode"]):
                raise ValueError(f"STORE root identity changed: {root}")
            if current.is_relative_to(root) and info.st_dev != declaration["device"]:
                raise ValueError(f"Unexpected filesystem inside STORE: {current}")
            if expected is not None and stamp(info) != stamp(expected):
                raise ValueError(f"STORE file changed while opening: {current}")
            opened.append((parent, current, info))
        yield opened


def inspect_root(declaration, guard):
    root = absolute_path(declaration["path"])
    try:
        with walk(root, root, declaration, guard, directory=True):
            pass
    except OSError as error:
        raise ValueError(f"Cannot open STORE root without following links: {root}") from error


def chain_stamp(nodes, root, *, current=False, times=True):
    result = []
    for fd, path, before in nodes:
        info = os.fstat(fd) if current else before
        if times and path.is_relative_to(root):
            result.append(stamp(info))
        else:
            result.append(identity(info))
    return result


def observe(raw, declaration, guard, *, command_input=False, hash_contents=True):
    path, root = absolute_path(raw), absolute_path(declaration["path"])
    if path == root or not path.is_relative_to(root):
        raise ValueError(f"File is outside its declared STORE: {path}")
    try:
        with walk(path, root, declaration, guard) as opened:
            fd, _, info = opened[-1]
            original = chain_stamp(opened, root)
            digest = stream_hash(fd, path, info.st_size) if hash_contents else None
            with walk(path, root, declaration, guard) as reopened:
                if (original != chain_stamp(reopened, root, current=True)
                        or original != chain_stamp(opened, root, current=True)):
                    raise ValueError(f"STORE path changed while reading: {path}")
            version = {"size": info.st_size, "device": info.st_dev, "inode": info.st_ino,
                       "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
                       "directories": [list(entry) for entry in chain_stamp(opened[:-1], root, times=command_input)]}
            if digest is not None:
                version["sha256"] = digest
            return version
    except OSError as error:
        raise ValueError(f"Cannot open STORE file without following links: {path}") from error


def stream_hash(fd, path, remaining):
    digest = hashlib.sha256()
    while remaining:
        chunk = os.read(fd, min(remaining, CHUNK_SIZE))
        if not chunk:
            raise ValueError(f"STORE file changed while reading: {path}")
        remaining -= len(chunk)
        digest.update(chunk)
    if os.read(fd, 1):
        raise ValueError(f"STORE file grew while reading: {path}")
    return digest.hexdigest()
