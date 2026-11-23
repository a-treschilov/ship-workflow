#!/usr/bin/env python3
"""Capture source state and validate immutable workflow artifacts without running gates."""

import argparse
import difflib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
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


def write_once(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=True)
        handle.write("\n")


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
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix=".run-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
        os.link(temporary, root / "run.json")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return record


def validate_run(contract_path, attempt_id, workflow):
    contract_file = Path(contract_path).absolute()
    root = contract_file.parent
    relative_path(attempt_id)
    require(re.fullmatch(r"[A-Za-z0-9_.-]+", attempt_id) is not None, "Invalid setup attempt ID")
    meta = load(root / "run.json")
    require(isinstance(meta, dict) and meta.get("schema_version") == 2
            and meta.get("run_id") == root.name and meta.get("workflow") in {"ship", "fix"},
            "Invalid run metadata")
    identifier = "setup-" + attempt_id
    session = os.environ.get("CODEX_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or ""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session):
        session = ""
    start = {"schema_version": 2, "run_id": meta["run_id"], "workflow": meta["workflow"],
             "repo": None, "phase_id": identifier, "phase": "setup", "event": "phase-start",
             "at": datetime.now(timezone.utc).isoformat(), "session_id": session}
    start_path = root / "telemetry" / (identifier + "-start.json")
    end_path = root / "telemetry" / (identifier + "-end.json")
    require(not end_path.exists() and not end_path.is_symlink(), "Setup attempt already exists")
    write_once(start_path, start)
    try:
        value = workflow.contract(contract_path)
        require(value["run_id"] == meta["run_id"] and value["workflow"] == meta["workflow"],
                "Run metadata/contract identity mismatch")
        require(value["repos"] == meta.get("repos"), "Run metadata/contract repositories mismatch")
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
               "start_sha256": hashlib.sha256(start_path.read_bytes()).hexdigest()}
        write_once(end_path, end)
        raise
    end = {**start, "event": "phase-end", "at": datetime.now(timezone.utc).isoformat(),
           "status": "passed", "start_sha256": hashlib.sha256(start_path.read_bytes()).hexdigest()}
    write_once(end_path, end)
    return end


def relative_path(value):
    require(isinstance(value, str) and bool(value), "Expected a nonempty relative path")
    path = PurePosixPath(value)
    require(not path.is_absolute() and ".." not in path.parts and ".git" not in path.parts
            and str(path) == value and value != ".", f"Unsafe relative path: {value}")
    return value


def safe_path(root, value, *, guard=Path):
    path = Path(root) / relative_path(value)
    guard(path)
    # A final symlink is source data; a symlinked parent would escape the scoped tree.
    for parent in path.parents:
        if parent == Path(root):
            break
        require(not parent.is_symlink(), f"Symlinked parent: {parent}")
    if any(spelling_key(part) == ".git" for part in value.split("/")):
        PathNames(root).key(value)
    return path


def guard_excluded_path(path, root, read_excludes):
    return guard_excluded_paths(path, ((Path(root).absolute(), read_excludes),) if read_excludes else ())


def guard_excluded_paths(path, policies, *, scope=False):
    """Resolve symlinks one component at a time without probing an excluded path."""
    if not policies:
        return Path(path)
    pending = [PathNames(root, exclusions) for root, exclusions in policies]
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
                names.append(PathNames(root, table.read_excludes))
        require(len(unresolved) < len(pending), "Cannot resolve repository roots without crossing read_excludes")
        pending = unresolved
    return resolve_guarded(path, names, scope=scope)


def same_path_prefix(path, other):
    length = min(len(path.parts), len(other.parts))
    prefix, other_prefix = path.parts[:length], other.parts[:length]
    if prefix == other_prefix:
        return True
    actual, expected = Path(*prefix), Path(*other_prefix)
    if spelling_key(str(actual)) != spelling_key(str(expected)):
        return False
    root_names = PathNames(expected.anchor, (expected.relative_to(expected.anchor).as_posix(),))
    return root_names.excluded(actual.relative_to(expected.anchor).as_posix())


def check_path_exclusions(path, names, *, scope=False):
    for table in names:
        if not same_path_prefix(path, table.root):
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
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            links += 1
            require(links <= 40, "Too many symlinks")
            target = Path(os.readlink(current))
            current = Path(target.anchor) if target.is_absolute() else current.parent
            pending = list(target.parts[1:] if target.is_absolute() else target.parts) + pending
    if scope:
        check_path_exclusions(current, names, scope=True)
    return current


def scope_paths(path):
    values = load(path)
    require(isinstance(values, list), "Scope must be a JSON array")
    return sorted({relative_path(value) for value in values})


def file_state(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing"}
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        return {"kind": "symlink", "target": os.readlink(path)}
    require(stat.S_ISREG(info.st_mode), f"Unsupported source kind: {path}")
    return {"kind": "file", "execute_bits": mode & 0o111,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def capture_once(root, scope, excludes, read_excludes=()):
    root = Path(root).resolve()
    require(root.is_dir(), f"Missing repo directory: {root}")
    # Do not mistake a directory inside a different checkout for an independent repo.
    is_git = (root / ".git").exists()
    explicit = {relative_path(p) for p in scope}
    forbidden = [relative_path(p) for p in read_excludes]
    names = PathNames(root, forbidden)

    def forbidden_path(path):
        return names.excluded(path, include_ancestors=True)

    require(not any(forbidden_path(p) for p in explicit), "Source scope intersects read_excludes")
    tracked, others, head = set(), set(), None
    if is_git:
        tracked = {p for p in git_paths(root, "--cached") if not forbidden_path(p)}
        if forbidden:
            # Avoid asking Git to traverse excluded directories during untracked discovery.
            def walk(directory: Path, prefix=""):
                with os.scandir(directory) as entries:
                    for entry in entries:
                        name = prefix + entry.name
                        if (spelling_key(entry.name) == ".git" and entry.name == names.entry(directory, ".git")
                                or names.excluded(name)):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            walk(Path(entry.path), name + "/")
                        else:
                            others.add(name)
            walk(root)
            if others:
                result = subprocess.run(["git", "-C", str(root), "check-ignore", "--no-index", "-z", "--stdin"],
                                        input=b"\0".join(os.fsencode(p) for p in others) + b"\0",
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                require(result.returncode in (0, 1), "git check-ignore failed")
                others -= {os.fsdecode(p) for p in result.stdout.split(b"\0") if p}
        else:
            others = {os.fsdecode(p) for p in git(root, "ls-files", "-z", "--others",
                                                 "--exclude-standard").split(b"\0") if p}
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
    files = {p: file_state(safe_path(root, p)) for p in sorted(preferred.values())}
    payload = {"repo_path": str(root), "head": head, "files": files}
    return {**payload, "fingerprint": digest(payload)}


def capture(root, scope, excludes=(), read_excludes=()):
    first = capture_once(root, scope, excludes, read_excludes)
    second = capture_once(root, scope, excludes, read_excludes)
    require(first == second, "Source changed during capture; stabilize and retry")
    return first


def checked_manifest(path):
    value = load(path)
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
    return {os.fsdecode(path) for path in git(root, "ls-files", "-z", *args).split(b"\0") if path}


def scoped_tracked_paths(root, paths, exclusions, *, guard=None):
    """Look up exact index entries, isolating Git from live metadata when reads are restricted."""
    includes = [f":(top,literal){name}" for name in paths]
    pathspecs = ["--", *includes, *[f":(top,exclude,literal){name}" for name in exclusions]]
    if guard is None:
        require((root / ".git").exists(), "Scope amendment requires a git repository")
        return git_paths(root, "--cached", *pathspecs)

    git_dir = guard(root / ".git")
    if git_dir.is_file():
        pointer = git_dir.read_text().strip()
        require(pointer.startswith("gitdir: "), "Invalid Git directory pointer")
        git_dir = guard(git_dir.parent / pointer.removeprefix("gitdir: "))
    require(git_dir.is_dir(), "Scope amendment requires a git repository")
    index = guard(root / os.environ["GIT_INDEX_FILE"] if "GIT_INDEX_FILE" in os.environ else git_dir / "index")
    require(index.is_file(), "Scope amendment needs a Git index")
    common_pointer = guard(git_dir / "commondir")
    common = guard(git_dir / common_pointer.read_text().strip()) if common_pointer.is_file() else git_dir
    config = guard(common / "config")
    return isolated_index_paths(git_dir, index, config, pathspecs, includes, guard)


def isolated_index_paths(git_dir, index, config, pathspecs, includes, guard):
    # Never expose the live config, object database, worktree or Git environment to ls-files.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0")
    scratch_parent = guard(Path(os.environ.get("TMPDIR") or "/tmp").absolute())
    with tempfile.TemporaryDirectory(prefix="ship-index-", dir=scratch_parent) as temporary:
        scratch = Path(temporary)
        if config.is_file():
            result = subprocess.run(["git", "config", "--file", str(config), "--no-includes",
                                     "--get", "extensions.objectformat"], cwd=scratch, env=env, capture_output=True)
            require(result.returncode in (0, 1), "Cannot read Git index object format")
            algorithm = result.stdout.strip()
        else:
            algorithm = b""
        require(algorithm in {b"", b"sha1", b"sha256"}, "Unsupported Git index object format")
        (scratch / "objects").mkdir()
        (scratch / "refs").mkdir()
        (scratch / "HEAD").write_text("ref: refs/heads/unused\n")
        if algorithm == b"sha256":
            (scratch / "config").write_text("[core]\nrepositoryformatversion = 1\n"
                                            "[extensions]\nobjectformat = sha256\n")
        shutil.copyfile(index, scratch / "index")
        for shared in git_dir.glob("sharedindex.*"):
            if re.fullmatch(r"sharedindex\.(?:[0-9a-f]{40}|[0-9a-f]{64})", shared.name):
                shared = guard(shared)
                require(shared.is_file(), "Invalid Git shared index")
                shutil.copyfile(shared, scratch / shared.name)
        command = ["git", "--git-dir", str(scratch), "ls-files", "-z", "--cached"]
        result = subprocess.run([*command, *pathspecs], cwd=scratch, env=env, capture_output=True)
        if result.stderr:
            # Sparse indexes can report their materialized entries without loading tree objects.
            (scratch / "info").mkdir()
            (scratch / "info/sparse-checkout").write_text("/*\n!/*/\n")
            command = ["git", "--git-dir", str(scratch), "-c", "core.sparseCheckout=true",
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

    def __init__(self, root, read_excludes=()):
        self.root = Path(root)
        self.read_excludes = read_excludes
        self.directories = {}
        self.keys_by_path = {}
        self.symlink_ancestors = set()

    def directory_entries(self, directory):
        if directory not in self.directories:
            by_spelling = {}
            if not directory.is_symlink() and directory.is_dir():
                for actual in os.listdir(directory):
                    by_spelling.setdefault(spelling_key(actual), set()).add(actual)
            self.directories[directory] = by_spelling
        return self.directories[directory]

    def entry(self, directory, name):
        candidates = self.directory_entries(directory).get(spelling_key(name), ())
        if name in candidates or not candidates:
            return name
        try:
            info = (directory / name).lstat()
        except FileNotFoundError:
            return name
        matches = [actual for actual in candidates if os.path.samestat(info, (directory / actual).lstat())]
        require(len(matches) <= 1, f"Ambiguous filesystem spelling: {directory / name}")
        return matches[0] if matches else name

    def key(self, path):
        if path not in self.keys_by_path:
            path = relative_path(path)
            require(not self.excluded(path), "Path intersects read_excludes")
            parent, _, name = path.rpartition("/")
            directory = self.root / self.key(parent) if parent else self.root
            # Resolve only entries in real directories. A symlink is distinct source data.
            if parent and (parent in self.symlink_ancestors or directory.is_symlink()):
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
                return True
        return False

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


def baseline_record(root, directory, path, *, guard=Path):
    markers = guard(directory / ".ship-state")
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


def baseline(root, directory, path):
    root = Path(root).resolve()
    source = safe_path(root, path)
    directory = Path(directory).resolve()
    marker = directory / ".ship-state" / (digest(path) + ".json")
    record = baseline_record(root, directory, path)
    if record is not None:
        require(file_state(safe_path(directory, record["path"])) == record["state"], "Baseline was modified")
        return
    before = file_state(source)
    target = safe_path(directory, path)
    require(not target.exists() and not target.is_symlink(), "Unrecorded baseline already exists")
    if before["kind"] == "file":
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(source.read_bytes())
        os.chmod(target, stat.S_IMODE(source.stat().st_mode))
    elif before["kind"] == "symlink":
        target.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(before["target"], target)
    require(file_state(source) == before and file_state(target) == before,
            "Source changed during baseline capture")
    write_once(marker, {"path": path, "state": before})


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


def evidence(value, final, read_excludes=(), *, historical=False, guard=Path):
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
        require(Path(path).is_absolute() and (historical or guard(path).is_file()),
                f"Missing absolute artifact: {path}")
    after = checked_manifest(guard(value["after_manifest"]))
    match(checked_manifest(guard(value["before_manifest"])), after)
    match_source(after, final, read_excludes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
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
        if name == "review-budget":
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
            selected = None
            if args.slice:
                require(args.schedule, "--slice needs --schedule")
                workflow.slice_schedule(args.contract, args.repo, args.schedule)
                item = next((entry for entry in load(args.schedule)["slices"] if entry["id"] == args.slice), None)
                require(item is not None, "Unknown slice")
                selected = {c["criterion_id"] for c in item["contributions"]}
            else:
                require(not args.schedule, "--schedule needs --slice")
            print(workflow.snapshot_deliverables(args.contract, args.repo, args.out_dir, selected))
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
            if args.slice:
                require(args.schedule, "--slice needs --schedule")
                workflow.slice_schedule(args.contract, args.repo, args.schedule)
                schedule = load(args.schedule)
                item = next((entry for entry in schedule["slices"] if entry["id"] == args.slice), None)
                require(item is not None, "Unknown slice")
                workflow.slice_matrix(value, args.repo, item, args.matrix, final)
            else:
                require(not args.schedule, "--schedule needs --slice")
                workflow.matrix(value, args.repo, args.matrix, final)
            selected = {c["criterion_id"] for c in item["contributions"]} if args.slice else None
            workflow.validate_deliverables(value, args.repo, args.deliverable_manifest, selected)
            print(json.dumps({"status": "valid", "repo": args.repo, "slice": args.slice}))
        elif args.command == "validate-contract":
            value = workflow.contract(args.contract)
            schedules = value.get("slice_schedules", {})
            require(isinstance(schedules, dict) and set(schedules) <= set(value["repos"]), "Invalid slice schedules")
            for repo, declaration in schedules.items():
                require(isinstance(declaration, dict), "Invalid slice schedule declaration")
                workflow.slice_schedule(args.contract, repo, declaration.get("path"))
        elif args.command == "validate-history":
            print(json.dumps(workflow.validate_history(args.contract, args.repo), indent=2))
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
        guard_excluded_path(Path(args.scope).absolute(), args.repo, args.read_exclude)
        guard_excluded_path(Path(args.out).absolute(), args.repo, args.read_exclude)
        write_once(args.out, capture(args.repo, scope_paths(args.scope), args.exclude, args.read_exclude))
    elif args.command == "project-ignored-dependencies":
        write_once(args.out, project_ignored_dependencies(args.manifest, args.dependencies))
    elif args.command == "baseline":
        baseline(args.repo, args.baseline, args.path)
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
