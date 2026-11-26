#!/usr/bin/env python3
"""Capture source state and validate immutable workflow artifacts without running gates."""

import argparse
import codecs
from contextlib import contextmanager, ExitStack
import difflib
import fcntl
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import unicodedata


DEFAULT_EXCLUDES = (".claude/review-findings.json", ".claude/review-applied.json")
MISSING = {"kind": "missing"}
SEVERITIES = {"Critical", "Warning", "Suggestion"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def contract_read_policy(path):
    """Read the declared exclusions or refuse a policy that cannot be enforced."""
    try:
        contract = load(path)
    except (json.JSONDecodeError, FileNotFoundError):
        return ()
    if not isinstance(contract, dict):
        return ()
    return read_policy_input(contract)


def read_policy_input(contract):
    """Validate an explicit exclusion policy and return the guarded roots."""
    if not isinstance(contract, dict):
        raise ValueError("Invalid read policy")
    exclusions = contract.get("read_excludes", {})
    require(isinstance(exclusions, dict), "Invalid read_excludes")
    roots = contract.get("repo_paths", {})
    require(isinstance(roots, dict), "Invalid repository paths")
    policies = []
    for repo, paths in exclusions.items():
        require(isinstance(paths, list) and all(isinstance(path, str) for path in paths), "Invalid read_excludes")
        root = roots.get(repo)
        require(isinstance(root, str) and Path(root).is_absolute(), "Invalid read_excludes repository")
        for excluded in paths:
            relative_path(excluded)
        if paths:
            policies.append((root, tuple(paths)))
    inherited = contract.get("inherited_read_policy")
    if inherited is not None:
        require(isinstance(inherited, dict) and set(inherited) == {"repo_paths", "read_excludes"},
                "Invalid inherited read policy")
        policies.extend(read_policy_input(inherited))
    merged = {}
    for root, paths in policies:
        merged.setdefault(root, set()).update(paths)
    return tuple(sorted((root, tuple(sorted(paths))) for root, paths in merged.items()))


def publish_once(path, writer, *, pinned=False, temporary_path=None, sync=False, mode=0o666):
    path = Path(path)
    if not pinned:
        path.parent.mkdir(parents=True, exist_ok=True)
        path = path.parent.resolve() / path.name
    temporary = Path(temporary_path) if temporary_path else path.parent / (".ship-write-" + secrets.token_hex(16) + ".tmp")
    require(temporary.parent == path.parent, "Temporary file must share the destination directory")
    with open_directory(path.parent, create=True) as parent:
        created = False
        try:
            fd = os.open(temporary.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
            created = True
            with os.fdopen(fd, "wb") as handle:
                writer(handle)
                if sync:
                    handle.flush()
                    os.fsync(handle.fileno())
            os.link(temporary.name, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            if sync:
                os.fsync(parent)
        finally:
            if created:
                os.unlink(temporary.name, dir_fd=parent)


def write_once(path, value, *, pinned=False):
    payload = (json.dumps(value, indent=2, ensure_ascii=True) + "\n").encode()
    publish_once(path, lambda handle: handle.write(payload), pinned=pinned)


def init_run(run_dir, workflow, repos, description, plan_path, session_id):
    root = Path(run_dir).resolve()
    relative_path(root.name)
    require(workflow in {"ship", "fix"}, "Invalid workflow")
    require(root.parent == (Path.cwd() / "plans" / f".{workflow}-runs").resolve(),
            "Run directory must be directly under the workflow run directory")
    require(repos and len(repos) == len(set(repos)), "Expected unique repositories")
    for repo in repos:
        relative_path(repo)
    text(description, "description")
    require(workflow != "ship" or (plan_path and Path(plan_path).is_absolute() and Path(plan_path).is_file()),
            "Ship needs an existing absolute plan path")
    record = {"schema_version": 2, "workflow": workflow, "run_id": root.name,
              "session_id": session_id or "", "started_at": datetime.now(timezone.utc).isoformat(),
              "repos": repos, "description": description}
    if plan_path:
        record["plan_path"] = plan_path
    payload = (json.dumps(record, indent=2, ensure_ascii=True) + "\n").encode()
    root.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir()
    publish_once(root / "run.json", lambda handle: handle.write(payload), pinned=True, mode=0o600)
    return record


def validate_run(contract_path, attempt_id, workflow):
    contract_file = Path(contract_path).absolute()
    policies = contract_read_policy(contract_file)
    resolved_contract = guard_excluded_paths(contract_file, policies, pinned=bool(policies))
    if not policies:
        resolved_contract = resolved_contract.resolve()
    root = resolved_contract.parent
    guard = lambda path: guard_excluded_paths(path.absolute(), policies, pinned=bool(policies))
    require(not guarded_exists(root / "setup-replacement.json", policies), "This setup run has been replaced")
    relative_path(attempt_id)
    require(re.fullmatch(r"[A-Za-z0-9_.-]+", attempt_id) is not None, "Invalid setup attempt ID")
    metadata_path = guard(root / "run.json")
    if policies:
        with capture_open(metadata_path) as handle:
            meta = json.load(handle)
    else:
        meta = load(metadata_path)
    require(isinstance(meta, dict) and meta.get("schema_version") == 2
            and meta.get("run_id") == root.name and meta.get("workflow") in {"ship", "fix"},
            "Invalid run metadata")
    for repo in meta.get("repos", []):
        relative_path(repo)
        reservation = root / repo / "successor-reservation.json"
        require(not guarded_exists(reservation, policies),
                "Predecessor has been reserved for a successor")
    identifier = "setup-" + attempt_id
    session = os.environ.get("CODEX_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or ""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session):
        session = ""
    start = {"schema_version": 2, "run_id": meta["run_id"], "workflow": meta["workflow"],
             "repo": None, "phase_id": identifier, "phase": "setup", "event": "phase-start",
             "at": datetime.now(timezone.utc).isoformat(), "session_id": session}
    start_path = guard(root / "telemetry" / (identifier + "-start.json"))
    end_path = guard(root / "telemetry" / (identifier + "-end.json"))
    require(not guarded_exists(end_path, policies), "Setup attempt already exists")
    write_once(start_path, start, pinned=bool(policies))
    if guarded_exists(contract_file, policies):
        contract_source = guard(contract_file)
        opener = capture_open(contract_source) if policies else open(contract_source, "rb")
        with opener as contract_input:
            contract_hash = hashlib.file_digest(contract_input, "sha256").hexdigest()
        seal = guard(root / "setup-seals" / (identifier + ".json"))
        write_once(seal, {"schema_version": 2, "run_id": meta["run_id"], "phase_id": identifier,
                          "contract_sha256": contract_hash}, pinned=bool(policies))
    try:
        value = workflow.contract(contract_path)
        require(value["run_id"] == meta["run_id"] and value["workflow"] == meta["workflow"],
                "Run metadata/contract identity mismatch")
        require(value["repos"] == meta.get("repos"), "Run metadata/contract repositories mismatch")
        workflow.validate_store_roots(value)
        if value["workflow"] == "ship":
            require(value.get("plan_path") == meta.get("plan_path"), "Run metadata/contract plan mismatch")
        schedules = value.get("slice_schedules", {})
        require(isinstance(schedules, dict) and set(schedules) <= set(value["repos"]), "Invalid slice schedules")
        for repo, declaration in schedules.items():
            require(isinstance(declaration, dict), "Invalid slice schedule declaration")
            workflow.checked_slice_schedule(value, repo, declaration.get("path"))
    except Exception as error:
        end = {**start, "event": "phase-end", "at": datetime.now(timezone.utc).isoformat(),
               "status": "failed", "end_reason": "unknown", "error": str(error),
               "start_sha256": hashlib.sha256(capture_bytes(start_path) if policies else start_path.read_bytes()).hexdigest()}
        write_once(end_path, end, pinned=bool(policies))
        raise
    end = {**start, "event": "phase-end", "at": datetime.now(timezone.utc).isoformat(),
           "status": "passed", "start_sha256": hashlib.sha256(capture_bytes(start_path) if policies else start_path.read_bytes()).hexdigest()}
    write_once(end_path, end, pinned=bool(policies))
    return end


def relative_path(value):
    require(isinstance(value, str) and bool(value), "Expected a nonempty relative path")
    path = PurePosixPath(value)
    require(not path.is_absolute() and ".." not in path.parts and ".git" not in path.parts
            and str(path) == value and value != ".", f"Unsafe relative path: {value}")
    return value


def safe_path(root, value, *, guard=Path):
    parts = relative_path(value).split("/")
    path = Path(root).joinpath(*parts)
    guard(path)
    # A final symlink is source data; a symlinked parent would escape the scoped tree.
    parent = Path(root)
    for part in parts[:-1]:
        parent /= part
        require(not parent.is_symlink(), f"Symlinked parent: {parent}")
    if any(spelling_key(part) == ".git" for part in value.split("/")):
        PathNames(root).key(value)
    return path


def guard_excluded_path(path, root, read_excludes, *, pinned=False):
    return guard_excluded_paths(path, ((Path(root).absolute(), read_excludes),) if read_excludes else (), pinned=pinned)


@contextmanager
def open_directory(path, *, create=False):
    """Open an absolute directory one component at a time without following links."""
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "Expected a resolved directory path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def capture_lstat(path):
    if path == path.parent:
        return path.lstat()
    with open_directory(path.parent) as parent:
        return os.stat(path.name, dir_fd=parent, follow_symlinks=False)


def capture_readlink(path):
    with open_directory(path.parent) as parent:
        return os.readlink(path.name, dir_fd=parent)


@contextmanager
def capture_open(path, expected=None):
    with open_directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode) and (expected is None or os.path.samestat(info, expected)),
                "Source or metadata changed while opening a file")
        yield handle


def capture_bytes(path):
    with capture_open(path) as handle:
        return handle.read()


def guard_excluded_paths(path, policies, *, scope=False, pinned=False):
    """Resolve symlinks one component at a time without probing an excluded path."""
    if not policies:
        return Path(path)
    return resolve_guarded(path, policy_tables(policies, pinned), scope=scope)


def guarded_exists(path, policies):
    path = Path(path).absolute()
    if not policies:
        return path.exists() or path.is_symlink()
    guard_excluded_paths(path, policies, pinned=True)
    parent = guard_excluded_paths(path.parent, policies, pinned=True)
    try:
        with open_directory(parent) as fd:
            os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def output_run_root(path, policies=()):
    if not path:
        return None
    candidate = Path(path).absolute()
    guarded = guard_excluded_paths(candidate, policies, pinned=bool(policies))
    if not policies:
        guarded = guarded.resolve()
    for output in (guarded, candidate):
        for parent in (output, *output.parents):
            if parent.parent.name in {".ship-runs", ".fix-runs"}:
                return parent
    for parent in (guarded, *guarded.parents):
        contract = parent / "contract.json"
        if not guarded_exists(contract, policies):
            continue
        source = guard_excluded_paths(contract, policies, pinned=bool(policies))
        try:
            if policies:
                with capture_open(source) as handle:
                    value = json.load(handle)
            else:
                value = load(source)
        except (ValueError, OSError):
            continue
        if (isinstance(value, dict) and value.get("schema_version") == 2
                and value.get("workflow") in {"ship", "fix"} and value.get("run_id") == parent.name):
            return parent
    return None


def policy_tables(policies, pinned):
    merged = {}
    for root, exclusions in policies:
        merged.setdefault(Path(root), set()).update(exclusions)
    pending = [PathNames(root, tuple(paths), pinned=pinned) for root, paths in merged.items() if paths]
    names = list(pending)
    while pending:
        unresolved = []
        for table in pending:
            # Another declared root may be an alias. Defer any candidate that might enter its
            # exclusions until that root can be resolved without crossing a protected path.
            unknown = [entry for entry in pending if entry is not table]
            root = resolve_guarded(table.root, names, unknown)
            if root is None:
                unresolved.append(table)
            elif root != table.root:
                names.append(PathNames(root, table.read_excludes, pinned=pinned))
        require(len(unresolved) < len(pending), "Cannot resolve repository roots without crossing read_excludes")
        pending = unresolved
    return names


def same_path_prefix(path, other, *, pinned=False):
    length = min(len(path.parts), len(other.parts))
    prefix, other_prefix = path.parts[:length], other.parts[:length]
    if prefix == other_prefix:
        return True
    actual, expected = Path(*prefix), Path(*other_prefix)
    if spelling_key(str(actual)) != spelling_key(str(expected)):
        return False
    root_names = PathNames(expected.anchor, (expected.relative_to(expected.anchor).as_posix(),), pinned=pinned)
    return root_names.excluded(actual.relative_to(expected.anchor).as_posix())


def check_path_exclusions(path, names, *, scope=False):
    for table in names:
        if not same_path_prefix(path, table.root, pinned=table.pinned):
            continue
        relative = "/".join(path.parts[len(table.root.parts):])
        if relative:
            excluded = table.excluded(relative, include_ancestors=scope)
        else:
            excluded = scope
        require(not excluded, "Path intersects read_excludes")


def resolve_guarded(path, names, unknown_roots=(), *, scope=False):
    path = Path(path)
    require(path.is_absolute(), "Expected an absolute path")
    inspect = capture_lstat if any(table.pinned for table in names) else Path.lstat
    readlink = capture_readlink if any(table.pinned for table in names) else os.readlink
    pending = list(path.parts[1:])
    current = Path(path.anchor)
    links = 0
    while pending:
        part = pending.pop(0)
        if part == ".":
            continue
        if part == "..":
            current = current.parent
            continue
        current = current / part
        check_path_exclusions(current, names, scope=scope and not pending)
        if any(spelling_key(current.as_posix()).endswith("/" + spelling_key(exclusion))
               for table in unknown_roots for exclusion in table.read_excludes):
            return None
        try:
            info = inspect(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            links += 1
            require(links <= 40, "Too many symlinks")
            target = Path(readlink(current))
            current = Path(target.anchor) if target.is_absolute() else current.parent
            pending = list(target.parts[1:] if target.is_absolute() else target.parts) + pending
    if scope:
        check_path_exclusions(current, names, scope=True)
    return current


def scope_paths(path, *, pinned=False):
    values = json.loads(capture_bytes(path)) if pinned else load(path)
    require(isinstance(values, list), "Scope must be a JSON array")
    return sorted({relative_path(value) for value in values})


def file_state(path, *, pinned=False):
    try:
        info = capture_lstat(path) if pinned else path.lstat()
    except FileNotFoundError:
        return {"kind": "missing"}
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        return {"kind": "symlink", "target": capture_readlink(path) if pinned else os.readlink(path)}
    require(stat.S_ISREG(info.st_mode), f"Unsupported source kind: {path}")
    if pinned:
        with capture_open(path, info) as handle:
            sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
    else:
        sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"kind": "file", "execute_bits": mode & 0o111, "sha256": sha256}


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def capture_once(root, scope, excludes, read_excludes=(), *, policies=()):
    explicit = {relative_path(p) for p in scope}
    forbidden = tuple(relative_path(p) for p in read_excludes)
    root = Path(root).absolute()
    declarations = (*policies, (root, forbidden)) if forbidden else policies
    tables = policy_tables(declarations, True)
    pinned = bool(tables)
    root = resolve_guarded(root, tables) if pinned else root.resolve()
    require(directory_status(root, pinned), f"Missing repo directory: {root}")
    names = PathNames(root, forbidden, pinned=pinned)
    tables = (names, *tables)

    def forbidden_path(path):
        return names.excluded(path, include_ancestors=True)

    require(not any(forbidden_path(p) for p in explicit), "Source scope intersects read_excludes")
    if pinned:
        for path in explicit:
            resolved = resolve_guarded(root / path, tables, scope=True)
            require(resolved.is_relative_to(root), "Source scope escapes repository through a symlink")
    tracked, others, head = set(), set(), None
    # A nested directory without its own .git remains a non-Git capture.
    git_marker = root / ".git"
    if pinned:
        guarded_marker = resolve_guarded(git_marker, tables)
        try:
            capture_lstat(guarded_marker)
        except FileNotFoundError:
            require(guarded_marker == git_marker, "Invalid Git directory pointer")
            require(bool(scope), "Non-git capture requires explicit source/dependency scope")
        else:
            tracked, others, head = restricted_git_capture(names, git_marker, tables)
    elif git_marker.exists():
        tracked = git_paths(root, "--cached")
        others = nul_paths(git(root, "ls-files", "-z", "--others", "--exclude-standard"))
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        head = result.stdout.decode().strip() if result.returncode == 0 else None
    else:
        require(bool(scope), "Non-git capture requires explicit source/dependency scope")
    exclusions = [relative_path(p) for p in (*DEFAULT_EXCLUDES, *excludes)]

    preferred = {}
    for path in sorted(tracked):
        key = names.key(path)
        require(key not in preferred, f"Git tracks multiple names for the same entry: {path}")
        preferred[key] = path
    for path in explicit | others:
        if not forbidden_path(path) and (path in explicit or not names.intersects(path, exclusions)):
            key = names.key(path)
            preferred.setdefault(key, key)
    files = {p: file_state(root / p, pinned=True) if pinned else file_state(safe_path(root, p))
             for p in sorted(preferred.values())}
    payload = {"repo_path": str(root), "head": head, "files": files}
    return {**payload, "fingerprint": digest(payload)}


def capture(root, scope, excludes=(), read_excludes=(), *, policies=()):
    first = capture_once(root, scope, excludes, read_excludes, policies=policies)
    second = capture_once(root, scope, excludes, read_excludes, policies=policies)
    require(first == second, "Source changed during capture; stabilize and retry")
    return first


def checked_manifest(path, *, loader=load):
    value = loader(path)
    require(isinstance(value, dict) and isinstance(value.get("files"), dict), "Invalid manifest")
    payload = {key: value[key] for key in ("repo_path", "head", "files")}
    require(value.get("fingerprint") == digest(payload), "Invalid manifest fingerprint")
    return value


def project_ignored_dependencies(path, dependencies):
    """Project a historical manifest to slice scope while checking ignored inputs remain unchanged."""
    original = checked_manifest(path)
    root = Path(original["repo_path"])
    def scan():
        names = PathNames(root)
        paths = names.keys(scope_paths(dependencies))
        original_files = names.files(original["files"])
        require(paths and paths <= original_files.keys(), "Dependencies must exist in the original manifest")
        require(not paths & names.keys(git_paths(root, "--cached")), "Cannot project tracked source files")
        result = {}
        for name in sorted(paths):
            ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "--", name])
            require(ignored.returncode == 0, f"Dependency is not ignored: {name}")
            result[name] = file_state(safe_path(root, name))
        expected = {name: original_files[name] for name in paths}
        files = {name: value for name, value in original["files"].items() if names.key(name) not in paths}
        return result, expected, files

    first, expected, files = scan()
    require((first, expected, files) == scan(), "Dependencies changed during projection")
    require(first == expected, "Dependency changed since initial manifest")
    payload = {"repo_path": original["repo_path"], "head": original["head"], "files": files}
    return {**payload, "fingerprint": digest(payload)}


def match(left, right):
    require(left["repo_path"] == right["repo_path"], "Manifests refer to different repositories")
    require(left["fingerprint"] == right["fingerprint"], "Stale certification: source states differ")


def git_paths(root, *args):
    return nul_paths(git(root, "ls-files", "-z", *args))


def nul_paths(output):
    return {os.fsdecode(path) for path in output.split(b"\0") if path}


def isolated_git_env():
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0")
    return env


def directory_status(path, pinned):
    try:
        info = capture_lstat(path) if pinned else path.stat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(info.st_mode)


def git_directories(marker, guard):
    spelling = marker
    git_dir = guard(marker)
    if not directory_status(git_dir, True) and guarded_file(git_dir, guard) is not None:
        pointer = os.fsdecode(capture_bytes(git_dir)).rstrip("\r\n")
        require(pointer.startswith("gitdir: ") and pointer[8:], "Invalid Git directory pointer")
        # Relative gitdir pointers are relative to the worktree entry, including a symlinked gitfile.
        git_dir = guard(marker.parent / pointer[8:])
        spelling = git_dir
    require(directory_status(git_dir, True), "Invalid Git directory")
    common_pointer = guarded_file(git_dir / "commondir", guard)
    common = git_dir
    if common_pointer is not None:
        target = os.fsdecode(capture_bytes(common_pointer)).rstrip("\r\n")
        require(bool(target), "Invalid Git common directory pointer")
        common = guard(git_dir / target)
        require(directory_status(common, True), "Invalid Git common directory")
    return git_dir, common, spelling


def exclude_index_entry(path):
    # A literal pathspec also matches descendants. Force an exact glob for a file
    # that shadows an excluded descendant, leaving other stages below it visible.
    tail = path[-1:]
    if tail in (b"\\", b"[", b"]", b"-", b"!", b"^"):
        tail = b"\\" + tail
    prefix = re.sub(rb"([*?\[\]\\])", rb"\\\1", path[:-1])
    return ":(top,exclude,glob)" + os.fsdecode(prefix + b"[" + tail + b"]")


def isolated_index_exclusions(data, oid_length, names, pathspecs):
    """Match index spellings to exclusion prefixes before Git can emit any tracked names.

    Git's literal pathspecs compare bytes and its icase pathspecs only fold ASCII.
    Inspect the private index snapshot with the same spelling policy as the walk.
    Keep only matched boundary prefixes and the split-index pointer, never a path list.
    """
    end = len(data) - oid_length
    require(end >= 12 and data[:4] == b"DIRC", "Invalid Git index header")
    version, count = struct.unpack_from(">II", data, 4)
    require(version in (2, 3, 4), "Unsupported Git index version")
    cursor, previous = 12, b""
    sparse, collapsed = False, {}
    depths = sorted({name.count("/") + 1 for name in names.read_excludes})
    for _ in range(count):
        start = cursor
        flags_at = start + 40 + oid_length
        require(flags_at + 2 <= end, "Truncated Git index entry")
        flags = struct.unpack_from(">H", data, flags_at)[0]
        cursor = flags_at + (4 if flags & 0x4000 else 2)
        strip = 0
        if version == 4:
            while True:
                require(cursor < end and strip <= len(previous), "Invalid Git index path compression")
                byte = data[cursor]
                cursor += 1
                strip = strip * 128 + (byte & 0x7f)
                if not byte & 0x80:
                    break
                strip += 1
            require(strip <= len(previous), "Invalid Git index path prefix")
        stop = data.find(b"\0", cursor, end)
        require(stop >= 0, "Unterminated Git index entry")
        previous = previous[:len(previous) - strip] + data[cursor:stop] if version == 4 else data[cursor:stop]
        require(min(len(previous), 0xfff) == (flags & 0xfff), "Invalid Git index path length")
        cursor = stop + 1 if version == 4 else start + ((stop + 8 - start) & ~7)
        if previous:  # Split-index replacements may inherit a name from the shared index.
            is_sparse = previous.endswith(b"/")
            sparse |= is_sparse
            raw = previous.removesuffix(b"/")
            require(not {b"", b".", b"..", b".git"}.intersection(raw.split(b"/")), "Unsafe Git index path")
            for depth in depths:
                components = raw.split(b"/", depth)
                boundary = os.fsdecode(b"/".join(components[:depth]))
                prefix = names.excluded_prefix(boundary)
                if prefix is not None:
                    pathspecs.add(f":(top,exclude,literal){prefix}")
                    break
            else:
                if is_sparse:
                    name = os.fsdecode(raw)
                    pathspecs.add(f":(top,exclude,literal){name}")
                    collapsed[name] = data[flags_at - oid_length:flags_at].hex()
                elif depths and len(components) <= depth and names.excluded(boundary, include_ancestors=True):
                    pathspecs.add(exclude_index_entry(raw))
    shared = None
    while cursor < end:
        require(cursor + 8 <= end, "Truncated Git index extension")
        kind, size = struct.unpack_from(">4sI", data, cursor)
        cursor += 8
        require(cursor + size <= end, "Truncated Git index extension payload")
        if kind == b"link":
            require(shared is None and size >= oid_length, "Invalid split Git index extension")
            shared = data[cursor:cursor + oid_length]
        cursor += size
    require(cursor == end, "Invalid Git index padding")
    return shared.hex() if shared and any(shared) else None, sparse, collapsed


def guarded_file(path, guard):
    path = guard(path)
    if path == Path(os.devnull):
        # Git permits this exact device to disable configuration or ignore files.
        return None
    try:
        mode = capture_lstat(path).st_mode
    except FileNotFoundError:
        return None
    require(stat.S_ISREG(mode), "Expected a regular Git metadata file")
    return path


def git_config_path(value, parent):
    value = os.fsdecode(value)
    require(not value.startswith("%(prefix)"), "Unsupported Git config path interpolation")
    expanded = os.path.expanduser(value)
    require(not expanded.startswith("~"), "Cannot resolve Git config path")
    return parent / expanded


def git_wildmatch(pattern, value, *, fold=False, pathname=True):
    """Match Git condition globs, keeping ordinary wildcards inside a component."""
    pattern, value = os.fsencode(pattern), os.fsencode(value)
    if fold:
        pattern, value = pattern.lower(), value.lower()
    tokens = re.finditer(rb"\\.|\[(?:\[:[a-z]+:\]|[^\]])+\]|\*+|\?|.", pattern, re.DOTALL)
    regex, skip = [], 0
    for match in tokens:
        if match.start() < skip:
            continue
        token = match[0]
        if token == b"?" or token.startswith(b"*"):
            if token == b"?":
                regex.append(b"[^/]" if pathname else b".")
            elif not pathname:
                regex.append(b".*")
            elif (len(token) > 1 and (match.start() == 0 or pattern[match.start() - 1] == 47) and
                  (match.end() == len(pattern) or pattern[match.end()] == 47)):
                if match.end() == len(pattern):
                    regex.append(b".*")
                else:
                    regex.append(b"(?:[^/]*/)*")
                    skip = match.end() + 1
            else:
                regex.append(b"[^/]*")
        elif token.startswith(b"[") and len(token) > 1:
            classes = {b"alnum": b"A-Za-z0-9", b"alpha": b"A-Za-z", b"blank": b" \t",
                       b"digit": b"0-9", b"lower": b"a-z", b"space": b" \t\r\n\f\v",
                       b"upper": b"A-Z", b"xdigit": b"A-Fa-f0-9", b"cntrl": b"\x00-\x1f\x7f",
                       b"graph": b"!-~", b"print": b" -~", b"punct": rb"!-/: -@\[-`{-~".replace(b" ", b"")}
            body = token[1:-1]
            negated = body.startswith((b"!", b"^"))
            body = body[1:] if negated else body
            body = re.sub(rb"\[:([a-z]+):\]", lambda m: classes.get(m[1], rb"\b\B"), body)
            if negated:
                body = b"^" + body
            regex.append((b"(?!/)" if pathname else b"") + b"[" + body + b"]")
        else:
            regex.append(re.escape(token[-1:]))
    try:
        return re.fullmatch(b"".join(regex), value, re.DOTALL) is not None
    except re.error:
        return False


def unquote_git_path(path):
    if path.startswith(b'"'):
        require(path.endswith(b'"'), "Invalid quoted Git object directory")
        return codecs.escape_decode(path[1:-1])[0]
    return path


def tree_entries(data, oid_length):
    position = 0
    while position < len(data):
        space = data.find(b" ", position)
        end = data.find(b"\0", position)
        require(position < space < end and end + oid_length < len(data), "Invalid Git tree entry")
        mode, name = data[position:space], data[space + 1:end]
        require(b"/" not in name and name not in {b"", b".", b"..", b".git"}, "Unsafe Git tree entry")
        require(mode in {b"40000", b"100644", b"100755", b"120000", b"160000"}, "Unsupported Git tree mode")
        position = end + 1 + oid_length
        yield mode, os.fsdecode(name), data[end + 1:position].hex()


def guarded_git_executable(cwd, guard):
    for directory in os.get_exec_path():
        candidate = guard(cwd / directory / "git")
        try:
            mode = capture_lstat(candidate).st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISREG(mode) and mode & 0o111:
            return str(candidate)
    raise ValueError("Cannot find a permitted Git executable")


class CaptureGit:
    """Run Git against a private index, refs and ignore files with no live repository pointers."""

    def __init__(self, scratch, names, tables):
        self.names = names
        self.tables = tables
        self.gitdir = scratch / "git"
        self.common = self.gitdir
        self.worktree = scratch / "worktree"
        self.worktree.mkdir()
        (self.gitdir / "objects").mkdir(parents=True)
        (self.gitdir / "refs").mkdir()
        (self.gitdir / "HEAD").write_bytes(b"ref: refs/heads/unused\n")
        self.env = isolated_git_env()
        self.parsed = {}
        self.condition_kinds = {}
        self.git = guarded_git_executable(self.worktree, self.guard)

    def guard(self, path):
        return resolve_guarded(path, self.tables)

    def guarded_file(self, path):
        return guarded_file(path, self.guard)

    def run(self, *args, **kwargs):
        return subprocess.run([self.git, "--git-dir", str(self.gitdir), "--work-tree", str(self.worktree), *args],
                              cwd=self.worktree, env=self.env, capture_output=True, **kwargs)

    def config(self, path):
        config = self.guarded_file(path)
        if config is None:
            return path, []
        if config in self.parsed:
            return config, self.parsed[config]
        result = self.run("config", "--file", "-", "--no-includes", "--null", "--list",
                          input=capture_bytes(config))
        require(result.returncode == 0, "Cannot read Git config for restricted capture")
        entries = [entry.partition(b"\n") for entry in result.stdout.split(b"\0") if entry]
        self.parsed[config] = [(key, value if separator else b"true") for key, separator, value in entries]
        return config, self.parsed[config]

    def config_path(self, value, parent):
        if value.startswith(b"%(prefix)/"):
            quoted = value.replace(b"\\", b"\\\\").replace(b'"', b'\\"').replace(b"\n", b"\\n")
            result = self.run("config", "--file", "-", "--path", "--get", "ship.path",
                              input=b'[ship]\npath = "' + quoted + b'"\n')
            require(result.returncode == 0, "Cannot expand Git config path")
            return parent / os.fsdecode(result.stdout.removesuffix(b"\n"))
        return git_config_path(value, parent)

    def system_config(self):
        disabled = os.environ.get("GIT_CONFIG_NOSYSTEM", "").lower()
        if disabled in {"1", "true", "yes", "on"}:
            return None
        if "GIT_CONFIG_SYSTEM" in os.environ:
            return self.names.root / os.environ["GIT_CONFIG_SYSTEM"]
        # An isolated `git var` suppresses disclosure of its default path. Discover the
        # build constant without allowing Git to open the live config to report it.
        reported = self.run("--exec-path")
        require(reported.returncode == 0, "Cannot find the Git installation")
        installed = Path(os.fsdecode(reported.stdout.removesuffix(b"\n")))
        binary = installed.parent.parent / "bin/git"
        if self.guarded_file(binary) is None:
            binary = Path(self.git)
        executable = self.guarded_file(binary)
        require(executable is not None, "Cannot read the Git executable")
        candidates = set(re.findall(rb"(?:^|\0)((?:/|etc/)[^\0\r\n\t]{0,512}/?gitconfig)(?=\0)",
                                    capture_bytes(executable)))
        # An Xcode Git launcher can delegate to a platform Git that uses /etc.
        if not candidates and sys.platform == "darwin" and binary == Path("/usr/bin/git"):
            candidates = {b"/etc/gitconfig"}
        require(len(candidates) == 1, "Cannot find the system Git config safely; set GIT_CONFIG_SYSTEM")
        path = candidates.pop()
        return self.config_path(path if path.startswith(b"/") else b"%(prefix)/" + path, self.names.root)

    def supports_condition(self, name):
        if name not in self.condition_kinds:
            probe = self.gitdir / "include-probe"
            probe.write_bytes(b"[ship]\nmatched = yes\n")
            # The only possible included file is generated inside the private Git directory.
            config = b'[includeIf "' + name + b':**"]\npath = ' + os.fsencode(probe) + b"\n"
            result = self.run("config", "--file", "-", "--includes", "--get", "ship.matched", input=config)
            self.condition_kinds[name] = result.returncode == 0 and result.stdout == b"yes\n"
        return self.condition_kinds[name]

    def effective_config(self, sources, git_dirs, branch):
        remote_prefix = b"hasconfig:remote.*.url:"

        def condition(raw, source, remotes):
            key, _, value = raw.partition(b":")
            if key in (b"gitdir", b"gitdir/i", b"worktree", b"worktree/i"):
                if key.startswith(b"worktree") and not self.supports_condition(key):
                    return False
                paths = git_dirs if key.startswith(b"gitdir") else (self.names.root,)
                return any(match_path(key, value, source, actual) for actual in paths)
            if key == b"onbranch":
                return branch is not None and git_wildmatch(value + (b"**" if value.endswith(b"/") else b""), branch)
            if key == b"hostname":
                return self.supports_condition(key) and git_wildmatch(value, socket.gethostname(), pathname=False)
            if raw.startswith(remote_prefix):
                return any(git_wildmatch(raw[len(remote_prefix):], url) for url in remotes)
            return False

        def match_path(key, value, source, actual):
            if value.startswith(b"./"):
                base = str(source.parent) + "/"
                text = str(actual)
                left, right = os.fsencode(text), os.fsencode(base)
                if key.endswith(b"/i"):
                    left, right = left.lower(), right.lower()
                if not left.startswith(right):
                    return False
                text, pattern = text[len(base):], os.fsdecode(value[2:])
            else:
                text = str(actual)
                pattern = os.fsdecode(value)
                if pattern.startswith("~"):
                    user, separator, suffix = pattern.partition("/")
                    home = os.path.expanduser(user)
                    if not home.startswith("~"):
                        pattern = str(self.guard(Path(home).absolute())) + separator + suffix
                elif pattern.startswith("%(prefix)/"):
                    expanded = str(self.config_path(value, actual))
                    pattern = expanded + ("/" if pattern.endswith("/") else "")
                if not pattern.startswith("/"):
                    pattern = "**/" + pattern
            return git_wildmatch(pattern + ("**" if pattern.endswith("/") else ""), text,
                                 fold=key.endswith(b"/i"))

        def entries(paths, remotes, parents=(), found_by_url=False):
            for path in paths:
                source, rows = self.config(path)
                require(source not in parents and len(parents) <= 10, "Cyclic or deeply nested Git config includes")
                for key, value in rows:
                    match = key.removeprefix(b"includeif.").removesuffix(b".path")
                    by_url = key.startswith(b"includeif." + remote_prefix)
                    if key == b"include.path" or (key.startswith(b"includeif.") and key.endswith(b".path") and
                                                  condition(match, source, remotes)):
                        included = self.config_path(value, path.parent)
                        yield from entries([included], remotes, (*parents, source), found_by_url or by_url)
                    elif key.startswith(b"remote.") and key.endswith(b".url"):
                        require(not found_by_url, "Remote URLs are forbidden inside hasconfig includes")
                        yield key, value
                    elif not key.startswith(b"includeif."):
                        yield key, value

        remotes = [value for key, value in entries(sources, ()) if key.startswith(b"remote.") and key.endswith(b".url")]
        return dict(entries(sources, remotes))

    def tracked(self, index, git_dir, common, algorithm):
        excludes = {f":(top,exclude,literal){name}" for name in self.names.read_excludes}
        options, sparse_flag = [], []
        collapsed = {}
        if index is not None:
            oid_length = 32 if algorithm == b"sha256" else 20
            data = capture_bytes(index)
            shared, sparse, collapsed = isolated_index_exclusions(data, oid_length, self.names, excludes)
            (self.gitdir / "index").write_bytes(data)
            if shared:
                name = f"sharedindex.{shared}"
                shared_index = self.guarded_file(git_dir / name)
                require(shared_index is not None, "Missing shared Git index")
                data = capture_bytes(shared_index)
                nested, shared_sparse, _ = isolated_index_exclusions(data, oid_length, self.names, excludes)
                require(nested is None and not shared_sparse, "Invalid shared Git index")
                (self.gitdir / name).write_bytes(data)
            if sparse:
                (self.gitdir / "info").mkdir()
                (self.gitdir / "info/sparse-checkout").write_text("/*\n!/*/\n")
                options = ["-c", "core.sparseCheckout=true", "-c", "core.sparseCheckoutCone=true",
                           "-c", "index.sparse=true"]
                sparse_flag = ["--sparse"]
        result = self.run(*options, "ls-files", "-z", "--cached", *sparse_flag, "--", *sorted(excludes))
        require(result.returncode == 0 and not result.stderr, "Cannot read isolated Git index for capture")
        (self.gitdir / "index").unlink(missing_ok=True)
        paths = nul_paths(result.stdout)
        require(not any(self.names.excluded(path, include_ancestors=True) for path in paths),
                "Isolated Git index lookup returned a path intersecting read_excludes")
        return paths | (self.expand_sparse(common, collapsed, oid_length) if collapsed else set())

    def expand_sparse(self, common, collapsed, oid_length):
        stores, seen = [common / "objects"], set()
        object_dirs = []
        for candidate in stores:
            directory = self.guard(candidate)
            if directory in seen:
                continue
            seen.add(directory)
            object_dirs.append(directory)
            alternates = self.guarded_file(directory / "info/alternates")
            if alternates is not None:
                for entry in capture_bytes(alternates).splitlines():
                    if entry:
                        stores.append(directory / os.fsdecode(unquote_git_path(entry)))
        target = self.common / "objects"
        pack_dir = target / "pack"
        pack_dir.mkdir(exist_ok=True)
        paths = set()
        with ExitStack() as stack:
            packs = {}
            for directory in object_dirs:
                parent = self.guard(directory / "pack")
                if not directory_status(parent, True):
                    continue
                with open_directory(parent) as fd:
                    entries = os.listdir(fd)
                for name in entries:
                    if name in packs or not re.fullmatch(r"pack-[0-9a-f]+\.(?:pack|idx)", name):
                        continue
                    source = self.guarded_file(parent / name)
                    require(source is not None, "Git pack changed during capture")
                    handle = stack.enter_context(capture_open(source))
                    packs[name] = handle.fileno()
                    link = pack_dir / name
                    link.symlink_to(f"/dev/fd/{handle.fileno()}")
                    stack.callback(link.unlink)
            pending = [(path, oid, frozenset()) for path, oid in collapsed.items()]
            while pending:
                prefix, oid, ancestors = pending.pop()
                require(oid not in ancestors, "Sparse Git tree contains an object cycle")
                loose = f"{oid[:2]}/{oid[2:]}"
                for directory in object_dirs:
                    source = self.guarded_file(directory / loose)
                    if source is None:
                        continue
                    (target / loose).parent.mkdir(exist_ok=True)
                    with capture_open(source) as handle:
                        (target / loose).write_bytes(handle.read())
                    break
                for fd in packs.values():
                    os.lseek(fd, 0, os.SEEK_SET)
                result = self.run("cat-file", "tree", oid, pass_fds=tuple(packs.values()))
                require(result.returncode == 0, "Cannot read allowed Git tree from sparse index")
                for mode, name, child in tree_entries(result.stdout, oid_length):
                    path = f"{prefix}/{name}"
                    tree = mode == b"40000"
                    if self.names.excluded(path, include_ancestors=not tree):
                        continue
                    if tree:
                        pending.append((path, child, ancestors | {oid}))
                    else:
                        paths.add(path)
        return paths

    def reftable(self, git_dir, common):
        if git_dir != common:
            self.common = self.gitdir.parent / "common"
            (self.common / "objects").mkdir(parents=True)
            (self.common / "refs").mkdir()
            (self.gitdir / "commondir").write_text("../common\n")
        for source, target in ((common, self.common), (git_dir, self.gitdir)):
            if (target / "reftable").exists():
                continue
            (target / "HEAD").write_bytes(b"ref: refs/heads/.invalid\n")
            tables = target / "reftable"
            tables.mkdir()
            listing = self.guarded_file(source / "reftable/tables.list")
            if listing is None:
                continue
            data = capture_bytes(listing)
            for line in data.splitlines():
                require(re.fullmatch(rb"[A-Za-z0-9._-]+\.ref", line), "Invalid Git reftable filename")
                name = os.fsdecode(line)
                table = self.guarded_file(source / "reftable" / name)
                require(table is not None, "Missing Git reftable")
                (tables / name).write_bytes(capture_bytes(table))
            (tables / "tables.list").write_bytes(data)

    def head(self, git_dir, common):
        path, filename = git_dir / "HEAD", "HEAD"
        seen = set()
        while True:
            require(filename not in seen and len(seen) < 10, "Invalid Git HEAD reference chain")
            seen.add(filename)
            entry = self.guarded_file(path)
            if entry is None:
                require(filename != "HEAD", "Missing Git HEAD")
                packed = self.guarded_file(common / "packed-refs")
                if packed is not None:
                    (self.gitdir / "packed-refs").write_bytes(capture_bytes(packed))
                break
            data = capture_bytes(entry)
            destination = self.gitdir / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            if not data.startswith(b"ref:"):
                break
            filename = os.fsdecode(data[4:].strip())
            require(filename.startswith("refs/") and self.run("check-ref-format", filename).returncode == 0,
                    "Invalid Git HEAD symbolic reference")
            per_worktree = filename.startswith(("refs/bisect/", "refs/worktree/", "refs/rewritten/"))
            path = (git_dir if per_worktree else common) / filename
        result = self.run("rev-parse", "--verify", "HEAD")
        return result.stdout.decode().strip() if result.returncode == 0 else None

    def untracked(self, tracked, common, excludesfile):
        others = set()

        def walk(fd, directory, prefix=""):
            with os.scandir(fd) as entries:
                for entry in entries:
                    name = prefix + entry.name
                    if (spelling_key(entry.name) == ".git" and entry.name == self.names.entry(directory, ".git")
                            or self.names.excluded(name)):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            walk(child, directory / entry.name, name + "/")
                        finally:
                            os.close(child)
                    elif name not in tracked and not self.names.excluded(name, include_ancestors=True):
                        others.add(name)

        with open_directory(self.names.root) as fd:
            walk(fd, self.names.root)
        if not others:
            return others
        for directory in {p for name in others for p in PurePosixPath(name).parents}:
            ignore = directory / ".gitignore"
            target = self.worktree / ignore
            target.parent.mkdir(parents=True, exist_ok=True)
            # Git does not follow symlinks for in-tree ignore files.
            source = self.names.root / ignore
            require(not self.names.excluded(ignore.as_posix()), "Git ignore rules intersect read_excludes")
            try:
                info = capture_lstat(source)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                with capture_open(source, info) as handle:
                    target.write_bytes(handle.read())
        args = []
        if excludesfile is not None:
            (self.gitdir / "user-excludes").write_bytes(capture_bytes(excludesfile))
            args = ["-c", f"core.excludesFile={self.gitdir / 'user-excludes'}"]
        info = self.guarded_file(common / "info/exclude")
        if info is not None:
            (self.common / "info").mkdir(exist_ok=True)
            (self.common / "info/exclude").write_bytes(capture_bytes(info))
        result = self.run(*args, "check-ignore", "--no-index", "-z", "--stdin",
                          input=b"\0".join(map(os.fsencode, others)) + b"\0")
        require(result.returncode in (0, 1) and not result.stderr, "Cannot check isolated Git ignore rules")
        return others - nul_paths(result.stdout)


def restricted_git_capture(names, marker, tables):
    require(not any(key in os.environ for key in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE")),
            "Git directory overrides are unsupported with read_excludes")
    guard = lambda path: resolve_guarded(path, tables)
    git_dir, common, git_dir_spelling = git_directories(marker, guard)
    override = os.environ.get("GIT_INDEX_FILE")
    index = guarded_file(names.root / override if override else git_dir / "index", guard)
    require(not override or index is not None, "Missing overridden Git index")
    # Repository-controlled TMPDIR is another pointer that could be swapped before
    # Git starts. Use the platform's system temporary directory for this boundary.
    scratch_parent = guard(Path("/tmp"))
    require(not scratch_parent.is_relative_to(names.root), "Restricted capture needs temporary storage outside the repo")
    with tempfile.TemporaryDirectory(prefix="ship-capture-", dir=scratch_parent) as temporary:
        capture_git = CaptureGit(Path(temporary), names, tables)
        home = Path.home().absolute()
        user_config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config").absolute()
        repo_config = common / "config"
        _, repo_format = capture_git.config(repo_config)
        repo_format = dict(repo_format)
        algorithm = repo_format.get(b"extensions.objectformat", b"sha1").lower()
        require(algorithm in (b"sha1", b"sha256"), "Unsupported Git index object format")
        refstorage = repo_format.get(b"extensions.refstorage", b"files").lower()
        require(refstorage in (b"files", b"reftable"), "Unsupported Git reference storage")
        config = b"[core]\nrepositoryformatversion = 1\n"
        if algorithm == b"sha256" or refstorage == b"reftable":
            config += b"[extensions]\nobjectformat = " + algorithm + b"\n"
        if refstorage == b"reftable":
            capture_git.reftable(git_dir, common)
            config += b"refstorage = reftable\n"
        (capture_git.common / "config").write_bytes(config)
        if refstorage == b"reftable":
            head_result = capture_git.run("rev-parse", "--verify", "HEAD")
            head = head_result.stdout.decode().strip() if head_result.returncode == 0 else None
        else:
            head = capture_git.head(git_dir, common)
        ref = capture_git.run("symbolic-ref", "--quiet", "HEAD")
        branch = ref.stdout.strip().removeprefix(b"refs/heads/") if ref.stdout.startswith(b"refs/heads/") else None

        system = capture_git.system_config()
        sources = [user_config / "git/config", home / ".gitconfig", repo_config]
        if system is not None:
            sources.insert(0, system)
        if repo_format.get(b"extensions.worktreeconfig", b"false").lower() not in (b"", b"false", b"no", b"off", b"0"):
            sources.append(git_dir / "config.worktree")
        settings = capture_git.effective_config(sources, (git_dir, git_dir_spelling), branch)
        ignorecase = settings.get(b"core.ignorecase", b"false").lower() or b"false"
        require(ignorecase in (b"", b"yes", b"no", b"true", b"false", b"on", b"off", b"1", b"0"),
                "Invalid core.ignoreCase for restricted capture")
        (capture_git.common / "config").write_bytes(config + b"[core]\nignorecase = " + ignorecase + b"\n")
        # Override the default user ignore location as well, even when that file is absent.
        capture_git.env["XDG_CONFIG_HOME"] = str(capture_git.gitdir)
        global_ignore = user_config / "git/ignore"
        if b"core.excludesfile" in settings:
            global_ignore = capture_git.config_path(settings[b"core.excludesfile"], names.root)
        excludesfile = capture_git.guarded_file(global_ignore)
        tracked = capture_git.tracked(index, git_dir, common, algorithm)
        return tracked, capture_git.untracked(tracked, common, excludesfile), head


def scoped_tracked_paths(root, paths, exclusions, *, guard=None):
    """Look up exact index entries, isolating Git from live metadata when reads are restricted."""
    includes = [f":(top,literal){name}" for name in paths]
    pathspecs = ["--", *includes, *[f":(top,exclude,literal){name}" for name in exclusions]]
    if guard is None:
        require((root / ".git").exists(), "Scope amendment requires a git repository")
        return git_paths(root, "--cached", *pathspecs)

    git_dir, common, _ = git_directories(root / ".git", guard)
    index_path = root / os.environ["GIT_INDEX_FILE"] if "GIT_INDEX_FILE" in os.environ else git_dir / "index"
    index = guarded_file(index_path, guard)
    require(index is not None, "Scope amendment needs a Git index")
    config = guarded_file(common / "config", guard)
    return isolated_index_paths(git_dir, index, config, pathspecs, includes, guard)


def isolated_index_paths(git_dir, index, config, pathspecs, includes, guard):
    # Never expose the live config, object database, worktree or Git environment to ls-files.
    env = isolated_git_env()
    scratch_parent = guard(Path("/tmp"))
    with tempfile.TemporaryDirectory(prefix="ship-index-", dir=scratch_parent) as temporary:
        scratch = Path(temporary)
        executable = guarded_git_executable(scratch, guard)
        (scratch / "objects").mkdir()
        (scratch / "refs").mkdir()
        (scratch / "HEAD").write_text("ref: refs/heads/unused\n")
        if config is not None:
            result = subprocess.run([executable, "--git-dir", str(scratch), "config", "--file", "-", "--no-includes",
                                     "--get", "extensions.objectformat"], cwd=scratch, env=env, capture_output=True,
                                    input=capture_bytes(config))
            require(result.returncode in (0, 1), "Cannot read Git index object format")
            algorithm = result.stdout.strip()
        else:
            algorithm = b""
        require(algorithm in {b"", b"sha1", b"sha256"}, "Unsupported Git index object format")
        if algorithm == b"sha256":
            (scratch / "config").write_text("[core]\nrepositoryformatversion = 1\n"
                                            "[extensions]\nobjectformat = sha256\n")
        (scratch / "index").write_bytes(capture_bytes(index))
        with open_directory(git_dir) as fd:
            entries = os.listdir(fd)
        for name in entries:
            if re.fullmatch(r"sharedindex\.(?:[0-9a-f]{40}|[0-9a-f]{64})", name):
                shared = guarded_file(git_dir / name, guard)
                require(shared is not None, "Invalid Git shared index")
                (scratch / name).write_bytes(capture_bytes(shared))
        command = [executable, "--git-dir", str(scratch), "ls-files", "-z", "--cached"]
        result = subprocess.run([*command, *pathspecs], cwd=scratch, env=env, capture_output=True)
        if result.stderr:
            # Sparse indexes can report their materialized entries without loading tree objects.
            (scratch / "info").mkdir()
            (scratch / "info/sparse-checkout").write_text("/*\n!/*/\n")
            command = [executable, "--git-dir", str(scratch), "-c", "core.sparseCheckout=true",
                       "-c", "core.sparseCheckoutCone=true", "-c", "index.sparse=true",
                       "ls-files", "-z", "--cached", "--sparse"]
            # Git's sparse matcher can drop allowed paths when an exclusion names a collapsed entry.
            # The caller already checked each literal against the policy before opening the index.
            result = subprocess.run([*command, "--", *includes], cwd=scratch, env=env, capture_output=True)
        require(result.returncode == 0 and not result.stderr,
                "Cannot verify scope amendment with the isolated Git index")
        return {os.fsdecode(path) for path in result.stdout.split(b"\0") if path}


def spelling_key(name):
    return unicodedata.normalize("NFD", name).casefold()


class PathNames:
    """Resolve alternate spellings against directory entries observed during one operation."""

    def __init__(self, root, read_excludes=(), *, pinned=False):
        self.root = Path(root)
        self.read_excludes = read_excludes
        self.pinned = pinned
        self.directories = {}
        self.keys_by_path = {}
        self.symlink_ancestors = set()
        self.real_directories = {}

    def can_list(self, directory):
        if directory not in self.real_directories:
            self.real_directories[directory] = ((directory == self.root or self.can_list(directory.parent)) and
                                                (directory_status(directory, True) if self.pinned else
                                                 not directory.is_symlink() and directory.is_dir()))
        return self.real_directories[directory]

    def directory_entries(self, directory):
        if directory not in self.directories:
            by_spelling = {}
            if self.can_list(directory):
                if self.pinned:
                    with open_directory(directory) as fd:
                        entries = os.listdir(fd)
                else:
                    entries = os.listdir(directory)
                for actual in entries:
                    by_spelling.setdefault(spelling_key(actual), set()).add(actual)
            self.directories[directory] = by_spelling
        return self.directories[directory]

    def entry(self, directory, name):
        candidates = self.directory_entries(directory).get(spelling_key(name), ())
        if name in candidates or not candidates:
            return name
        try:
            inspect = capture_lstat if self.pinned else Path.lstat
            info = inspect(directory / name)
        except FileNotFoundError:
            return name
        matches = [actual for actual in candidates if os.path.samestat(info, inspect(directory / actual))]
        require(len(matches) <= 1, f"Ambiguous filesystem spelling: {directory / name}")
        return matches[0] if matches else name

    def key(self, path):
        if path not in self.keys_by_path:
            path = relative_path(path)
            require(not self.excluded(path), "Path intersects read_excludes")
            parent, _, name = path.rpartition("/")
            directory = self.root / self.key(parent) if parent else self.root
            # Resolve only entries in real directories. A symlink is distinct source data.
            if parent and (parent in self.symlink_ancestors or
                           (not self.can_list(directory) if self.pinned else directory.is_symlink())):
                self.symlink_ancestors.add(path)
                actual = name
            else:
                actual = self.entry(directory, name)
                require(spelling_key(actual) != ".git" or self.entry(directory, ".git") != actual,
                        f"Unsafe relative path: {path}")
            self.keys_by_path[path] = relative_path((directory / actual).relative_to(self.root).as_posix())
        return self.keys_by_path[path]

    def keys(self, paths):
        return {self.key(path) for path in paths}

    def spellings(self, paths):
        result = {}
        for path in paths:
            key = self.key(path)
            result.setdefault(key, {key}).add(path)
        return result

    def intersects(self, path, exclusions, include_ancestors=False):
        parts = path.split("/")
        for exclusion in sorted(exclusions, key=lambda name: name.count("/")):
            excluded = exclusion.split("/")
            if len(parts) < len(excluded) and not include_ancestors:
                continue
            length = min(len(parts), len(excluded))
            prefix, other = "/".join(parts[:length]), "/".join(excluded[:length])
            if prefix == other:
                return True
            if spelling_key(prefix) != spelling_key(other):
                continue
            # Stop at the first distinct parent before looking up anything below it.
            if all(part == expected or self.key("/".join(parts[:i])) == self.key("/".join(excluded[:i]))
                   for i, (part, expected) in enumerate(zip(parts[:length], excluded[:length]), 1)):
                return True
        return False

    def files(self, files):
        result = {}
        for path, recorded in files.items():
            require(not self.excluded(path, include_ancestors=True),
                    "Manifest path intersects read_excludes")
            key = self.key(path)
            require(key not in result or result[key] == recorded, f"Conflicting manifest entries for {key}")
            result[key] = recorded
        return result

    def excluded(self, path, *, include_ancestors=False):
        """Check read exclusions without probing possible aliases of a protected entry."""
        return self.excluded_prefix(path, include_ancestors=include_ancestors) is not None

    def excluded_prefix(self, path, *, include_ancestors=False):
        parts = path.split("/")
        for exclusion in sorted(self.read_excludes, key=lambda name: name.count("/")):
            excluded = exclusion.split("/")
            if len(parts) < len(excluded) and not include_ancestors:
                continue
            prefix = list(zip(parts, excluded))
            if any(spelling_key(part) != spelling_key(expected) for part, expected in prefix):
                continue
            directory = self.root
            for part, expected in prefix:
                if part != expected:
                    entries = self.directory_entries(directory).get(spelling_key(part), ())
                    if part in entries and expected in entries:
                        break
                    # An absent spelling could be an alias. Do not stat it to find out.
                    if part not in entries:
                        part = expected
                directory /= part
            else:
                return "/".join(parts[:len(prefix)])
        return None

    def changed(self, before, after, *, missing=None):
        left, right = self.files(before["files"]), self.files(after["files"])
        return {path for path in left.keys() | right.keys() if left.get(path, missing) != right.get(path, missing)}


def same_source(left, right, read_excludes=()):
    """Compare source across captures while retaining each manifest's exact fingerprint."""
    if left["repo_path"] != right["repo_path"] or left["head"] != right["head"]:
        return False
    if left["files"] == right["files"]:
        return True
    return not PathNames(left["repo_path"], read_excludes).changed(left, right)


def match_source(left, right, read_excludes=()):
    require(left["repo_path"] == right["repo_path"], "Manifests refer to different repositories")
    require(same_source(left, right, read_excludes), "Stale certification: source states differ")


def baseline_record(root, directory, path, *, guard=Path, pinned=False):
    markers = guard(directory / ".ship-state")
    if pinned:
        marker_name = digest(path) + ".json"
        try:
            with open_directory(markers) as descriptor:
                with os.scandir(descriptor) as entries:
                    names = sorted(entry.name for entry in entries if entry.name.endswith(".json"))
                records = []
                for name in names:
                    guard(markers / name)
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                    with os.fdopen(fd, "rb") as handle:
                        info = os.fstat(handle.fileno())
                        require(stat.S_ISREG(info.st_mode), "Invalid baseline marker")
                        record = json.load(handle)
                    recorded_path = relative_path(record.get("path"))
                    require(name == digest(recorded_path) + ".json", "Baseline marker does not match path")
                    if name == marker_name or spelling_key(recorded_path) == spelling_key(path):
                        records.append(record)
        except FileNotFoundError:
            return None
        source = guard(root / path)
        try:
            capture_lstat(source)
        except FileNotFoundError:
            return next((record for record in records if record["path"] == path), None)
        names = PathNames(root, pinned=True)
        entry = names.key(path)
        matches = []
        for record in records:
            guard(root / record["path"])
            if names.key(record["path"]) == entry:
                target = safe_path(directory, record["path"], guard=guard)
                require(file_state(target, pinned=True) == record["state"], "Baseline was modified")
                matches.append(record)
        require(len(matches) <= 1, "Conflicting baseline markers for source path")
        return matches[0] if matches else None
    require(not markers.is_symlink(), "Symlinked baseline marker directory")
    marker = guard(markers / (digest(path) + ".json"))
    exact = None
    if os.path.lexists(marker):
        require(not marker.is_symlink() and marker.is_file(), "Invalid baseline marker")
        exact = load(marker)
        require(exact.get("path") == path, "Baseline marker does not match path")
    names = PathNames(root)
    guard(root / path)
    entry = names.key(path)
    if not os.path.lexists(safe_path(root, path, guard=guard)):
        return exact
    matches = [exact] if exact is not None else []
    for candidate in markers.glob("*.json") if markers.is_dir() else ():
        if candidate == marker:
            continue
        guard(candidate)
        require(not candidate.is_symlink() and candidate.is_file(), "Invalid baseline marker")
        record = load(candidate)
        name = relative_path(record.get("path"))
        require(candidate.name == digest(name) + ".json", "Baseline marker does not match path")
        if spelling_key(name) != spelling_key(path):
            continue
        guard(root / name)
        if names.key(name) == entry:
            require(file_state(safe_path(directory, name, guard=guard)) == record["state"], "Baseline was modified")
            matches.append(record)
    require(len(matches) <= 1, "Conflicting baseline markers for source path")
    return matches[0] if matches else None


def baseline(root, directory, path, *, pinned_directory=None, policies=()):
    root = guard_excluded_paths(Path(root).absolute(), policies, pinned=bool(policies))
    if not policies:
        root = root.resolve()
    guard = lambda item: guard_excluded_paths(Path(item).absolute(), policies, pinned=bool(policies))
    source = safe_path(root, path, guard=guard if policies else Path)
    directory = Path(pinned_directory) if pinned_directory is not None else Path(directory).resolve()
    if pinned_directory is not None:
        with open_directory(directory, create=True):
            pass
    marker = directory / ".ship-state" / (digest(path) + ".json")
    record = baseline_record(root, directory, path, guard=guard if policies else Path,
                             pinned=pinned_directory is not None)
    if record is not None:
        require(file_state(safe_path(directory, record["path"]), pinned=pinned_directory is not None) == record["state"], "Baseline was modified")
        return
    if policies:
        guard(source)
    before = file_state(source, pinned=bool(policies))
    target = safe_path(directory, path)
    require(not guarded_exists(target, policies), "Unrecorded baseline already exists")
    if before["kind"] == "file":
        with open_directory(target.parent, create=True) as parent:
            fd = os.open(target.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666, dir_fd=parent)
            with os.fdopen(fd, "wb") as handle:
                handle.write(capture_bytes(source) if policies else source.read_bytes())
                source_info = capture_lstat(source) if policies else source.stat()
                os.fchmod(handle.fileno(), stat.S_IMODE(source_info.st_mode))
    elif before["kind"] == "symlink":
        with open_directory(target.parent, create=True) as parent:
            os.symlink(before["target"], target.name, dir_fd=parent)
    require(file_state(source, pinned=bool(policies)) == before
            and file_state(target, pinned=pinned_directory is not None) == before,
            "Source changed during baseline capture")
    write_once(marker, {"path": path, "state": before}, pinned=True)


def delta(root, directory, scope):
    root, directory = Path(root).resolve(), Path(directory).resolve()
    changes = []
    names = PathNames(root)
    seen = set()
    for name in scope:
        record = baseline_record(root, directory, name)
        require(record is not None, f"Missing first-touch record: {name}")
        old_path, new_path = safe_path(directory, record["path"]), safe_path(root, name)
        before, after = file_state(old_path), file_state(new_path)
        require(before == record["state"], f"Baseline modified: {name}")
        key = names.key(name)
        if before == after or key in seen:
            continue
        seen.add(key)
        entry = {"path": name, "before": before, "after": after}
        if before["kind"] in {"file", "missing"} and after["kind"] in {"file", "missing"}:
            try:
                old = old_path.read_text().splitlines(keepends=True) if before["kind"] == "file" else []
                new = new_path.read_text().splitlines(keepends=True) if after["kind"] == "file" else []
                entry["diff"] = "".join(difflib.unified_diff(old, new, "baseline/" + name, "current/" + name))
            except UnicodeError:
                entry["binary"] = True
        changes.append(entry)
    return {"repo_path": str(root), "diff_base": "baseline", "changes": changes}


def text(value, field):
    require(isinstance(value, str) and bool(value.strip()), f"Missing {field}")


def findings(value, repo, round_number, manifest=None, prior=()):
    require(isinstance(value, dict), "Findings must be an object")
    require(value.get("repo") == repo and value.get("round") == round_number, "Wrong repo/round")
    require(type(value.get("round")) is int and round_number > 0, "Invalid round")
    require(isinstance(value.get("findings"), list), "Missing findings array")
    if manifest is not None:
        require(value.get("source_fingerprint") == manifest["fingerprint"], "Stale review fingerprint")
        coverage = value.get("coverage")
        require(isinstance(coverage, dict) and isinstance(coverage.get("read"), list)
                and isinstance(coverage.get("skipped"), list), "Missing review coverage")
    old_ids = {entry["id"] for history in prior for entry in history["findings"]}
    suffixes = [int(item[len(repo) + 1:]) for item in old_ids
                if item.startswith(repo + "-") and item[len(repo) + 1:].isdigit()]
    maximum = max(suffixes, default=0)
    seen = set()
    for entry in value["findings"]:
        require(isinstance(entry, dict), "Finding must be an object")
        identifier = entry.get("id")
        text(identifier, "id")
        require(identifier not in seen, f"Duplicate finding ID: {identifier}")
        seen.add(identifier)
        require(identifier.startswith(repo + "-") and identifier[len(repo) + 1:].isdigit(),
                "Invalid finding ID")
        require(entry.get("severity") in SEVERITIES, "Invalid severity")
        require(entry.get("status") in {"new", "recurring", "regressed"}, "Invalid finding status")
        if entry["status"] == "new":
            require(identifier not in old_ids and int(identifier[len(repo) + 1:]) > maximum,
                    "New finding reuses a historical ID")
        else:
            require(identifier in old_ids, "Carry-forward ID missing from prior history")
        relative_path(entry.get("file"))
        require(type(entry.get("line")) is int and entry["line"] > 0, "Invalid finding line")
        for field in ("category", "description", "recommendation", "failure_scenario", "verification_expectation"):
            text(entry.get(field), field)
        require(isinstance(entry.get("criterion_ids"), list)
                and all(isinstance(item, str) for item in entry["criterion_ids"]), "Invalid criterion IDs")


def applied(value, review):
    require(isinstance(value, dict) and value.get("round") == review["round"], "Wrong applied round")
    expected = [entry["id"] for entry in review["findings"]]
    require(len(expected) == len(set(expected)), "Duplicate source finding IDs")
    seen = []
    for group in ("applied", "failed", "skipped"):
        require(isinstance(value.get(group), list), f"Missing {group} array")
        for entry in value[group]:
            require(isinstance(entry, dict), f"Invalid {group} entry")
            text(entry.get("id"), "id")
            text(entry.get("verified" if group == "applied" else "reason"), "evidence/reason")
            seen.append(entry["id"])
    require(len(seen) == len(set(seen)) and set(seen) == set(expected),
            "Each finding must occur exactly once in applied/failed/skipped")


def evidence(value, final, read_excludes=(), *, historical=False, guard=Path, loader=load, file_check=None):
    require(isinstance(value, dict), "Evidence must be an object")
    for field in ("criterion_id", "environment", "data_scope"):
        text(value.get(field), field)
    require(value.get("producer") in {"implementer", "orchestrator", "reviewer", "checker"}, "Invalid evidence producer")
    if value.get("producer") in {"reviewer", "checker"}:
        require(value.get("kind") == "static", "Reviewer/checker evidence must be read-only inspection")
    require(value.get("kind") in {"static", "empirical"}, "Invalid evidence kind")
    require(value.get("status") in {"passed", "failed", "unverified"}, "Invalid evidence status")
    if value["status"] != "passed":
        text(value.get("reason"), "failure/unverified reason")
    if value["kind"] == "empirical":
        text(value.get("command"), "command")
        if value["status"] == "unverified":
            require(value.get("exit_code") is None, "Unverified check must not claim an exit status")
        else:
            require(type(value.get("exit_code")) is int, "Missing actual exit code")
            require((value["exit_code"] == 0) == (value["status"] == "passed"), "Exit/status mismatch")
    else:
        text(value.get("assertion"), "static assertion")
    artifacts = value.get("artifact_paths")
    require(isinstance(artifacts, list) and bool(artifacts), "Missing evidence artifacts")
    for path in artifacts:
        require(Path(path).is_absolute() and (historical or (file_check(path) if file_check else guard(path).is_file())),
                f"Missing absolute artifact: {path}")
    after = checked_manifest(guard(value["after_manifest"]), loader=loader)
    match(checked_manifest(guard(value["before_manifest"]), loader=loader), after)
    match_source(after, final, read_excludes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sub = commands.add_parser("review-rubric")
    source = sub.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo-path")
    source.add_argument("--contract")
    sub.add_argument("--repo")
    sub = commands.add_parser("init-run")
    sub.add_argument("--run-dir", required=True)
    sub.add_argument("--workflow", choices=("ship", "fix"), required=True)
    sub.add_argument("--repo", action="append", required=True)
    sub.add_argument("--description", required=True)
    sub.add_argument("--plan-path")
    sub.add_argument("--session-id", default=os.environ.get("CODEX_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or "")
    sub = commands.add_parser("validate-run")
    sub.add_argument("--contract", required=True)
    sub.add_argument("--attempt-id", required=True)
    sub = commands.add_parser("replace-setup")
    sub.add_argument("--contract", required=True)
    sub.add_argument("--successor-draft", required=True)
    sub.add_argument("--reason", required=True)
    sub.add_argument("--read-policy", required=True)
    sub.add_argument("--bootstrap-read-policy")
    check_mode = sub.add_mutually_exclusive_group()
    check_mode.add_argument("--check", action="store_true")
    check_mode.add_argument("--expect-check-sha256")
    sub.add_argument("--exclude-related-run", action="append", default=[])
    sub.add_argument("--legacy-repair-recovery", action="append", default=[], metavar="ABS_PATH@SHA256")
    sub.add_argument("--legacy-host-signers", metavar="ABS_PATH@SHA256")
    for name in ("capture", "delta"):
        sub = commands.add_parser(name)
        sub.add_argument("--repo", required=True)
        sub.add_argument("--scope", required=True)
        sub.add_argument("--out", required=True)
        if name == "capture":
            sub.add_argument("--exclude", action="append", default=[])
            sub.add_argument("--read-exclude", action="append", default=[])
        else:
            sub.add_argument("--baseline", required=True)
    sub = commands.add_parser("baseline")
    for flag in ("repo", "baseline", "path"):
        sub.add_argument("--" + flag, required=True)
    sub = commands.add_parser("match")
    sub.add_argument("--left", required=True)
    sub.add_argument("--right", required=True)
    sub = commands.add_parser("project-ignored-dependencies")
    for flag in ("manifest", "dependencies", "out"):
        sub.add_argument("--" + flag, required=True)
    sub = commands.add_parser("validate-findings")
    for flag in ("file", "repo", "manifest"):
        sub.add_argument("--" + flag, required=True)
    sub.add_argument("--round", type=int, required=True)
    sub.add_argument("--prior", action="append", default=[])
    sub = commands.add_parser("validate-applied")
    sub.add_argument("--file", required=True)
    sub.add_argument("--findings", required=True)
    sub = commands.add_parser("validate-slices")
    for flag in ("contract", "repo", "file"):
        sub.add_argument("--" + flag, required=True)
    sub = commands.add_parser("slice-transition")
    for flag in ("contract", "repo", "slice", "schedule", "file", "out"):
        sub.add_argument("--" + flag, required=True)
    sub = commands.add_parser("batch-start")
    for flag in ("contract", "repo", "schedule", "base", "baseline", "out"):
        sub.add_argument("--" + flag, required=True)
    sub.add_argument("--member", action="append", required=True)
    sub.add_argument("--scope", default=None)
    sub = commands.add_parser("batch-barrier")
    for flag in ("contract", "repo", "schedule", "start", "manifest", "results", "out"):
        sub.add_argument("--" + flag, required=True)
    sub.add_argument("--scope", default=None)
    sub = commands.add_parser("slice-gate")
    for flag in ("contract", "repo", "schedule", "initial", "manifest", "out"):
        sub.add_argument("--" + flag, required=True)
    sub.add_argument("--amendment", default=None)
    sub = commands.add_parser("validate-evidence")
    sub.add_argument("--file", required=True)
    sub.add_argument("--manifest", required=True)
    sub = commands.add_parser("validate-submission")
    for flag in ("contract", "repo", "matrix", "manifest"):
        sub.add_argument("--" + flag, required=True)
    sub.add_argument("--slice")
    sub.add_argument("--schedule")
    sub.add_argument("--deliverable-manifest")
    sub = commands.add_parser("snapshot-deliverables")
    for flag in ("contract", "repo", "out-dir"):
        sub.add_argument("--" + flag, required=True)
    sub.add_argument("--slice")
    sub.add_argument("--schedule")
    for name in ("validate-contract", "validate-history", "review-budget", "transition", "complete", "packet"):
        sub = commands.add_parser(name)
        sub.add_argument("--contract", required=True)
        if name in ("transition", "packet", "validate-history", "review-budget"):
            sub.add_argument("--repo", required=True)
        if name == "transition":
            sub.add_argument("--file", required=True)
        if name in ("review-budget", "validate-history"):
            sub.add_argument("--slice")
        if name == "packet":
            sub.add_argument("--scope", required=True)
            sub.add_argument("--baseline", required=True)
        if name not in ("validate-contract", "validate-history", "review-budget"):
            sub.add_argument("--out", required=True)
    sub = commands.add_parser("phase")
    sub.add_argument("--contract", required=True)
    sub.add_argument("--file")
    sub.add_argument("--repo")
    sub.add_argument("--phase-id")
    sub.add_argument("--phase")
    sub.add_argument("--event")
    sub.add_argument("--status")
    sub.add_argument("--end-reason")
    sub.add_argument("--agent-id")
    sub.add_argument("--round", type=int)
    sub.add_argument("--attempt", type=int)
    sub.add_argument("--session-id")
    sub.add_argument("--at")
    sub.add_argument("--action")
    sub.add_argument("--previous-state-path")
    sub.add_argument("--previous-state-sha256")
    sub.add_argument("--checkpoint-path")
    sub.add_argument("--capsule-path")
    sub.add_argument("--slice-id")
    sub.add_argument("--out", required=True)
    sub = commands.add_parser("record-evidence")
    sub.add_argument("--contract", required=True)
    sub.add_argument("--repo")
    sub.add_argument("--batch")
    sub.add_argument("--stop-on-fail", action="store_true")
    sub.add_argument("--from-file", "--file", dest="from_file")
    sub.add_argument("--claim-id")
    sub.add_argument("--claim-assertion")
    sub.add_argument("--kind")
    sub.add_argument("--producer")
    sub.add_argument("--status")
    sub.add_argument("--command", dest="evidence_command")
    sub.add_argument("--exit-code", type=int)
    sub.add_argument("--assertion")
    sub.add_argument("--environment")
    sub.add_argument("--data-scope")
    sub.add_argument("--before-manifest")
    sub.add_argument("--after-manifest")
    sub.add_argument("--artifact", action="append", default=[])
    sub.add_argument("--reason")
    sub.add_argument("--out")
    sub = commands.add_parser("digest")
    sub.add_argument("--contract", required=True)
    mode = sub.add_mutually_exclusive_group(required=True)
    mode.add_argument("--repo")
    mode.add_argument("--all-repos", action="store_true")
    sub.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if args.command != "replace-setup" and getattr(args, "contract", None):
        policies = contract_read_policy(args.contract)
        source = guard_excluded_paths(Path(args.contract).absolute(), policies, pinned=bool(policies))
        if not policies:
            source = source.resolve()
        root = source.parent
        guard_excluded_paths(root, policies, pinned=bool(policies))
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return dispatch(args)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    output = getattr(args, "out", None) or (getattr(args, "baseline", None) if args.command == "baseline" else None)
    explicit = ((args.repo, tuple(args.read_exclude)),) if getattr(args, "read_exclude", None) else ()
    lexical_root = None
    if output:
        candidate = Path(output).absolute()
        lexical_root = next((parent for parent in (candidate, *candidate.parents)
                             if parent.parent.name in {".ship-runs", ".fix-runs"}), None)
    if lexical_root is not None:
        contract_file = lexical_root / "contract.json"
        guard_excluded_paths(contract_file, explicit, pinned=bool(explicit))
        explicit = (*explicit, *contract_read_policy(contract_file))
    if output:
        resolved_output = guard_excluded_paths(Path(output).absolute(), explicit, pinned=bool(explicit))
        if not explicit:
            resolved_output = resolved_output.resolve()
        if args.command == "baseline":
            args.baseline = str(resolved_output)
        else:
            args.out = str(resolved_output)
    root = output_run_root(output, explicit)
    if root is not None:
        contract_file = root / "contract.json"
        guard_excluded_paths(contract_file, explicit, pinned=bool(explicit))
        policies = (*explicit, *contract_read_policy(contract_file))
        args._read_policy = policies
        guard_excluded_paths(root, policies, pinned=bool(policies))
        guard_excluded_paths(Path(output).absolute(), policies, pinned=bool(policies))
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            require(not guarded_exists(root / "setup-replacement.json", policies), "This setup run has been replaced")
            if guarded_exists(contract_file, policies):
                path = guard_excluded_paths(contract_file, policies, pinned=bool(policies))
                if policies:
                    with capture_open(path) as handle:
                        record = json.load(handle)
                else:
                    record = load(path)
                for repo in record.get("repos", []):
                    relative_path(repo)
                    require(not guarded_exists(root / repo / "successor-reservation.json", policies),
                            "Predecessor has been reserved for a successor")
            return dispatch(args)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    return dispatch(args)


def dispatch(args):
    if args.command == "review-rubric":
        if args.contract:
            import importlib.util
            module_spec = importlib.util.spec_from_file_location("ship_workflow", Path(__file__).with_name("ship_workflow.py"))
            workflow = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(workflow)
            contract = workflow.contract(args.contract)
            require(args.repo in contract["repos"], "Unknown rubric repo")
            repo = Path(contract["repo_paths"][args.repo])
            policies = contract_read_policy(args.contract)
        else:
            require(not args.repo, "--repo requires --contract")
            repo = Path(args.repo_path)
            require(repo.is_absolute() and repo.is_dir(), "--repo-path must be an absolute directory")
            policies = ()
        project = repo / ".agents/skills/review/SKILL.md"
        project = guard_excluded_paths(project, policies, pinned=bool(policies))
        if policies:
            exists = guarded_exists(project, policies)
        else:
            try:
                os.lstat(project)
                exists = True
            except FileNotFoundError:
                exists = False
        if exists:
            selected, source = project, "project"
        else:
            selected, source = Path(__file__).resolve().parent.parent / "DEFAULT-REVIEW.md", "default"
        selected = guard_excluded_paths(selected, policies, pinned=bool(policies))
        if not policies:
            selected = selected.resolve(strict=True)
        with capture_open(selected) as handle:
            content = handle.read().decode("utf-8")
        ids = re.findall(r"^## ([a-z][a-z0-9-]*)$", content, re.M) if source == "default" else None
        if source == "default":
            require(ids and len(ids) == len(set(ids)), "Invalid default rubric IDs")
        print(json.dumps({"source": source, "path": str(selected.absolute()), "content": content, "ids": ids}))
        return
    if args.command == "init-run":
        print(json.dumps(init_run(args.run_dir, args.workflow, args.repo, args.description, args.plan_path, args.session_id)))
        return
    if args.command == "validate-run":
        import importlib.util
        module_spec = importlib.util.spec_from_file_location("ship_workflow", Path(__file__).with_name("ship_workflow.py"))
        workflow = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(workflow)
        print(json.dumps(validate_run(args.contract, args.attempt_id, workflow)))
        return
    if args.command == "replace-setup":
        import importlib.util
        module_spec = importlib.util.spec_from_file_location("ship_workflow", Path(__file__).with_name("ship_workflow.py"))
        workflow = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(workflow)
        policy_bytes = args.read_policy.encode("utf-8")
        policy_path = None
        if Path(args.read_policy).is_absolute():
            require(args.bootstrap_read_policy is not None,
                    "A read-policy file requires an inline bootstrap read policy")
            bootstrap = read_policy_input(json.loads(args.bootstrap_read_policy))
            policy_path = Path(args.read_policy)
            guarded = guard_excluded_paths(policy_path, bootstrap, pinned=bool(bootstrap))
            if not bootstrap:
                guarded = guarded.resolve()
            policy_bytes = capture_bytes(guarded)
        policy_sha256 = hashlib.sha256(policy_bytes).hexdigest()
        policies = read_policy_input(json.loads(policy_bytes))
        if policy_path is not None:
            supplied = {(root, path) for root, paths in bootstrap for path in paths}
            required = {(root, path) for root, paths in policies for path in paths}
            require(required <= supplied, "Bootstrap read policy omits a file policy exclusion")
            policies = bootstrap
        else:
            require(args.bootstrap_read_policy is None, "Inline read policy cannot use a separate bootstrap policy")
        def pinned_reference(reference):
            path, separator, digest = reference.rpartition("@")
            require(separator and Path(path).is_absolute() and re.fullmatch(r"[0-9a-f]{64}", digest),
                    "Legacy repair evidence and allowed signers need ABS_PATH@SHA256")
            return {"path": path, "sha256": digest}

        require(bool(args.legacy_repair_recovery) == bool(args.legacy_host_signers),
                "Legacy repair recovery requires an independently provisioned --legacy-host-signers file")
        legacy_recovery = [{**pinned_reference(reference),
                            "allowed_signers": pinned_reference(args.legacy_host_signers)}
                           for reference in args.legacy_repair_recovery]
        require(len({reference["path"] for reference in legacy_recovery}) == len(legacy_recovery),
                "Duplicate legacy repair recovery path")
        result = workflow.replace_setup(args.contract, args.successor_draft, args.reason,
                                        bootstrap_policy=policies, check=args.check,
                                        expected_check=args.expect_check_sha256, policy_sha256=policy_sha256,
                                        policy_path=policy_path, excluded_related_runs=args.exclude_related_run,
                                        legacy_recovery=legacy_recovery)
        print(json.dumps(result))
        if result.get("status") == "blocked":
            sys.exit(1)
        return
    if args.command in {"slice-transition", "slice-gate", "batch-start", "batch-barrier"}:
        import importlib.util
        module_spec = importlib.util.spec_from_file_location(
            "ship_workflow", Path(__file__).with_name("ship_workflow.py"))
        workflow = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(workflow)
        if args.command == "batch-start":
            workflow.batch_start(args.contract, args.repo, args.schedule, args.member, args.base, args.baseline,
                                 scope_paths(args.scope) if args.scope else [], args.out)
        elif args.command == "batch-barrier":
            workflow.batch_barrier(args.contract, args.repo, args.schedule, args.start, args.manifest, args.results,
                                   args.out)
        elif args.command == "slice-transition":
            workflow.slice_transition(args.contract, args.repo, args.slice, args.schedule, args.file, args.out)
        else:
            workflow.slice_gate(args.contract, args.repo, args.schedule, args.initial, args.manifest, args.out,
                                args.amendment)
    elif args.command == "validate-slices":
        import importlib.util
        module_spec = importlib.util.spec_from_file_location(
            "ship_workflow", Path(__file__).with_name("ship_workflow.py"))
        workflow = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(workflow)
        print(json.dumps(workflow.slice_schedule(args.contract, args.repo, args.file), indent=1))
    elif args.command in {"validate-contract", "validate-history", "review-budget", "transition", "validate-submission", "snapshot-deliverables",
                          "complete", "packet", "phase", "record-evidence", "digest"}:
        import importlib.util
        module_spec = importlib.util.spec_from_file_location(
            "ship_workflow", Path(__file__).with_name("ship_workflow.py"))
        workflow = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(workflow)
        if args.command == "snapshot-deliverables":
            value = workflow.contract(args.contract)
            item = workflow.selected_slice(value, args.repo, args.slice, args.schedule)
            print(workflow.snapshot_deliverables(args.contract, args.repo, args.out_dir, slice_item=item))
        elif args.command == "validate-submission":
            value = workflow.contract(args.contract)
            final = workflow.manifest(value, args.repo, args.manifest)
            events = workflow.history(Path(args.contract).resolve().parent / args.repo)
            if events:
                current = events[-1][2]
                scope = sorted(set(current.get("scope", [])) | set(current.get("source_scope", [])))
                workflow.fresh(value, args.repo, args.manifest, scope, current.get("excludes", []))
            errors = workflow.submission_evidence_errors(value, args.repo, args.matrix, final,
                                                          "rows" if args.slice else "criteria")
            require(not errors, "Invalid submission:\n" + "\n".join(errors))
            item = workflow.selected_slice(value, args.repo, args.slice, args.schedule)
            if item is not None:
                workflow.slice_matrix(value, args.repo, item, args.matrix, final)
            else:
                workflow.matrix(value, args.repo, args.matrix, final)
            workflow.validate_deliverables(value, args.repo, args.deliverable_manifest, slice_item=item)
            print(json.dumps({"status": "valid", "repo": args.repo, "slice": args.slice}))
        elif args.command == "validate-contract":
            value = workflow.contract(args.contract)
            workflow.validate_store_roots(value)
            schedules = value.get("slice_schedules", {})
            require(isinstance(schedules, dict) and set(schedules) <= set(value["repos"]), "Invalid slice schedules")
            for repo, declaration in schedules.items():
                require(isinstance(declaration, dict), "Invalid slice schedule declaration")
                workflow.slice_schedule(args.contract, repo, declaration.get("path"))
        elif args.command == "validate-history":
            result = (workflow.audit_slice_history(args.contract, args.repo, args.slice, strict=True) if args.slice
                      else workflow.validate_history(args.contract, args.repo))
            print(json.dumps(result, indent=2))
        elif args.command == "review-budget":
            print(json.dumps(workflow.review_budget_status(args.contract, args.repo, args.slice), indent=2))
        elif args.command == "transition":
            workflow.transition(args.contract, args.repo, args.file, args.out)
        elif args.command == "complete":
            workflow.complete(args.contract, args.out)
        elif args.command == "packet":
            workflow.packet(args.contract, args.repo, args.scope, args.baseline, args.out)
        elif args.command == "record-evidence":
            if args.batch:
                raw = sys.stdin.read() if args.batch == "-" else open(args.batch, encoding="utf-8").read()
                data = json.loads(raw)
                records = data["records"] if isinstance(data, dict) else data
                results, ok = workflow.record_evidence_batch(args.contract, records, args.stop_on_fail)
                print(json.dumps(results, separators=(",", ":"), ensure_ascii=False))
                if not ok:
                    sys.exit(1)
                return
            elif args.from_file:
                if not args.repo or not args.out:
                    raise ValueError("record-evidence needs --repo and --out (or --batch)")
                workflow.record_evidence(args.contract, args.repo, args.from_file, args.out)
            else:
                if not args.repo or not args.out:
                    raise ValueError("record-evidence needs --repo and --out (or --batch)")
                if not args.claim_id or not args.claim_assertion:
                    raise ValueError("record-evidence needs --from-file or --claim-id/--claim-assertion")
                record = {"claims": [{"id": args.claim_id, "assertion": args.claim_assertion}]}
                values = {"kind": args.kind, "producer": args.producer, "status": args.status,
                          "command": args.evidence_command, "exit_code": args.exit_code,
                          "assertion": args.assertion, "environment": args.environment,
                          "data_scope": args.data_scope, "before_manifest": args.before_manifest,
                          "after_manifest": args.after_manifest, "reason": args.reason}
                record.update({key: value for key, value in values.items() if value is not None})
                if args.artifact:
                    record["artifact_paths"] = args.artifact
                workflow.record_evidence_record(args.contract, args.repo, record, args.out)
        elif args.command == "phase":
            if args.file:
                workflow.phase(args.contract, args.file, args.out)
            else:
                if not args.phase_id:
                    raise ValueError("phase needs --file or --phase-id")
                workflow.phase_from_flags(
                    args.contract, args.out, phase_id=args.phase_id, phase=args.phase,
                    event=args.event, repo=args.repo, status=args.status,
                    end_reason=args.end_reason, agent_id=args.agent_id, round=args.round, attempt=args.attempt,
                    session_id=args.session_id, at=args.at, action=args.action,
                    previous_state_path=args.previous_state_path,
                    previous_state_sha256=args.previous_state_sha256,
                    checkpoint_path=args.checkpoint_path, capsule_path=args.capsule_path, slice_id=args.slice_id)
        elif args.command == "digest":
            result = (workflow.digest_all_repos(args.contract, args.verbose) if args.all_repos
                      else workflow.digest(args.contract, args.repo, args.verbose))
            if args.verbose:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print(json.dumps(result, separators=(",", ":"), ensure_ascii=False))
            if result.get("status") != "ok":
                sys.exit(1)
            return
        else:
            raise AssertionError(args.command)
    elif args.command == "capture":
        pinned = bool(args.read_exclude)
        scope = guard_excluded_path(Path(args.scope).absolute(), args.repo, args.read_exclude, pinned=pinned)
        out = guard_excluded_path(Path(args.out).absolute(), args.repo, args.read_exclude, pinned=pinned)
        write_once(out, capture(args.repo, scope_paths(scope, pinned=pinned), args.exclude, args.read_exclude),
                   pinned=pinned)
    elif args.command == "project-ignored-dependencies":
        write_once(args.out, project_ignored_dependencies(args.manifest, args.dependencies))
    elif args.command == "baseline":
        baseline(args.repo, args.baseline, args.path, pinned_directory=args.baseline,
                 policies=getattr(args, "_read_policy", ()))
    elif args.command == "delta":
        write_once(args.out, delta(args.repo, args.baseline, scope_paths(args.scope)))
    elif args.command == "match":
        match(checked_manifest(args.left), checked_manifest(args.right))
    elif args.command == "validate-findings":
        findings(load(args.file), args.repo, args.round, checked_manifest(args.manifest),
                 [load(path) for path in args.prior])
    elif args.command == "validate-applied":
        applied(load(args.file), load(args.findings))
    else:
        evidence(load(args.file), checked_manifest(args.manifest))
    print("OK: " + args.command)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        print(f"Blocked: {error}", file=sys.stderr)
        sys.exit(1)
