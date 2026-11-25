"""Versioned workflow validation; never executes checks or launches agents."""

from datetime import datetime, timezone
import copy
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat

import importlib.util

_spec = importlib.util.spec_from_file_location("workflow_state_helpers", Path(__file__).with_name("ship_state.py"))
state = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(state)
_spec = importlib.util.spec_from_file_location("workflow_store", Path(__file__).with_name("ship_store.py"))
store = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(store)

VERSION = 2
GATE_FIX_LIMIT = 2
# Why a phase ended; unknown is a legitimate answer and must never be inferred from duration.
END_REASONS = {"done", "context", "api-error", "user-stop", "timeout", "unknown"}
UNSPECIFIED_DELIVERY = object()
TERMINAL = {"blocked", "capped", "handoff", "preflight-blocked"}
EDITS = {"implement", "review-fix", "gate-fix", "plan-gap"}
TRANSITIONS = {
    None: {"initialized"},
    "initialized": {"ready", "preflight-blocked"},
    "preflight-blocked": {"ready", "implemented"},
    "ready": {"implemented", "pre-review-passed"},
    "implemented": {"pre-review-passed"},
    "edited-unverified": {"pre-review-passed"},
    "pre-review-passed": {"needs-fix", "reviewed"},
    "needs-fix": {"edited-unverified", "reviewed"},
    "reviewed-with-findings": {"gates-passed", "edited-unverified"},
    "reviewed": {"gates-passed", "edited-unverified"},
    "gates-passed": {"verified", "edited-unverified"},
    "verified": {"edited-unverified", "verified"},
    "blocked": {"ready", "edited-unverified", "pre-review-passed"},
    "handoff": {"pre-review-passed", "needs-fix", "reviewed"},
    "capped": set(),
}


def sha(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def require(value, message):
    state.require(value, message)


def unique_ids(entries, label):
    require(isinstance(entries, list), f"Missing {label}")
    ids = []
    for entry in entries:
        require(isinstance(entry, dict), f"Invalid {label} entry")
        state.text(entry.get("id"), label + " ID")
        ids.append(entry["id"])
    require(len(ids) == len(set(ids)), f"Duplicate {label} IDs")
    return set(ids)


def read_policy(value):
    if value is None:
        return ()
    if "_read_policy" in value:
        return value["_read_policy"]
    return declared_read_policy(value)


def declared_read_policy(value):
    return tuple(sorted((value["repo_paths"][repo], tuple(sorted(paths)))
                        for repo, paths in value.get("read_excludes", {}).items() if paths))


def with_read_policy(value, inherited):
    by_root = {}
    for root, paths in (*declared_read_policy(value), *inherited):
        by_root.setdefault(root, set()).update(paths)
    return {**value, "_read_policy": tuple(sorted((root, tuple(sorted(paths))) for root, paths in by_root.items()))}


def guard_path(path, value, *, scope=False):
    state.guard_excluded_paths(path, read_policy(value), scope=scope)
    return Path(path)


def guarded_resolve(path, value):
    require(isinstance(path, (str, Path)) and Path(path).is_absolute(), "Expected absolute path")
    policies = read_policy(value)
    resolved = state.guard_excluded_paths(Path(path).absolute(), policies, pinned=bool(policies))
    return resolved if policies else resolved.resolve()


def run_local_evidence_path(path, root, value, label):
    require(isinstance(path, str) and Path(path).is_absolute(), f"{label} needs an absolute path")
    require(guarded_resolve(path, value).is_relative_to(guarded_resolve(root, value)),
            f"{label} must be run-local")
    return absolute_file(path, value)


def require_writable_run(path, contract_path, value):
    path = Path(path).absolute()
    policies = read_policy(value)
    state.guard_excluded_paths(path, policies, pinned=bool(policies))
    output_run = state.output_run_root(path, policies)
    require(output_run is None or output_run == guarded_resolve(contract_path, value).parent,
            "Writable path cannot be inside a different run")


def guarded_entries(directory, value):
    """List names and types through a pinned descriptor after applying the read policy."""
    directory = Path(directory).absolute()
    policies = read_policy(value)
    guarded = state.guard_excluded_paths(directory, policies, pinned=bool(policies))
    if not policies:
        guarded = guarded.resolve()
    try:
        with state.open_directory(guarded) as fd:
            with os.scandir(fd) as entries:
                names = sorted(entry.name for entry in entries)
            result = []
            for name in names:
                state.guard_excluded_paths(directory / name, policies, pinned=bool(policies))
                result.append((name, os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode))
            return result
    except FileNotFoundError:
        return []


def guarded_names(directory, value):
    return [name for name, _ in guarded_entries(directory, value)]


def slice_capture_paths(value, repo, patterns):
    """Enumerate non-Git slice scope without opening excluded paths or following directories through symlinks."""
    root = guarded_resolve(value["repo_paths"][repo], value)
    tables = state.policy_tables(read_policy(value), pinned=True)
    names = source_names(value, repo)
    found = set()
    visited = set()
    for table in tables:
        if not table.root.is_relative_to(root):
            continue
        for exclusion in table.read_excludes:
            excluded = (table.root / exclusion).relative_to(root).as_posix()
            parts = excluded.split("/")
            ancestors = ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]
            for pattern in patterns:
                prefix = "/".join(scope_prefix(pattern))
                require(not any(in_scope(path, [pattern], names) for path in ancestors)
                        and not (prefix and names.intersects(prefix, [excluded])),
                        "Slice write scope intersects read_excludes")

    def walk(directory):
        if directory in visited:
            return
        visited.add(directory)
        try:
            state.check_path_exclusions(directory, tables)
        except ValueError as error:
            if str(error) == "Path intersects read_excludes":
                return
            raise
        try:
            with state.open_directory(directory) as descriptor:
                with os.scandir(descriptor) as entries:
                    children = sorted(entry.name for entry in entries)
                for name in children:
                    path = directory / name
                    try:
                        state.check_path_exclusions(path, tables)
                    except ValueError as error:
                        if str(error) == "Path intersects read_excludes":
                            continue
                        raise
                    mode = os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode
                    relative = path.relative_to(root).as_posix()
                    if stat.S_ISDIR(mode):
                        walk(path)
                    elif in_scope(relative, patterns, names):
                        found.add(relative)
        except FileNotFoundError:
            return

    for pattern in patterns:
        prefix = "/".join(scope_prefix(pattern))
        candidate = root / prefix if prefix else root
        if prefix and in_scope(prefix, [pattern], names):
            try:
                state.check_path_exclusions(candidate, tables)
                mode = state.capture_lstat(candidate).st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(mode):
                found.add(prefix)
                continue
        walk(candidate)
    return sorted(found)


def guarded_history(directory, value):
    directory = Path(directory).absolute()
    events = []
    for name in guarded_names(directory, value):
        match = re.fullmatch(r"state-(\d+)\.json", name)
        if match:
            path = directory / name
            policies = read_policy(value)
            guarded = state.guard_excluded_paths(path, policies, pinned=bool(policies))
            if not policies:
                guarded = guarded.resolve()
            with state.capture_open(guarded) as handle:
                record = json.load(handle)
            require(isinstance(record, dict), "Malformed lifecycle event")
            events.append((int(match[1]), path, record))
    events.sort()
    require([event[0] for event in events] == list(range(1, len(events) + 1)), "Broken lifecycle event sequence")
    return events


def guarded_sha(path, value):
    policies = read_policy(value)
    if not policies:
        return sha(path)
    guarded = state.guard_excluded_paths(Path(path).absolute(), policies, pinned=True)
    with state.capture_open(guarded) as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def guarded_load(path, value):
    policies = read_policy(value)
    if not policies:
        return state.load(path)
    guarded = state.guard_excluded_paths(Path(path).absolute(), policies, pinned=True)
    with state.capture_open(guarded) as handle:
        return json.load(handle)


def guarded_bytes(path, value):
    policies = read_policy(value)
    if not policies:
        return Path(path).read_bytes()
    guarded = state.guard_excluded_paths(Path(path).absolute(), policies, pinned=True)
    return state.capture_bytes(guarded)


def guarded_exists(path, value):
    path = Path(path).absolute()
    policies = read_policy(value)
    if not policies:
        require(not path.is_symlink(), f"Artifact cannot be a symlink: {path}")
        return path.exists()
    state.guard_excluded_paths(path, policies, pinned=True)
    parent = state.guard_excluded_paths(path.parent, policies, pinned=True)
    try:
        with state.open_directory(parent) as fd:
            info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            require(not stat.S_ISLNK(info.st_mode), f"Artifact cannot be a symlink: {path}")
        return True
    except FileNotFoundError:
        return False


def setup_history(contract_path, value, *, closed=False, require_telemetry=True, transaction_temp=None):
    """Inspect all run artifacts before permitting a setup-only replacement."""
    root = Path(contract_path).parent
    policies = read_policy(value)
    pinned = bool(policies)
    state.guard_excluded_paths(root.absolute(), policies, pinned=pinned)
    files, telemetry, seals = {}, {}, []
    prohibited = {"evidence", "reviews", "review", "rounds", "batches", "budget-grants", "activity-started.json",
                  "completion.json"}
    def scan(directory_fd, directory):
        before = os.fstat(directory_fd)
        with os.scandir(directory_fd) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            path = directory / name
            state.guard_excluded_paths(path.absolute(), policies, pinned=pinned)
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            require(not stat.S_ISLNK(info.st_mode), f"Setup history contains a symlink: {path}")
            relative = path.relative_to(root)
            if closed and relative == Path("setup-replacement.json"):
                continue
            if transaction_temp is not None and path == transaction_temp:
                require(stat.S_ISREG(info.st_mode), "Invalid replacement transaction temporary file")
                continue
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                try:
                    require(os.path.samestat(info, os.fstat(child)), "Setup directory changed during inspection")
                    scan(child, path)
                finally:
                    os.close(child)
                continue
            require(relative != Path("setup-replacement.json") and not any(part in prohibited for part in relative.parts)
                    and not re.fullmatch(r"state-\d+\.json", name),
                    f"Run contains lifecycle or work artifacts: {path}")
            require(stat.S_ISREG(info.st_mode), f"Unexpected setup artifact: {path}")
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            with os.fdopen(fd, "rb") as handle:
                opened = os.fstat(handle.fileno())
                require(os.path.samestat(info, opened) and stat.S_ISREG(opened.st_mode),
                        "Setup artifact changed during inspection")
                content = handle.read() if path.suffix == ".json" else None
                digest = hashlib.sha256(content).hexdigest() if content is not None else hashlib.file_digest(handle, "sha256").hexdigest()
                after = os.fstat(handle.fileno())
                require((opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                        == (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                        "Setup artifact changed during inspection")
            files[str(path)] = digest
            if relative.parts[0] == "telemetry":
                require(len(relative.parts) == 2 and path.suffix == ".json", "Invalid setup telemetry path")
                event = json.loads(content)
                require(isinstance(event, dict) and event.get("schema_version") == VERSION
                        and event.get("run_id") == value["run_id"] and event.get("workflow") == value["workflow"]
                        and event.get("repo") is None and event.get("phase") == "setup"
                        and event.get("event") in {"phase-start", "phase-end"}, "Non-setup telemetry exists")
                identifier = event.get("phase_id")
                require(isinstance(identifier, str) and re.fullmatch(r"setup-[A-Za-z0-9_.-]+", identifier),
                        "Invalid setup phase identifier")
                suffix = "start" if event["event"] == "phase-start" else "end"
                require(path.name == f"{identifier}-{suffix}.json", "Invalid setup telemetry filename")
                telemetry.setdefault(identifier, {})[suffix] = (path, event)
            elif path.suffix == ".json" and relative not in {Path("run.json"), Path("contract.json")}:
                try:
                    record = json.loads(content)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    schedules = value.get("slice_schedules", {})
                    declared = [item.get("path") for item in schedules.values() if isinstance(item, dict)] if isinstance(schedules, dict) else []
                    require(relative.parts[0] != "setup-seals" and str(path) in declared,
                            f"Cannot inspect malformed setup artifact: {path}")
                    lifecycle_states = (set(TRANSITIONS) - {None}) | TERMINAL | {"accepted", "reviewed-with-findings"}
                    status_pattern = rb'"status"\s*:\s*"(' + b"|".join(
                        re.escape(status.encode()) for status in sorted(lifecycle_states)) + rb')"'
                    require(re.search(status_pattern, content) is None,
                            f"Malformed setup artifact contains a work event: {path}")
                    continue
                if relative.parts[0] == "setup-seals":
                    require(len(relative.parts) == 2, "Invalid setup seal path")
                    seals.append((path, record))
                pending = [record]
                while pending:
                    item = pending.pop()
                    if isinstance(item, list):
                        pending.extend(item)
                    elif isinstance(item, dict):
                        is_event = item.get("run_id") == value["run_id"] or "event" in item
                        if is_event:
                            require(not ({"review_fix_count", "gate_fix_count", "budget_grant_applied", "original_agent_id",
                                          "author_agent_id", "review_path", "gate_evidence_paths", "gate_fix_budget",
                                          "review_budget"} & item.keys()),
                                    f"Run contains work or budget evidence: {path}")
                            status = item.get("status")
                            require(item.get("phase", "setup") == "setup"
                                    and (not isinstance(status, str) or status not in
                                         (set(TRANSITIONS) | TERMINAL | {"accepted", "reviewed-with-findings"})),
                                    f"Run contains a work event: {path}")
                        pending.extend(item.values())
        after = os.fstat(directory_fd)
        require((before.st_mtime_ns, before.st_ctime_ns) == (after.st_mtime_ns, after.st_ctime_ns),
                "Setup directory changed during inspection")

    guarded_root = guarded_resolve(root.absolute(), value)
    with state.open_directory(guarded_root) as root_fd:
        scan(root_fd, root)
    require(telemetry or not require_telemetry, "Run has no setup telemetry")
    for pair in telemetry.values():
        require("start" in pair, "Setup telemetry is missing its start")
        if "end" in pair:
            start, end = pair["start"], pair["end"]
            require(end[1].get("start_sha256") == files[str(start[0])]
                    and end[1].get("status") in {"passed", "failed"}
                    and all(start[1].get(key) == end[1].get(key) for key in
                            ("run_id", "workflow", "repo", "phase", "phase_id", "session_id"))
                    and timestamp(start[1].get("at")) <= timestamp(end[1].get("at")), "Invalid setup telemetry end")
    for path, seal in seals:
        require(isinstance(seal, dict) and seal.get("schema_version") == VERSION
                and seal.get("run_id") == value["run_id"] and seal.get("phase_id") in telemetry
                and path.name == seal["phase_id"] + ".json"
                and seal.get("contract_sha256") == files.get(str(contract_path)), "Setup contract seal mismatch")
    return dict(sorted(files.items()))


def replacement_chain(value, contract_path, *, pending=None):
    """Verify the immutable links back through replaced setup runs."""
    records, seen = [], {guarded_resolve(contract_path, value)}
    current, current_path = value, Path(contract_path).absolute()
    policy = read_policy(value)
    while current.get("setup_replacement") is not None:
        link = current["setup_replacement"]
        require(isinstance(link, dict) and set(link) == {"contract_path", "contract_sha256", "reason", "files"},
                "Invalid setup replacement declaration")
        state.text(link["reason"], "setup replacement reason")
        previous = absolute_file(link["contract_path"], {"_read_policy": policy})
        previous_identity = guarded_resolve(previous, {"_read_policy": policy})
        require(previous.name == "contract.json" and previous_identity not in seen,
                "Setup replacement cycle or invalid contract path")
        seen.add(previous_identity)
        require(guarded_sha(previous, {"_read_policy": policy}) == link["contract_sha256"], "Replaced contract SHA-256 mismatch")
        old = guarded_load(previous, {"_read_policy": policy})
        require(isinstance(old, dict) and old.get("schema_version") == VERSION
                and old.get("run_id") == previous.parent.name
                and old.get("workflow") == current.get("workflow")
                and old.get("repos") == current.get("repos"), "Setup replacement identity mismatch")
        old = with_read_policy(old, policy)
        policy = read_policy(old)
        guard_path(previous, old)
        meta_path = guard_path(previous.parent / "run.json", old)
        meta = guarded_load(meta_path, old)
        require(meta.get("schema_version") == VERSION and meta.get("run_id") == old["run_id"]
                and meta.get("workflow") == old["workflow"] and meta.get("repos") == old["repos"],
                "Replaced run metadata mismatch")
        for repo in old["repos"]:
            require(old.get("budget_work_items", {}).get(repo)
                    and old["budget_work_items"][repo] == current.get("budget_work_items", {}).get(repo),
                    "Setup replacement work item mismatch")
            state.text(old["budget_work_items"][repo], "setup replacement work item")
            old_repo = old.get("repo_paths", {}).get(repo)
            new_repo = current.get("repo_paths", {}).get(repo)
            require(isinstance(old_repo, str) and Path(old_repo).is_absolute()
                    and isinstance(new_repo, str) and Path(new_repo).is_absolute(),
                    "Setup replacement requires absolute repository paths")
            require(guarded_resolve(old_repo, old) == guarded_resolve(new_repo, old),
                    "Setup replacement repository mismatch")
        closure_path = guard_path(previous.parent / "setup-replacement.json", old)
        guard_path(current_path, old)
        pending_link = pending is not None and previous == pending["old"]
        successor_hash = pending["successor_sha256"] if pending_link else guarded_sha(current_path, old)
        closure = {"schema_version": VERSION, "status": "replaced", "run_id": old["run_id"],
                   "workflow": old["workflow"], "replacement": link,
                   "work_items": old["budget_work_items"], "repo_paths": old["repo_paths"],
                   "successor_run_id": current["run_id"], "successor_contract_path": str(guarded_resolve(current_path, old)),
                   "successor_contract_sha256": successor_hash}
        if pending_link:
            actual = pending["closure"]
        else:
            actual = guarded_load(closure_path, old)
        require(actual == closure and (pending_link or guarded_bytes(closure_path, old) == json_bytes(closure)),
                "Setup replacement closure mismatch")
        require(setup_history(previous, old, closed=True,
                              transaction_temp=pending.get("temporary_path") if pending_link else None)
                == link["files"], "Replaced setup artifacts changed")
        records.append({"run_id": old["run_id"], "reason": link["reason"],
                        "contract_path": str(previous), "contract_sha256": link["contract_sha256"],
                        "closure_path": str(closure_path), "closure_sha256":
                        (pending["closure_sha256"] if pending_link
                         else guarded_sha(closure_path, old)), "files": link["files"]})
        current, current_path = old, previous
    return records


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=True, sort_keys=True) + "\n").encode()


def publish_once(path, payload, value, *, temporary_path=None):
    policies = read_policy(value)
    path = state.guard_excluded_paths(path.absolute(), policies, pinned=bool(policies))
    temporary = state.guard_excluded_paths(temporary_path or path.parent / (".setup-replacement-" + secrets.token_hex(16)),
                                           policies, pinned=bool(policies))
    require(temporary.parent == path.parent, "Temporary file must share the destination directory")
    parent_path = guarded_resolve(path.parent, value)
    with state.open_directory(parent_path) as parent:
        created = False
        try:
            fd = os.open(temporary.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666, dir_fd=parent)
            created = True
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary.name, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.fsync(parent)
        finally:
            if created:
                os.unlink(temporary.name, dir_fd=parent)


def mark_work_started(contract_path, repo=None):
    """Record that an external helper may perform work after releasing the run lock."""
    initial = state.load(contract_path)
    require(isinstance(initial, dict), "Invalid contract")
    root = guarded_resolve(contract_path, initial).parent
    state.guard_excluded_paths(root, read_policy(initial), pinned=bool(read_policy(initial)))
    with ExitStack() as stack:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        stack.callback(fcntl.flock, fd, fcntl.LOCK_UN)
        value = guarded_load(contract_path, initial)
        require(value == initial, "Contract changed while acquiring the run lock")
        closure = state.guard_excluded_paths(root / "setup-replacement.json", read_policy(value),
                                             pinned=bool(read_policy(value)))
        require(not guarded_exists(closure, value), "This setup run has been replaced")
        require_unreserved(value, contract_path, repo)
        marker = state.guard_excluded_paths(root / "activity-started.json", read_policy(value),
                                            pinned=bool(read_policy(value)))
        if not guarded_exists(marker, value):
            publish_once(marker, json_bytes({"schema_version": VERSION, "run_id": value.get("run_id", root.name),
                                             "contract_sha256": guarded_sha(contract_path, value)}), value)


@contextmanager
def prepare_run_artifact(contract_path, repo=None):
    value = state.load(contract_path)
    root = guarded_resolve(contract_path, value).parent
    policies = read_policy(value)
    state.guard_excluded_paths(root, policies, pinned=bool(policies))
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        current = guarded_load(contract_path, value)
        require(current == value, "Contract changed while acquiring the run lock")
        require(not guarded_exists(root / "setup-replacement.json", value), "This setup run has been replaced")
        require_unreserved(value, contract_path, repo)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def require_unreserved(value, contract_path, repo=None):
    repos = [repo] if repo else value.get("repos", [])
    root = guarded_resolve(contract_path, value).parent
    for name in repos:
        state.relative_path(name)
        path = state.guard_excluded_paths(root / name / "successor-reservation.json",
                                          read_policy(value), pinned=bool(read_policy(value)))
        require(not guarded_exists(path, value), "Predecessor has been reserved for a successor")


def reserve_predecessors(draft, new_path, successor_hash, policy_value, *, lineage_cache=None):
    draft = with_read_policy(draft, read_policy(policy_value))
    if lineage_cache is None:
        lineage_cache = {}
    groups = {}
    prepared = []
    replaced = set()
    previous = draft
    inherited = read_policy(draft)
    while previous.get("setup_replacement") is not None:
        link = previous["setup_replacement"]
        source = absolute_file(link.get("contract_path"), {"_read_policy": inherited})
        identity = guarded_resolve(source, {"_read_policy": inherited})
        require(identity not in replaced, "Setup replacement cycle")
        replaced.add(identity)
        previous = with_read_policy(guarded_load(source, {"_read_policy": inherited}), inherited)
        inherited = read_policy(previous)

    def add(source, repo, work_item, contract_hash, state_path, state_hash, history_hash, slice_id, source_policy=None):
        source_policy = source_policy or policy_value
        source = absolute_file(source, source_policy)
        policies = read_policy(source_policy)
        source = state.guard_excluded_paths(source.absolute(), policies, pinned=bool(policies))
        if not policies:
            source = source.resolve()
        state_file = state.guard_excluded_paths(Path(state_path).absolute(), policies, pinned=bool(policies))
        if not policies:
            state_file = state_file.resolve()
        scope = {"slice_id": slice_id, "state_path": str(state_file), "state_sha256": state_hash,
                 "history_sha256": history_hash}
        key = (source, repo, work_item, contract_hash)
        group = groups.setdefault(key, {"scopes": [], "policies": []})
        group["policies"].extend(policies)
        existing = group["scopes"]
        duplicate = next((entry for entry in existing if entry["slice_id"] == slice_id), None)
        require(duplicate is None or duplicate == scope, "Conflicting predecessor scope snapshots")
        if duplicate is None:
            existing.append(scope)

    for item in draft.get("budget_continuity", []):
        repo = item["repo"]
        if "recovery_sources" in item:
            references = [(source["contract_path"], source["contract_sha256"], source["state_path"],
                           source["state_sha256"], source["history_sha256"], None)
                          for source in item["recovery_sources"]]
        else:
            references = [(item["source_contract_path"], item["source_contract_sha256"], item["source_state_path"],
                           item["source_state_sha256"], item.get("source_history_sha256"), item.get("source_slice_id"))]
        for raw, contract_hash, state_path, state_hash, history_hash, slice_id in references:
            add(raw, repo, item["work_item"], contract_hash, state_path, state_hash, history_hash, slice_id)
    for repo in draft["repos"]:
        lineage = GateFixLineage(draft, repo, lineage_cache)
        budget_continuity(draft, repo, new_path, gate_lineage=lineage)
        for (source, cached_repo, inherited_policy), (audited, _, _, _) in lineage.cache.items():
            if cached_repo == repo:
                source_policy = {"_read_policy": inherited_policy}
                add(source, repo, draft["budget_work_items"][repo], guarded_sha(source, source_policy),
                    audited["state_path"], audited["state_sha256"], audited["history_sha256"], None, source_policy)
        for item in draft.get("budget_continuity", []):
            if item.get("repo") != repo or item.get("slice_id") is None:
                continue
            current = item
            inherited = read_policy(policy_value)
            while current:
                source = absolute_file(current["source_contract_path"], {"_read_policy": inherited})
                source_value = with_read_policy(guarded_load(source, {"_read_policy": inherited}), inherited)
                inherited = read_policy(source_value)
                slice_id = current["source_slice_id"]
                audited = audit_slice_history(source, repo, slice_id, inherited_policy=inherited, strict=True)
                add(source, repo, item["work_item"], guarded_sha(source, source_value), audited["state_path"],
                    audited["state_sha256"], audited["history_sha256"], slice_id, source_value)
                current = next((entry for entry in source_value.get("budget_continuity", [])
                                if entry.get("repo") == repo and entry.get("slice_id") == slice_id), None)
    for (source, repo, work_item, contract_hash), group in groups.items():
        scopes = group["scopes"]
        inherited = (*read_policy(policy_value), *group["policies"])
        old = with_read_policy(guarded_load(source, {"_read_policy": inherited}), inherited)
        scope_states = {scope["slice_id"]: guarded_load(absolute_file(scope["state_path"], old), old) for scope in scopes}
        telemetry = source.parent / "telemetry"
        phases = {}
        phase_seals = {}
        for name in guarded_names(telemetry, old):
            if not name.endswith(".json"):
                continue
            event = guarded_load(telemetry / name, old)
            require(isinstance(event, dict), "Malformed predecessor telemetry")
            require(event.get("schema_version") == VERSION and event.get("run_id") == old.get("run_id")
                    and event.get("workflow") == old.get("workflow") and event.get("repo") in {None, *old["repos"]},
                    "Predecessor telemetry identity mismatch")
            require(event.get("phase") in {"setup", "preflight", "implement", "pre-review", "review", "review-fix",
                                           "gate-fix", "checker", "gates", "checkpoint-wait"},
                    "Unknown predecessor telemetry phase")
            if event.get("repo") in {None, repo} and event.get("action") in {"review-fix", "plan-gap", "gate-fix"}:
                expected_phase = "gate-fix" if event["action"] == "gate-fix" else "review-fix"
                require(event.get("phase") == expected_phase, "Conflicting predecessor phase action")
            if event.get("repo") not in {None, repo} or event.get("phase") not in {"implement", "review", "review-fix", "gate-fix"}:
                continue
            identifier = event.get("phase_id")
            require(isinstance(identifier, str), "Invalid predecessor phase")
            phase_seals[str(telemetry / name)] = guarded_sha(telemetry / name, old)
            pair = phases.setdefault(identifier, {})
            kind = event.get("event")
            require(kind in {"phase-start", "phase-end"} and kind not in pair, "Invalid predecessor phase event")
            suffix = "start" if kind == "phase-start" else "end"
            require(name == f"{identifier}-{suffix}.json", "Invalid predecessor phase filename")
            pair[kind] = (telemetry / name, event)
        matched_repairs = set()
        for phase in phases.values():
            require("phase-start" in phase and "phase-end" in phase, "Predecessor has an open work phase")
            start_path, start = phase["phase-start"]
            _, end = phase["phase-end"]
            require(not (start.get("phase") == "implement" and start.get("action") in
                         {"review-fix", "plan-gap", "gate-fix"}), "Conflicting predecessor phase action")
            require(start.get("schema_version") == VERSION and start.get("run_id") == old.get("run_id")
                    and start.get("workflow") == old.get("workflow")
                    and end.get("schema_version") == VERSION and end.get("status") in {"passed", "failed", "blocked", "interrupted"}
                    and end.get("start_sha256") == guarded_sha(start_path, old)
                    and all(start.get(key) == end.get(key) for key in
                            ("run_id", "workflow", "repo", "phase", "phase_id", "slice_id", "session_id"))
                    and timestamp(start.get("at")) <= timestamp(end.get("at")), "Invalid predecessor phase pair")
            phase_slice = start.get("slice_id")
            latest = scope_states.get(phase_slice)
            if latest is None and phase_slice is not None:
                slice_events = guarded_history(source.parent / repo / "slices" / state.relative_path(phase_slice), old)
                latest = slice_events[-1][2] if slice_events else None
            require(latest is not None and timestamp(end["at"]) <= timestamp(latest.get("at")),
                    "Predecessor work has not been recorded in lifecycle history")
            if start["phase"] in {"review-fix", "gate-fix"}:
                legacy_run_scope = (start.get("repo") is None and len(old["repos"]) == 1
                                    and start.get("repair_source") is None and end.get("repair_source") is None)
                require(start.get("repo") == repo or legacy_run_scope,
                        f"Predecessor repair phase {start['phase_id']} has invalid repo")
                scope_directory = source.parent / repo
                if phase_slice is not None:
                    scope_directory = scope_directory / "slices" / state.relative_path(phase_slice)
                events = guarded_history(scope_directory, old)
                actions = {"gate-fix"} if start["phase"] == "gate-fix" else {"review-fix", "plan-gap"}
                for field in ("action", "previous_state_path", "previous_state_sha256", "round", "attempt", "capsule_path"):
                    require(start.get(field) == end.get(field), "Predecessor repair phase boundary mismatch")
                for field in ("tool_use_id", "request_id"):
                    require(start.get(field) is None or end.get(field) is None or start[field] == end[field],
                            f"Predecessor repair phase {start['phase_id']} has conflicting {field}")
                matches = []
                for index, (_, _, event) in enumerate(events):
                    if not index or event.get("status") != "edited-unverified" or event.get("action") not in actions:
                        continue
                    identifier = event.get("repair_phase_id")
                    if identifier is not None and identifier != start["phase_id"]:
                        continue
                    previous = events[index - 1][2]
                    if timestamp(previous.get("at")) <= timestamp(start.get("at")) <= timestamp(end.get("at")) <= timestamp(event.get("at")):
                        matches.append((index, event))
                if not matches and end["status"] != "passed":
                    require(not legacy_run_scope,
                            f"Predecessor repair phase {start['phase_id']} lacks repo-bound source evidence")
                    anchor = [(path, event) for _, path, event in events
                              if start.get("previous_state_path") and
                              guarded_resolve(path, old) == guarded_resolve(start["previous_state_path"], old)]
                    require(len(anchor) == 1 and guarded_sha(anchor[0][0], old) == start.get("previous_state_sha256"),
                            f"Predecessor repair phase {start['phase_id']} has invalid predecessor state")
                    previous = anchor[0][1]
                    author = (previous.get("author_agent_id") if phase_slice is not None else
                              previous.get("current_author_agent_id") or previous.get("original_agent_id"))
                    require(start.get("agent_id") == author and bool(author),
                            f"Predecessor repair phase {start['phase_id']} has invalid author")
                    if end.get("agent_id") is not None and end.get("agent_id") != author:
                        capsule_path = run_local_evidence_path(start.get("capsule_path"), source.parent, old,
                                                              "Partial repair capsule")
                        capsule = guarded_load(capsule_path, old)
                        bound(old, repo, capsule)
                        capsule_manifest_path = run_local_evidence_path(capsule.get("manifest_path"), source.parent,
                                                                       old, "Repair capsule manifest")
                        capsule_manifest = manifest(old, repo, capsule_manifest_path)
                        require(old.get("author_rotation") == "capsule" and capsule.get("written_by") == author
                                and capsule.get("source_fingerprint") == previous.get("source_fingerprint")
                                and capsule_manifest["fingerprint"] == capsule.get("source_fingerprint")
                                and start.get("capsule_sha256") == guarded_sha(capsule_path, old)
                                and end.get("capsule_sha256") == start.get("capsule_sha256")
                                and start.get("capsule_manifest_sha256") == guarded_sha(capsule_manifest_path, old)
                                and end.get("capsule_manifest_sha256") == start.get("capsule_manifest_sha256")
                                and (phase_slice is None or capsule.get("slice_id") == phase_slice),
                                f"Predecessor repair phase {start['phase_id']} has invalid author handoff")
                    if start.get("capsule_path"):
                        require(end.get("agent_id") is not None and end.get("agent_id") != author,
                                f"Predecessor repair phase {start['phase_id']} has invalid fresh author")
                    repair_author = end.get("agent_id") or author
                    require(timestamp(previous["at"]) <= timestamp(start["at"]),
                            f"Predecessor repair phase {start['phase_id']} has invalid predecessor timestamp")
                    action = start.get("action")
                    require(action in actions, f"Predecessor repair phase {start['phase_id']} has invalid action")
                    if action == "gate-fix":
                        require(start.get("attempt") == previous.get("gate_fix_count", 0) + 1,
                                f"Predecessor repair phase {start['phase_id']} has invalid attempt")
                    else:
                        require(start.get("round") == previous.get("latest_review_round", 0),
                                f"Predecessor repair phase {start['phase_id']} has invalid round")
                    snapshots = [boundary.get("repair_source") for boundary in (start, end)]
                    for boundary, snapshot in zip((start, end), snapshots):
                        require(isinstance(snapshot, dict) and snapshot.get("contract_sha256") == contract_hash
                                and isinstance(snapshot.get("manifest"), dict),
                                f"Predecessor repair phase {start['phase_id']} lacks source evidence")
                        capture = snapshot["manifest"]
                        require(all(key in capture for key in ("repo_path", "head", "files", "fingerprint"))
                                and capture["fingerprint"] == state.digest({key: capture[key] for key in
                                                                           ("repo_path", "head", "files")})
                                and capture["repo_path"] == str(guarded_resolve(old["repo_paths"][repo], old))
                                and timestamp(boundary["at"]) <= timestamp(snapshot.get("observed_at"))
                                <= timestamp(latest["at"]),
                                f"Predecessor repair phase {start['phase_id']} has invalid source evidence")
                    require(timestamp(snapshots[0]["observed_at"]) <= timestamp(end["at"])
                            <= timestamp(snapshots[1]["observed_at"]),
                            f"Predecessor repair phase {start['phase_id']} has invalid capture order")
                    if phase_slice is not None and snapshots[0]["manifest"]["head"] is None:
                        schedule, _ = checked_slice_schedule(old, repo, old["slice_schedules"][repo]["path"])
                        item = next((entry for entry in schedule["slices"] if entry["id"] == phase_slice), None)
                        require(item is not None and all(snapshot.get("scope_coverage") == item["write_scope"]
                                                         for snapshot in snapshots),
                                f"Predecessor repair phase {start['phase_id']} has incomplete source coverage")
                    candidates = []
                    for other in phases.values():
                        if "phase-start" not in other or "phase-end" not in other or other is phase:
                            continue
                        other_start = other["phase-start"][1]
                        if (other_start.get("repo") == repo and other_start.get("slice_id") == phase_slice
                                and other_start.get("previous_state_path") == start.get("previous_state_path")
                                and other_start.get("previous_state_sha256") == start.get("previous_state_sha256")
                                and other_start.get("action") == action):
                            candidates.append(other)
                    before = [other for other in candidates
                              if timestamp(other["phase-start"][1]["at"]) < timestamp(start["at"])]
                    after = [other for other in candidates
                             if timestamp(other["phase-start"][1]["at"]) > timestamp(start["at"])]
                    names = repo_read_excludes(old, repo)
                    if before:
                        prior = max(before, key=lambda other: timestamp(other["phase-start"][1]["at"]))
                        prior_end = prior["phase-end"][1]
                        prior_capture = prior_end.get("repair_source", {}).get("manifest")
                        require(timestamp(prior_end["at"]) <= timestamp(start["at"])
                                and isinstance(prior_capture, dict)
                                and state.same_source(prior_capture, snapshots[0]["manifest"], names),
                                f"Predecessor repair phase {start['phase_id']} has a source evidence gap")
                    else:
                        require(state.same_source(event_manifest(old, repo, previous), snapshots[0]["manifest"], names),
                                f"Predecessor repair phase {start['phase_id']} differs from predecessor source")
                    require(after, f"Predecessor repair phase {start['phase_id']} has no recorded retry")
                    following = min(after, key=lambda other: timestamp(other["phase-start"][1]["at"]))
                    following_start = following["phase-start"][1]
                    for other in phases.values():
                        if other is phase or other is following or "phase-start" not in other or "phase-end" not in other:
                            continue
                        other_start, other_end = other["phase-start"][1], other["phase-end"][1]
                        if other_start.get("phase") not in {"implement", "review-fix", "gate-fix"}:
                            continue
                        following_end = following["phase-end"][1]
                        require(timestamp(other_end.get("repair_source", {}).get("observed_at", other_end["at"]))
                                <= timestamp(start["at"])
                                or timestamp(other_start["at"]) >= timestamp(
                                    following_end.get("repair_source", {}).get("observed_at", following_end["at"])),
                                f"Predecessor repair phase {start['phase_id']} overlaps another writer")
                    following_capture = following_start.get("repair_source", {}).get("manifest")
                    references = following_start.get("prior_repair_boundaries", {})
                    require(timestamp(snapshots[1]["observed_at"]) <= timestamp(following_start["at"])
                            and isinstance(following_capture, dict)
                            and state.same_source(snapshots[1]["manifest"], following_capture, names)
                            and isinstance(references, dict)
                            and all(any(guarded_resolve(path, old) == guarded_resolve(boundary, old)
                                            and digest == guarded_sha(boundary, old)
                                            for path, digest in references.items())
                                    for boundary in (start_path, phase["phase-end"][0])),
                            f"Predecessor repair phase {start['phase_id']} has unaccounted partial changes or retry gap")
                    if not state.same_source(snapshots[0]["manifest"], snapshots[1]["manifest"], names):
                        recovery_path = run_local_evidence_path(following_start.get("repair_recovery_path"),
                                                               source.parent, old, "Partial repair recovery")
                        recovery = guarded_load(recovery_path, old)
                        bound(old, repo, recovery)
                        require(recovery.get("kind") == "ship-partial-repair-recovery-v1"
                                and end.get("end_reason") in {"context", "api-error", "user-stop", "timeout", "unknown"}
                                and recovery.get("slice_id") == phase_slice
                                and recovery.get("action") == action
                                and recovery.get("phase_id") == start["phase_id"]
                                and recovery.get("start_sha256") == guarded_sha(start_path, old)
                                and recovery.get("end_sha256") == guarded_sha(phase["phase-end"][0], old)
                                and recovery.get("before_fingerprint") == snapshots[0]["manifest"]["fingerprint"]
                                and recovery.get("partial_fingerprint") == snapshots[1]["manifest"]["fingerprint"]
                                and recovery.get("author_agent_id") == repair_author
                                and recovery.get("retry_phase_id") == following_start["phase_id"]
                                and following_start.get("repair_recovery_sha256") == guarded_sha(recovery_path, old)
                                and timestamp(end["at"]) <= timestamp(recovery.get("at")) <= timestamp(following_start["at"]),
                                f"Predecessor repair phase {start['phase_id']} has invalid partial recovery evidence")
                        changed = source_names(old, repo).changed(snapshots[0]["manifest"], snapshots[1]["manifest"])
                        declared = recovery.get("changed_paths")
                        require(isinstance(declared, list) and declared and
                                source_names(old, repo).keys(declared) == changed,
                                "Partial repair recovery paths do not match source changes")
                        require(phase_slice is not None and action == "review-fix",
                                "Partial repair recovery requires slice review-fix")
                        schedule, _ = checked_slice_schedule(old, repo, old["slice_schedules"][repo]["path"])
                        item = next((entry for entry in schedule["slices"] if entry["id"] == phase_slice), None)
                        require(item is not None and all(in_scope(path, item["write_scope"], source_names(old, repo))
                                                         for path in declared),
                                "Partial repair recovery changed files outside slice scope")
                        state.text(recovery.get("remaining_work"), "partial repair remaining work")
                        finding_ids = recovery.get("finding_ids")
                        require(phase_slice is not None and action == "review-fix" and isinstance(finding_ids, list)
                                and finding_ids and len(set(finding_ids)) == len(finding_ids),
                                "Partial repair recovery needs slice finding IDs")
                        checkpoint_path = run_local_evidence_path(end.get("checkpoint_path"), source.parent, old,
                                                                 "Partial repair checkpoint")
                        checkpoint = guarded_load(checkpoint_path, old)
                        bound(old, repo, checkpoint)
                        require(guarded_resolve(checkpoint_path, old).is_relative_to(guarded_resolve(source.parent, old))
                                and end.get("checkpoint_sha256") == guarded_sha(checkpoint_path, old)
                                and checkpoint.get("kind") == "ship-partial-repair-checkpoint-v1"
                                and checkpoint.get("slice_id") == phase_slice
                                and checkpoint.get("phase_id") == start["phase_id"]
                                and checkpoint.get("start_sha256") == guarded_sha(start_path, old)
                                and checkpoint.get("author_agent_id") == repair_author
                                and checkpoint.get("previous_state_sha256") == start.get("previous_state_sha256")
                                and checkpoint.get("source_fingerprint") == snapshots[1]["manifest"]["fingerprint"]
                                and checkpoint.get("status") == end["status"]
                                and checkpoint.get("completed") == []
                                and timestamp(start["at"]) <= timestamp(checkpoint.get("at")) <= timestamp(end["at"])
                                and set(finding_ids) <= {entry.get("id") for entry in checkpoint.get("unfinished", [])
                                                         if isinstance(entry.get("reason"), str) and entry["reason"].strip()},
                                "Partial repair checkpoint does not establish unfinished findings")
                        retry_ids = {other["phase-start"][1]["phase_id"] for other in after}
                        recovered = [event for _, _, event in events
                                     if event.get("status") == "edited-unverified"
                                     and event.get("repair_phase_id") in retry_ids]
                        require(len(recovered) == 1, "Partial repair retry lacks a charged lifecycle state")
                        recorded = recovered[0]
                        require(all(any(guarded_resolve(path, old) == guarded_resolve(artifact, old)
                                        and digest == guarded_sha(artifact, old)
                                        for path, digest in recorded.get("artifact_hashes", {}).items())
                                    for artifact in (recovery_path, checkpoint_path)),
                                "Partial repair recovery evidence is not sealed")
                        result = guarded_load(absolute_file(recorded.get("applied_path"), old), old)
                        applied = {entry.get("id") for entry in result.get("applied", [])
                                   if entry.get("evidence_paths")}
                        require(set(finding_ids) <= applied, "Partial repair findings lack confirmed recovery")
                    sealed = any(all(any(guarded_resolve(path, old) == guarded_resolve(boundary, old)
                                             and digest == guarded_sha(boundary, old)
                                             for path, digest in event.get("artifact_hashes", {}).items())
                                         for boundary in (start_path, phase["phase-end"][0]))
                                 for _, _, event in events)
                    require(sealed, f"Predecessor repair phase {start['phase_id']} boundaries are not sealed in history")
                    continue
                require(len(matches) == 1, "Predecessor repair phase is missing from lifecycle history")
                index, event = matches[0]
                key = (phase_slice, index)
                require(key not in matched_repairs, "Multiple repair phases claim the same lifecycle state")
                matched_repairs.add(key)
                for field in ("action", "previous_state_path", "previous_state_sha256", "round", "attempt"):
                    require(start.get(field) == end.get(field), "Predecessor repair phase boundary mismatch")
                if start.get("action") is not None:
                    require(event.get("action") == start["action"], "Predecessor repair phase action mismatch")
                if start.get("previous_state_path") is not None:
                    require(absolute_file(start["previous_state_path"], old).resolve() == events[index - 1][1].resolve()
                            and start.get("previous_state_sha256") == guarded_sha(events[index - 1][1], old),
                            "Predecessor repair phase binding mismatch")
                previous = events[index - 1][2]
                expected_author = (previous.get("author_agent_id") if phase_slice is not None else
                                   previous.get("current_author_agent_id") or previous.get("original_agent_id"))
                if start.get("agent_id") is not None and expected_author is not None:
                    require(start["agent_id"] == expected_author,
                            f"Predecessor repair phase {start['phase_id']} has invalid author")
                if start.get("agent_id") is not None and end.get("agent_id") is not None and not start.get("capsule_path"):
                    require(start["agent_id"] == end["agent_id"],
                            f"Predecessor repair phase {start['phase_id']} has conflicting authors")
                end_source = end.get("repair_source")
                if end_source is not None:
                    capture = end_source.get("manifest") if isinstance(end_source, dict) else None
                    start_source = start.get("repair_source")
                    start_capture = start_source.get("manifest") if isinstance(start_source, dict) else None
                    require(isinstance(capture, dict)
                            and all(key in capture for key in ("repo_path", "head", "files", "fingerprint"))
                            and capture["fingerprint"] == state.digest({key: capture[key] for key in
                                                                       ("repo_path", "head", "files")})
                            and isinstance(start_capture, dict)
                            and all(key in start_capture for key in ("repo_path", "head", "files", "fingerprint"))
                            and start_capture["fingerprint"] == state.digest({key: start_capture[key] for key in
                                                                             ("repo_path", "head", "files")})
                            and start_source.get("contract_sha256") == contract_hash
                            and end_source.get("contract_sha256") == contract_hash
                            and timestamp(start["at"]) <= timestamp(start_source.get("observed_at")) <= timestamp(end["at"])
                            and timestamp(end["at"]) <= timestamp(end_source.get("observed_at")) <= timestamp(event["at"])
                            and state.same_source(capture, event_manifest(old, repo, event), repo_read_excludes(old, repo)),
                            f"Predecessor repair phase {start['phase_id']} result source does not match lifecycle")
                    previous_phases = [other for other in phases.values()
                                       if other is not phase and "phase-start" in other and "phase-end" in other
                                       and other["phase-start"][1].get("repo") == repo
                                       and other["phase-start"][1].get("slice_id") == phase_slice
                                       and other["phase-start"][1].get("previous_state_path") == start.get("previous_state_path")
                                       and other["phase-start"][1].get("action") == start.get("action")
                                       and timestamp(other["phase-start"][1]["at"]) < timestamp(start["at"])]
                    prior_source = event_manifest(old, repo, previous)
                    if previous_phases:
                        prior_phase = max(previous_phases, key=lambda item: timestamp(item["phase-start"][1]["at"]))
                        prior_end = prior_phase["phase-end"][1]
                        prior_source = prior_end.get("repair_source", {}).get("manifest")
                        references = start.get("prior_repair_boundaries", {})
                        require(isinstance(references, dict) and all(
                            any(guarded_resolve(path, old) == guarded_resolve(boundary[0], old)
                                and digest == guarded_sha(boundary[0], old) for path, digest in references.items())
                            for boundary in (prior_phase["phase-start"], prior_phase["phase-end"]))
                            and timestamp(prior_end["at"]) <= timestamp(start["at"]),
                            f"Predecessor repair phase {start['phase_id']} lacks previous phase binding")
                    require(isinstance(prior_source, dict)
                            and state.same_source(prior_source, start_capture, repo_read_excludes(old, repo)),
                            f"Predecessor repair phase {start['phase_id']} starts from unrecorded source")
                counter = "gate_fix_count" if event["action"] == "gate-fix" else "review_fix_count"
                require(event.get(counter, 0) == previous.get(counter, 0) + 1,
                        "Predecessor repair phase counter mismatch")
                if start.get("attempt") is not None:
                    require(start["attempt"] == previous.get("gate_fix_count", 0) + 1,
                            "Predecessor repair phase attempt mismatch")
                if start.get("round") is not None:
                    require(start["round"] == previous.get("latest_review_round", 0),
                            "Predecessor repair phase round mismatch")
        reservation = {"schema_version": VERSION, "source_contract_path": str(source),
                       "source_contract_sha256": contract_hash, "repo": repo, "work_item": work_item,
                       "successor_contract_path": str(guarded_resolve(new_path, policy_value)), "successor_contract_sha256": successor_hash,
                       "scopes": sorted(scopes, key=lambda scope: scope["slice_id"] or ""), "telemetry": phase_seals}
        target = state.guard_excluded_paths((source.parent / repo / "successor-reservation.json").absolute(),
                                            read_policy(old), pinned=bool(read_policy(old)))
        payload = json_bytes(reservation)
        if guarded_exists(target, old):
            existing = guarded_load(target, old)
            if guarded_bytes(target, old) != payload:
                earlier = absolute_file(existing.get("successor_contract_path"), old)
                earlier_identity = guarded_resolve(earlier, old)
                require(earlier_identity in replaced or
                        any(path == earlier_identity and source_repo == repo
                            and source_work == work_item and digest == existing.get("successor_contract_sha256")
                            for path, source_repo, source_work, digest in groups),
                        "Predecessor already reserved for a different successor")
                require(existing.get("schema_version") == VERSION
                        and guarded_resolve(existing.get("source_contract_path"), old) == source
                        and guarded_sha(earlier, old) == existing.get("successor_contract_sha256")
                        and existing.get("source_contract_sha256") == contract_hash and existing.get("repo") == repo
                        and existing.get("work_item") == work_item
                        and existing.get("telemetry") == reservation["telemetry"]
                        and isinstance(existing.get("scopes"), list)
                        and all(scope in reservation["scopes"] for scope in existing["scopes"]),
                        "Historical predecessor reservation mismatch")
        else:
            with state.open_directory(guarded_resolve(target.parent, old)):
                pass
            prepared.append((target, payload, old))
    return prepared


def replace_setup(contract_path, draft_path, reason, *, bootstrap_policy):
    state.text(reason, "setup replacement reason")
    old_path = Path(contract_path).absolute()
    draft_path = Path(draft_path).absolute()
    require(old_path.name == "contract.json" and draft_path.name == "replacement-contract.json"
            and draft_path.parent.name == "inputs", "Invalid replacement paths")
    new_root = draft_path.parent.parent
    new_path = new_root / "contract.json"
    require(old_path.parent != new_root and old_path.parent.parent.parent == new_root.parent.parent,
            "Replacement runs must belong to the same project")
    state.guard_excluded_paths(old_path, bootstrap_policy, pinned=bool(bootstrap_policy))
    state.guard_excluded_paths(draft_path, bootstrap_policy, pinned=bool(bootstrap_policy))
    initial_old = guarded_load(old_path, {"_read_policy": bootstrap_policy})
    require(isinstance(initial_old, dict), "Invalid replaced contract")
    old_policy = declared_read_policy(initial_old)
    state.guard_excluded_paths(draft_path, old_policy, pinned=bool(old_policy))
    initial_draft = guarded_load(draft_path, {"_read_policy": (*bootstrap_policy, *old_policy)})
    require(isinstance(initial_draft, dict), "Invalid replacement draft")
    draft_policy = declared_read_policy(initial_draft)
    supplied = {(root, path) for root, paths in bootstrap_policy for path in paths}
    required = {(root, path) for root, paths in (*old_policy, *draft_policy) for path in paths}
    require(required <= supplied, "Bootstrap read policy omits a contract exclusion")
    guard_path(old_path, {"_read_policy": draft_policy})
    combined = with_read_policy(initial_old, (*draft_policy, *bootstrap_policy))
    for path in (old_path, old_path.parent, draft_path, new_root, new_path):
        state.guard_excluded_paths(path, read_policy(combined), pinned=bool(read_policy(combined)))
    policies = read_policy(combined)
    lock_roots = set()
    for path in (old_path.parent, new_root):
        guarded_root = state.guard_excluded_paths(path, policies, pinned=bool(policies))
        lock_roots.add(guarded_root if policies else guarded_root.resolve())
    for item in initial_draft.get("budget_continuity", []):
        references = (item.get("recovery_sources") if "recovery_sources" in item
                      else [{"contract_path": item.get("source_contract_path")}])
        require(isinstance(references, list), "Invalid predecessor sources")
        for reference in references:
            source = absolute_file(reference.get("contract_path"), combined)
            guarded_root = state.guard_excluded_paths(source.parent, read_policy(combined), pinned=bool(read_policy(combined)))
            lock_roots.add(guarded_root if policies else guarded_root.resolve())
    preliminary = with_read_policy(initial_draft, read_policy(combined))
    for repo in preliminary.get("repos", []):
        lineage = GateFixLineage(preliminary, repo)
        budget_continuity(preliminary, repo, new_path, gate_lineage=lineage)
        for (source, _, inherited_policy), (_, _, _, cached_source) in lineage.cache.items():
            combined_policy = (*inherited_policy, *read_policy(cached_source))
            require({(root, path) for root, paths in combined_policy for path in paths} <= supplied,
                    "Bootstrap read policy omits a predecessor exclusion")
            guarded_root = state.guard_excluded_paths(source.parent, read_policy(combined), pinned=bool(policies))
            lock_roots.add(guarded_root if policies else guarded_root.resolve())
        for item in preliminary.get("budget_continuity", []):
            if item.get("repo") != repo or item.get("slice_id") is None:
                continue
            budget_continuity(preliminary, repo, new_path, item["slice_id"])
            current = item
            inherited = read_policy(preliminary)
            while current:
                source = absolute_file(current["source_contract_path"], {"_read_policy": inherited})
                source_value = with_read_policy(guarded_load(source, {"_read_policy": inherited}), inherited)
                inherited = read_policy(source_value)
                require({(root, path) for root, paths in inherited for path in paths} <= supplied,
                        "Bootstrap read policy omits a predecessor exclusion")
                guarded_root = state.guard_excluded_paths(source.parent, inherited, pinned=bool(inherited))
                lock_roots.add(guarded_root if inherited else guarded_root.resolve())
                source_slice = current["source_slice_id"]
                current = next((entry for entry in source_value.get("budget_continuity", [])
                                if entry.get("repo") == repo and entry.get("slice_id") == source_slice), None)
    with ExitStack() as stack:
        for root in sorted(lock_roots, key=str):
            guarded_root = state.guard_excluded_paths(root, read_policy(combined), pinned=bool(read_policy(combined)))
            fd = os.open(guarded_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, fd)
            fcntl.flock(fd, fcntl.LOCK_EX)
            stack.callback(fcntl.flock, fd, fcntl.LOCK_UN)
        draft = guarded_load(draft_path, combined)
        require(isinstance(draft, dict) and "setup_replacement" not in draft, "Invalid replacement draft")
        old = guarded_load(old_path, combined)
        require(isinstance(old, dict), "Invalid replaced contract")
        require(draft == initial_draft and old == initial_old, "Replacement inputs changed while acquiring locks")
        require(old_path.parent.parent.name == f".{old.get('workflow')}-runs"
                and new_root.parent.name == f".{draft.get('workflow')}-runs",
                "Replacement requires standard workflow run directories")
        old = with_read_policy(old, (*declared_read_policy(draft), *bootstrap_policy))
        for path in (old_path, draft_path, new_path, new_root / "run.json"):
            guard_path(path, old)
        new_meta = guarded_load(new_root / "run.json", old)
        require(new_meta.get("schema_version") == VERSION and new_meta.get("run_id") == new_root.name
                and new_meta.get("workflow") == draft.get("workflow") and new_meta.get("repos") == draft.get("repos"),
                "Replacement run metadata mismatch")
        if draft.get("workflow") == "ship":
            require(new_meta.get("plan_path") == draft.get("plan_path"), "Replacement run plan mismatch")
        setup_history(new_path, with_read_policy(draft, read_policy(old)), require_telemetry=False)
        closure_path = old_path.parent / "setup-replacement.json"
        journal_path = guard_path(new_root / "inputs/setup-replacement-transaction.json", old)
        journal = guarded_load(journal_path, old) if guarded_exists(journal_path, old) else None
        temporary = None
        if journal is not None:
            require(isinstance(journal, dict) and journal.get("old_contract_path") == str(old_path)
                    and journal.get("new_contract_path") == str(new_path) and journal.get("reason") == reason,
                    "Conflicting replacement transaction")
            temporary = Path(journal.get("temporary_path", ""))
            require(temporary.parent == old_path.parent
                    and re.fullmatch(r"\.setup-replacement-[0-9a-f]{32}", temporary.name),
                    "Invalid replacement transaction temporary path")
            guard_path(temporary, old)
        files = setup_history(old_path, old, closed=guarded_exists(closure_path, old), transaction_temp=temporary)
        draft["setup_replacement"] = {"contract_path": str(old_path), "contract_sha256": guarded_sha(old_path, old),
                                      "reason": reason, "files": files}
        payload = json_bytes(draft)
        successor_hash = hashlib.sha256(payload).hexdigest()
        closure = {"schema_version": VERSION, "status": "replaced", "run_id": old["run_id"],
                   "workflow": old["workflow"], "replacement": draft["setup_replacement"],
                   "work_items": old["budget_work_items"], "repo_paths": old["repo_paths"],
                   "successor_run_id": draft["run_id"], "successor_contract_path": str(guarded_resolve(new_path, old)),
                   "successor_contract_sha256": successor_hash}
        closure_payload = json_bytes(closure)
        transaction = {"old_contract_path": str(old_path), "new_contract_path": str(new_path),
                       "reason": reason, "files": files, "closure_sha256": hashlib.sha256(closure_payload).hexdigest(),
                       "successor_sha256": successor_hash,
                       "temporary_path": str(temporary or old_path.parent / (".setup-replacement-" + secrets.token_hex(16)))}
        if journal is not None:
            require(journal == transaction, "Conflicting replacement transaction")
        else:
            temporary = Path(transaction["temporary_path"])
            guard_path(temporary, old)
            require(not guarded_exists(temporary, old), "Replacement temporary path already exists")
        pending = {"old": old_path, "closure": closure, "successor_sha256": successor_hash,
                   "closure_sha256": hashlib.sha256(closure_payload).hexdigest(), "temporary_path": temporary}
        checked = contract(new_path, draft=draft, pending_replacement=pending, inherited_policy=read_policy(old))
        validate_store_roots(checked)
        for repo, declaration in checked.get("slice_schedules", {}).items():
            checked_slice_schedule(checked, repo, declaration.get("path"))
        reservations = reserve_predecessors(draft, new_path, successor_hash, old)
        for target, _, predecessor in reservations:
            guarded_root = state.guard_excluded_paths(target.parent.parent, read_policy(predecessor),
                                                       pinned=bool(read_policy(predecessor)))
            require((guarded_root if read_policy(predecessor) else guarded_root.resolve()) in lock_roots,
                    "Predecessor lineage changed while acquiring locks")
        if journal is None:
            publish_once(journal_path, json_bytes(transaction), old)
        for target, reservation_payload, predecessor in reservations:
            publish_once(target, reservation_payload, predecessor)
        if guarded_exists(temporary, old):
            parent = guarded_resolve(temporary.parent, old)
            with state.open_directory(parent) as fd:
                info = os.stat(temporary.name, dir_fd=fd, follow_symlinks=False)
                require(stat.S_ISREG(info.st_mode), "Invalid replacement transaction temporary file")
                os.unlink(temporary.name, dir_fd=fd)
        for path, expected, writer in ((closure_path, closure_payload, old), (new_path, payload, checked)):
            if guarded_exists(path, writer):
                require(guarded_bytes(path, writer) == expected, f"Conflicting replacement artifact: {path}")
            else:
                publish_once(path, expected, writer, temporary_path=temporary if path == closure_path else None)
        return {"status": "replaced", "closure_path": str(closure_path), "contract_path": str(new_path)}


def absolute_file(path, value=None):
    require(isinstance(path, (str, Path)) and Path(path).is_absolute(), f"Missing absolute file: {path}")
    path = guard_path(path, value)
    if value and value.get("external_stores"):
        require_local_mode(path, value)
    if value is not None and read_policy(value):
        guarded = state.guard_excluded_paths(path, read_policy(value), pinned=True)
        try:
            with state.capture_open(guarded):
                pass
        except OSError as error:
            raise ValueError(f"Missing absolute file: {path}") from error
    else:
        require(path.is_file(), f"Missing absolute file: {path}")
    return path


def require_local_mode(path, value):
    declarations = value.get("external_stores", {})
    if not declarations:
        return
    resolved = Path(path).absolute().resolve()
    for declaration in declarations.values():
        root = Path(declaration["path"])
        require(len(resolved.parts) < len(root.parts) or not state.same_path_prefix(resolved, root, pinned=True),
                "STORE files must use an explicit external binding")


def excluded_guard(value):
    names = state.policy_tables(read_policy(value), pinned=True)
    return lambda path: state.check_path_exclusions(path, names)


def store_file(value, path, name):
    require(isinstance(name, str) and name in value.get("external_stores", {}), "Unknown external STORE")
    declaration = value["external_stores"][name]
    source = store.absolute_path(path)
    root = store.absolute_path(declaration["path"])
    require(root in source.parents, "File is outside its declared STORE")
    excluded_guard(value)(source)
    return declaration


def validate_store_declarations(value, run):
    declarations = value.get("external_stores", {})
    require(isinstance(declarations, dict), "Invalid external_stores")
    if not declarations:
        return
    protected = [run.resolve(), *(Path(path).resolve() for path in value["repo_paths"].values())]
    for name, declaration in declarations.items():
        require(re.fullmatch(r"[A-Za-z0-9_-]+", name), "Invalid STORE name")
        require(isinstance(declaration, dict) and set(declaration) == {"path", "device", "inode"}
                and type(declaration["device"]) is int and declaration["device"] >= 0
                and type(declaration["inode"]) is int and declaration["inode"] > 0, "Invalid STORE declaration")
        root = store.absolute_path(declaration["path"])
        require(not any(state.same_path_prefix(root, other, pinned=True) for other in protected),
                "STORE roots must not overlap each other, a repository, or the run")
        excluded_guard(value)(root)
        protected.append(root)


def validate_store_roots(value):
    if value.get("external_stores"):
        guard = excluded_guard(value)
        for declaration in value["external_stores"].values():
            store.inspect_root(declaration, guard)


def delivery_scope(entry):
    return entry.get("required_at")


def staged_deliverables(value, repo):
    return any("required_at" in entry for entry in value.get("deliverables", {}).get(repo, []))


def scope_input(path, value, *, optional=False):
    require(isinstance(path, (str, Path)) and Path(path).is_absolute(), f"Missing absolute file: {path}")
    policies = read_policy(value)
    source = state.guard_excluded_paths(Path(path), policies, pinned=bool(policies))
    try:
        return state.scope_paths(source, pinned=bool(policies))
    except FileNotFoundError as error:
        if optional:
            return []
        raise ValueError(f"Missing absolute file: {path}") from error


def repo_read_excludes(value, repo):
    policies = read_policy(value)
    if not policies:
        return ()
    root = state.guard_excluded_paths(value["repo_paths"][repo], policies)
    exclusions = set()
    for other, paths in policies:
        source_root = state.guard_excluded_paths(other, policies)
        for path in paths:
            protected = source_root / path
            if len(protected.parts) > len(root.parts) and state.same_path_prefix(protected, root):
                exclusions.add("/".join(protected.parts[len(root.parts):]))
    return tuple(sorted(exclusions))


def scope_amendment(contract_path, value, repo, path, events, initial):
    """Validate a bounded, run-local addition without replacing the sealed schedule."""
    path = absolute_file(path, value)
    run = Path(contract_path).parent.resolve()
    require(path.resolve().is_relative_to(run), "Scope amendment must be run-local")
    amendment = state.load(path)
    bound(value, repo, amendment)
    require(amendment.get("approved_by") == "user", "Scope amendment requires user approval")
    state.text(amendment.get("user_authorization"), "scope amendment authorization")
    state.text(amendment.get("reason"), "scope amendment reason")
    require(amendment.get("contract_sha256") == sha(contract_path), "Scope amendment contract changed")
    declaration = value.get("slice_schedules", {}).get(repo, {})
    require(amendment.get("schedule_sha256") == declaration.get("sha256"), "Scope amendment schedule changed")
    schedule, _ = checked_slice_schedule(value, repo, declaration.get("path"))
    slices = schedule["slices"]
    anchor_path = absolute_file(amendment.get("previous_state_path"), value)
    require(anchor_path.resolve().is_relative_to(run), "Scope amendment state must be run-local")
    require(amendment.get("previous_state_sha256") == sha(anchor_path), "Scope amendment state changed")
    matches = [(i, event) for i, (_, event_path, event) in enumerate(events) if event_path == anchor_path]
    require(len(matches) == 1, "Scope amendment must reference repo history")
    index, anchor = matches[0]
    require(anchor.get("status") == "reviewed", "Scope amendment needs reviewed source")
    require(amendment.get("author_agent_id") == anchor.get("current_author_agent_id"), "Scope amendment author differs")
    require(timestamp(anchor["at"]) <= timestamp(amendment.get("approved_at")) <= datetime.now(timezone.utc),
            "Invalid scope amendment time")
    require(all(event.get("scope_amendment_path") == str(path) for _, _, event in events[index + 1:]),
            "Scope amendment is not linked through subsequent history")
    before_path = absolute_file(amendment.get("before_manifest"), value)
    require(str(before_path) == anchor.get("source_manifest") and
            amendment.get("before_manifest_sha256") == sha(before_path), "Scope amendment source changed")
    before = manifest(value, repo, before_path)
    require(before["fingerprint"] == anchor["source_fingerprint"] and before["head"] == initial["head"],
            "Scope amendment source is not the reviewed source")
    original_gate = state.load(absolute_file(anchor.get("slice_gate_path"), value))
    bound(value, repo, original_gate)
    check_seals(original_gate["artifact_hashes"], value)
    require(amendment.get("slice_states") == original_gate.get("slices"),
            "Scope amendment must bind all accepted slices")
    for identifier, expected_hash in amendment["slice_states"].items():
        state.relative_path(identifier)
        slice_events = history(run / repo / "slices" / identifier, value)
        require(slice_events and slice_events[-1][2].get("status") == "accepted" and
                sha(slice_events[-1][1]) == expected_hash, "Scope amendment accepted slices changed")
    additions = amendment.get("paths")
    require(isinstance(additions, list) and additions and len(additions) == len(set(additions)),
            "Scope amendment needs unique paths")
    root = Path(value["repo_paths"][repo])
    names = source_names(value, repo)
    baseline_guard = lambda p: guard_path(p, value)
    for name in anchor.get("scope", []) + anchor.get("source_scope", []):
        guard_path(root / state.relative_path(name), value, scope=True)
    for name in additions:
        state.relative_path(name)
        require(not any(char in name for char in "*?[]"), "Scope amendment paths cannot be patterns")
        guard_path(root / name, value, scope=True)
    policies = read_policy(value)
    tracked = state.scoped_tracked_paths(
        root, additions, names.read_excludes,
        guard=(lambda path: state.guard_excluded_paths(path, policies, pinned=True)) if policies else None)
    require(len(names.keys(additions)) == len(additions), "Scope amendment needs unique source entries")
    before_files, initial_files = names.files(before["files"]), names.files(initial["files"])
    already_scoped = names.keys(anchor.get("scope", []) + anchor.get("source_scope", []))
    baseline_dir = guard_path(anchor["baseline_dir"], value)
    for name in additions:
        require(name in tracked, "Scope amendment path must be tracked")
        key = names.key(name)
        require(not any(in_scope(spelling, item["write_scope"], names)
                        for item in slices for spelling in {name, key}),
                "Scope amendment path belongs to an approved slice")
        require(key not in already_scoped,
                "Scope amendment path is already scoped")
        require(before_files.get(key, state.MISSING) == initial_files.get(key, state.MISSING),
                "Scope amendment cannot adopt pre-existing source drift")
        try:
            record = state.baseline_record(root, baseline_dir, name, guard=baseline_guard)
        except ValueError as error:
            raise ValueError("Scope amendment needs a matching first-touch baseline") from error
        require(record is not None and record.get("state") == initial_files.get(key) and
                state.file_state(state.safe_path(baseline_dir, record["path"], guard=baseline_guard)) == record["state"],
                "Scope amendment needs a matching first-touch baseline")
    return additions, [str(path), str(anchor_path), str(before_path), anchor["slice_gate_path"]]


def timestamp(value):
    state.text(value, "timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, "Timestamp needs timezone")
    return parsed


def requirement_amendment(value, contract_path, path, previous_path):
    """Validate one explicit, run-local user amendment against its pre-cutover state."""
    root = Path(contract_path).parent.resolve()
    path = absolute_file(path, value)
    require(path.resolve().is_relative_to(root), "Amendment must be run-local")
    a = state.load(path)
    require(isinstance(a, dict) and a.get("schema_version") == VERSION
            and a.get("run_id") == value["run_id"] and a.get("workflow") == "ship",
            "Amendment belongs to a different run")
    require(value["workflow"] == "ship" and value["repos"] == [a.get("repo")],
            "Amendment supports exactly one included ship repo")
    require(a.get("contract_sha256") == sha(contract_path)
            and a.get("approved_snapshot_sha256") == value["plan_sha256"],
            "Amendment contract or snapshot mismatch")
    require(a.get("previous_state_path") == str(previous_path)
            and a.get("previous_state_sha256") == sha(previous_path), "Amendment state binding mismatch")
    require(a.get("approved_by") == "user", "Amendment needs user approval")
    for key in ("user_question", "user_answer", "reason"):
        state.text(a.get(key), "amendment " + key)
    require(timestamp(state.load(previous_path)["at"]) <= timestamp(a.get("approved_at"))
            <= datetime.now(timezone.utc), "Invalid amendment time")
    changes = a.get("changes")
    require(isinstance(changes, list) and len(changes) == 1, "Amendment must name exactly one criterion")
    change = changes[0]
    base_fields = {"criterion_id", "old_requirement", "old_verification", "new_requirement", "new_verification"}
    environment_fields = {"old_environment", "new_environment"}
    scope_fields = {"old_data_scope", "new_data_scope"}
    allowed_fields = {frozenset(base_fields), frozenset(base_fields | environment_fields),
                      frozenset(base_fields | scope_fields), frozenset(base_fields | environment_fields | scope_fields)}
    require(isinstance(change, dict) and frozenset(change) in allowed_fields, "Invalid amendment change")
    items = [c for c in value["criteria"] if c["id"] == change["criterion_id"] and a["repo"] in c["repos"]]
    require(len(items) == 1 and items[0]["requirement"] == change["old_requirement"]
            and items[0]["verification"] == change["old_verification"], "Amendment original criterion mismatch")
    state.text(change["new_requirement"], "amended requirement")
    state.text(change["new_verification"], "amended verification")
    if environment_fields <= set(change):
        require(items[0]["environment"] == change["old_environment"], "Amendment original environment mismatch")
        state.text(change["new_environment"], "amended environment")
    if scope_fields <= set(change):
        require(items[0]["data_scope"] == change["old_data_scope"], "Amendment original data_scope mismatch")
        state.text(change["new_data_scope"], "amended data_scope")
    require((change["new_requirement"], change["new_verification"]) !=
            (change["old_requirement"], change["old_verification"]), "Empty amendment")
    return a


def requirement_amendment_suffix(value, criterion_id):
    """Amendment hashes at least as new as the last amendment that touched the criterion."""
    amendments = value.get("_amendments") or []
    touches = [i for i, applied in enumerate(amendments) if applied["criterion_id"] == criterion_id]
    return {amendments[touches[-1]]["sha256"]} if touches else set()


def continuity_declarations(value, *, historical=False):
    repos = value["repos"]
    continuity = value.get("budget_continuity", [])
    require(isinstance(continuity, list), "Invalid budget continuity")
    work_items = value.get("budget_work_items", {})
    require(isinstance(work_items, dict) and set(work_items) <= set(repos), "Invalid budget work items")
    for work_item in work_items.values():
        state.text(work_item, "budget work item")
    continuity_keys = []
    continuity_sources = []
    for item in continuity:
        require(isinstance(item, dict) and item.get("repo") in repos, "Invalid continuity repo")
        if item.get("slice_id") is not None:
            state.relative_path(item["slice_id"])
        continuity_keys.append((item["repo"], item.get("slice_id")))
        source = item.get("source_contract_path")
        if isinstance(source, str) and Path(source).is_absolute():
            source = guard_path(source, value).resolve()
        continuity_sources.append((source, item["repo"], item.get("source_slice_id")))
    require(len(continuity_keys) == len(set(continuity_keys)), "Duplicate budget continuity scope")
    require(len(continuity_sources) == len(set(continuity_sources)), "Duplicate budget continuity source")
    successors = value.get("successor_scopes", [])
    require(isinstance(successors, list) and all(isinstance(item, dict) for item in successors),
            "Invalid successor scopes")
    successor_keys = [(item.get("repo"), item.get("slice_id"), item.get("work_item")) for item in successors]
    require(len(successor_keys) == len(set(successor_keys)), "Duplicate successor scope")
    continuity_work = {(item["repo"], item.get("slice_id"), item.get("work_item")) for item in continuity}
    if not historical or "successor_scopes" in value:
        require(set(successor_keys) == continuity_work, "Successor scope requires matching budget continuity")
    for item in continuity:
        if item.get("slice_id") is None and item["repo"] in work_items:
            require(item.get("work_item") == work_items[item["repo"]], "Budget work item and continuity differ")
    return continuity, work_items


def contract(path, *, draft=None, pending_replacement=None, inherited_policy=()):
    path = absolute_file(path) if draft is None else Path(path).absolute()
    value = state.load(path) if draft is None else copy.deepcopy(draft)
    require(isinstance(value, dict), "Contract must be an object")
    require(not {"_amendment", "_contract_path", "_read_policy", "_setup_replacements", "_pending_replacement"} & value.keys(),
            "Contract cannot define internal state")
    if inherited_policy:
        value = with_read_policy(value, inherited_policy)
    require(value.get("schema_version") == VERSION, "Expected workflow contract v2")
    require(value.get("workflow") in {"ship", "fix"}, "Invalid workflow")
    state.relative_path(value.get("run_id"))
    require(path.parent.name == value["run_id"], "Contract/run directory mismatch")
    if "telemetry_policy" in value:
        policy = value["telemetry_policy"]
        require(isinstance(policy, dict), "telemetry_policy must be an object")
        require(type(policy.get("repair_phase_required")) is bool,
                "telemetry_policy.repair_phase_required must be boolean")
        for flag in ("end_reason_required", "run_local_seals"):
            require(type(policy.get(flag, False)) is bool, f"telemetry_policy.{flag} must be boolean")
    # Absent means resume (legacy behaviour): repairs go to the same author session.
    require(value.get("author_rotation", "resume") in {"capsule", "resume"}, "author_rotation must be capsule or resume")
    repos = value.get("repos")
    require(isinstance(repos, list) and repos and len(set(repos)) == len(repos), "Invalid repos")
    for repo in repos:
        state.relative_path(repo)
        root = value.get("repo_paths", {}).get(repo)
        require(isinstance(root, str) and Path(root).is_absolute(),
                f"Missing absolute repo path: {repo}")
    read_excludes = value.get("read_excludes", {})
    require(isinstance(read_excludes, dict) and set(read_excludes) <= set(repos), "Invalid read_excludes")
    for paths in read_excludes.values():
        require(isinstance(paths, list) and len(paths) == len(set(paths)), "Invalid read_excludes paths")
        for excluded_path in paths:
            state.relative_path(excluded_path)
    guard_path(path, value)
    closure_path = guard_path(path.parent / "setup-replacement.json", value)
    require(not guarded_exists(closure_path, value),
            "This setup run has been replaced")
    for repo in repos:
        require(guard_path(value["repo_paths"][repo], value).is_dir(), f"Missing absolute repo path: {repo}")
    validate_store_declarations(value, path.parent)
    continuity, work_items = continuity_declarations(value)
    deliverables = value.get("deliverables", {})
    require(isinstance(deliverables, dict) and set(deliverables) <= set(repos), "Invalid deliverables")
    for repo, entries in deliverables.items():
        unique_ids(entries, "deliverables")
        paths = []
        for entry in entries:
            deliverable_path = entry.get("path")
            require(isinstance(deliverable_path, str) and Path(deliverable_path).is_absolute(), "Deliverable needs an absolute path")
            paths.append(deliverable_path)
            if "required_at" in entry:
                required_at = entry["required_at"]
                require(isinstance(required_at, dict) and (required_at == {"scope": "repo"}
                        or (set(required_at) == {"scope", "slice_id"} and required_at["scope"] == "slice")),
                        "Invalid deliverable required_at")
                if required_at["scope"] == "slice":
                    state.relative_path(required_at["slice_id"])
                store.absolute_path(deliverable_path)
            if "store" in entry:
                require("required_at" in entry, "STORE deliverable needs explicit required_at")
                store_file(value, deliverable_path, entry["store"])
            elif value.get("external_stores") or "required_at" in entry:
                guard_path(deliverable_path, value)
                require_local_mode(deliverable_path, value)
            criteria = entry.get("criterion_ids")
            require(isinstance(criteria, list) and criteria and len(criteria) == len(set(criteria)),
                    "Deliverable needs unique criterion IDs")
            require(all(any(c.get("id") == item and repo in c.get("repos", []) for c in value.get("criteria", []))
                        for item in criteria), "Unknown deliverable criterion")
        require(len(paths) == len(set(paths)), "Duplicate deliverable path")
    ids = unique_ids(value.get("criteria"), "criteria")
    require(ids, "Acceptance criteria cannot be empty")
    for criterion in value["criteria"]:
        require(criterion.get("kind") in {"static", "empirical"}, "Invalid criterion kind")
        require(type(criterion.get("mandatory")) is bool, "Criterion needs mandatory flag")
        require(isinstance(criterion.get("repos"), list) and criterion["repos"]
                and set(criterion["repos"]) <= set(repos), "Invalid criterion repos")
        if "assessment_repo" in criterion:
            owner = criterion["assessment_repo"]
            require(criterion["mandatory"] and len(set(criterion["repos"])) > 1,
                    "Deferred assessment requires a mandatory cross-repo criterion")
            require(owner in criterion["repos"] and owner == max(criterion["repos"], key=repos.index),
                    "Deferred assessment must belong to the last participating repo")
        for field in ("requirement", "source", "verification", "environment", "data_scope"):
            state.text(criterion.get(field), field)
    for criterion in value["criteria"]:
        if "assessment_repo" in criterion:
            for participant in criterion["repos"]:
                require(any(c.get("mandatory") and c.get("repos") == [participant]
                            and "assessment_repo" not in c for c in value["criteria"]),
                        f"Deferred cross-repo criterion needs a local criterion for {participant}")
    for group in ("required_gates", "pre_review_checks", "preflight_checks"):
        require(set(value.get(group, {})) == set(repos), f"Missing repo in {group}")
        for repo in repos:
            entries = value[group][repo]
            unique_ids(entries, group)
            require(entries, f"Empty {group} for {repo}; declare justified inspection when no runner exists")
            for entry in entries:
                require(entry.get("kind") in {"static", "empirical"}, "Invalid check kind")
                state.text(entry.get("verification"), "check verification")
                inputs = entry.get("input_paths", [])
                require(isinstance(inputs, list), "Invalid check input_paths")
                for input_path in inputs:
                    require(isinstance(input_path, str) and Path(input_path).is_absolute(),
                            "Check input path must be absolute")
                require(len(inputs) == len(set(inputs)), "Duplicate check input_paths")
                external = entry.get("external_input_stores", {})
                require(isinstance(external, dict) and set(external) <= set(inputs), "Invalid external_input_stores")
                if external:
                    require(entry["kind"] == "empirical" and "argv" in entry,
                            "STORE check inputs require an empirical argv check")
                for input_path, name in external.items():
                    store_file(value, input_path, name)
                if value.get("external_stores"):
                    for input_path in set(inputs) - external.keys():
                        guard_path(input_path, value)
                        require_local_mode(input_path, value)
                if entry["kind"] == "empirical":
                    require(("command" in entry) != ("argv" in entry), "Check needs exactly one of command or argv")
                    if "argv" in entry:
                        argv = entry["argv"]
                        require(isinstance(argv, list) and argv and all(isinstance(a, str) and a for a in argv),
                                "Check argv must be a non-empty list of non-empty strings")
                    require(not re.search(r"<[^>]+>", check_command(entry)), "Unresolved command placeholder")
    for repo in repos:
        all_checks = [x for g in ("required_gates", "pre_review_checks", "preflight_checks")
                      for x in value[g][repo]]
        require(len({x["id"] for x in all_checks}) == len(all_checks), "Check IDs overlap")
        require(not ids & {x["id"] for x in all_checks}, "Check/criterion IDs overlap")
    unique_ids(value.get("prerequisites", []), "prerequisites")
    for entry in value.get("prerequisites", []):
        require(entry.get("kind") in {"static", "empirical"}, "Invalid prerequisite kind")
        require(entry.get("repo") in repos, "Prerequisite needs an included repo")
        state.text(entry.get("verification"), "prerequisite verification")
        require(entry.get("evidence_paths"), "Prerequisite requires proof")
    require(isinstance(value.get("cross_repo_contracts", []), list), "Invalid cross-repo contracts")
    for item in value.get("cross_repo_contracts", []):
        require(item.get("criterion_id") in ids, "Cross-repo contract must reference a criterion")
    for repo in repos:
        passes = value.get("required_passes", {}).get(repo)
        require(isinstance(passes, list) and len(set(passes)) == len(passes), "Missing required passes")
        for item in passes:
            state.text(item, "rubric pass")
    if value["workflow"] == "ship":
        snapshot = absolute_file(value.get("approved_snapshot"), value)
        require(snapshot.resolve().is_relative_to(path.parent.resolve()), "Approved snapshot must be frozen inside run directory")
        require(sha(snapshot) == value.get("plan_sha256"),
                "Approved snapshot hash mismatch")
        require(sha(absolute_file(value.get("plan_path"), value)) == value["plan_sha256"],
                "Approved plan changed before completion")
    else:
        brief = value.get("acceptance", {})
        for field in ("original_scenario", "expected_behavior", "non_goals"):
            state.text(brief.get(field), field)
        require(brief.get("mode") in {"reproduced-bug", "improvement", "unavailable-baseline"},
                "Invalid fix acceptance mode")
        scenario_ids = set(brief.get("criterion_ids", []))
        mandatory_ids = {c["id"] for c in value["criteria"] if c["mandatory"]}
        require(scenario_ids and scenario_ids <= mandatory_ids, "Fix scenario needs mandatory acceptance criteria")
        if brief["mode"] == "unavailable-baseline":
            state.text(brief.get("reason"), "unavailable baseline reason")
    if value["workflow"] == "ship" and len(repos) == 1:
        events = history(path.parent / repos[0], value)
        registrations = [(i, event) for i, (_, _, event) in enumerate(events) if event.get("amendment_applied")]
        value["_amendments"] = []
        for position, (i, registration) in enumerate(registrations):
            require(i > 0, "Amendment registration must follow initialization")
            end = registrations[position + 1][0] if position + 1 < len(registrations) else len(events)
            require(all(e.get("amendment_path") == registration.get("amendment_path")
                        and e.get("amendment_sha256") == registration.get("amendment_sha256")
                        for _, _, e in events[i:end]), "Amendment history changed")
            if position == 0:
                require(all(not e.get("amendment_path") and not e.get("amendment_sha256")
                            for _, _, e in events[:i]), "Amendment history changed")
            a_path = registration["amendment_path"]
            require(sha(absolute_file(a_path, value)) == registration.get("amendment_sha256")
                    and registration.get("artifact_hashes", {}).get(a_path) == registration["amendment_sha256"],
                    "Amendment seal mismatch")
            a = requirement_amendment(value, path, a_path, events[i - 1][1])
            change = a["changes"][0]
            item = next(c for c in value["criteria"] if c["id"] == change["criterion_id"])
            item["requirement"] = change["new_requirement"]
            item["verification"] = change["new_verification"]
            if "new_environment" in change:
                item["environment"] = change["new_environment"]
            if "new_data_scope" in change:
                item["data_scope"] = change["new_data_scope"]
            value["_amendments"].append({"path": a_path, "sha256": registration["amendment_sha256"],
                                         "criterion_id": change["criterion_id"]})
        if value["_amendments"]:
            value["_amendment"] = value["_amendments"][-1]
    value["_contract_path"] = str(path)
    value["_setup_replacements"] = replacement_chain(value, path, pending=pending_replacement)
    if pending_replacement is not None:
        value["_pending_replacement"] = True
    for repo in repos:
        entries = value.get("deliverables", {}).get(repo, [])
        if any(delivery_scope(entry) and delivery_scope(entry)["scope"] == "slice" for entry in entries):
            declaration = value.get("slice_schedules", {}).get(repo)
            require(isinstance(declaration, dict), "Slice deliverable needs an approved slice schedule")
            checked_slice_schedule(value, repo, declaration.get("path"))
    for item in continuity:
        repo, slice_id = item["repo"], item.get("slice_id")
        if slice_id is None and not history(path.parent / repo, value):
            require(all(continuity_history_seals(item)), "New successor requires source history SHA-256")
        budget_continuity(value, repo, path, slice_id)
    related_repos = set(work_items) | {item["repo"] for item in continuity if item.get("slice_id") is None}
    for repo in sorted(related_repos):
        check_related_budgets(value, repo, path)
    return value


def check_command(entry):
    """Canonical command identity: argv is quoted exactly once, so no outer shell can re-expand it."""
    if "argv" in entry:
        return shlex.join(entry["argv"])
    state.text(entry.get("command"), "check command")
    return entry["command"]


def spec(value, repo, identifier):
    for item in value["criteria"]:
        if item["id"] == identifier and repo in item["repos"]:
            return item
    for group in ("required_gates", "pre_review_checks", "preflight_checks"):
        for item in value[group][repo]:
            if item["id"] == identifier:
                return item
    for item in value.get("prerequisites", []):
        if item["id"] == identifier and item["repo"] == repo:
            return item
    raise ValueError(f"Unknown criterion/check ID: {repo}/{identifier}")


def manifest(value, repo, path):
    result = state.checked_manifest(absolute_file(path, value), loader=lambda item: guarded_load(item, value))
    require(result["repo_path"] == str(guard_path(value["repo_paths"][repo], value).resolve()), "Wrong manifest repo")
    return result


def fresh(value, repo, path, scope, excludes=()):
    recorded = manifest(value, repo, path)
    read_excludes = repo_read_excludes(value, repo)
    state.match_source(recorded, state.capture(value["repo_paths"][repo], scope, excludes, read_excludes,
                                             policies=read_policy(value)), read_excludes)
    return recorded


def source_names(value, repo):
    return state.PathNames(guard_path(value["repo_paths"][repo], value).resolve(), repo_read_excludes(value, repo))


def event_manifest(value, repo, event):
    source = manifest(value, repo, event["source_manifest"])
    require(source["fingerprint"] == event["source_fingerprint"], "Source manifest does not match state fingerprint")
    return source


def review_source_matches(value, repo, previous, events, final):
    fingerprint = previous.get("review_fingerprint")
    if fingerprint == final["fingerprint"]:
        return True
    for _, _, event in reversed(events):
        if (event["status"] in {"reviewed", "reviewed-with-findings"}
                and event.get("review_path") == previous.get("review_path")
                and event.get("source_fingerprint") == fingerprint):
            return state.same_source(event_manifest(value, repo, event), final,
                                     repo_read_excludes(value, repo))
    return False


def deliverable_selection(value, repo, slice_item=None, criterion_ids=None):
    require(repo in value["repos"], "Unknown deliverable repo")
    entries = value.get("deliverables", {}).get(repo, [])
    if slice_item is None and criterion_ids is None:
        return entries, []
    require(slice_item is not None or not staged_deliverables(value, repo),
            "Staged deliverables need an approved slice")
    criteria = ({entry["criterion_id"] for entry in slice_item["contributions"]}
                if slice_item is not None else set(criterion_ids))
    selected, pending = [], []
    for entry in entries:
        scope = delivery_scope(entry)
        due = (scope == {"scope": "slice", "slice_id": slice_item["id"]} if scope
               else bool(criteria.intersection(entry["criterion_ids"])))
        if due:
            selected.append(entry)
        else:
            pending.append(entry["id"])
    return selected, pending


def deliverable_scope_record(slice_item):
    return ({"scope": "repo"} if slice_item is None
            else {"scope": "slice", "slice_id": slice_item["id"]})


def run_local_path(value, path, *, file=False):
    require(isinstance(path, (str, Path)) and Path(path).is_absolute(), "Expected absolute run-local path")
    path = Path(path)
    guarded = state.guard_excluded_paths(path, read_policy(value), pinned=bool(read_policy(value)))
    root = Path(value["_contract_path"]).parent.resolve()
    require(guarded.resolve().is_relative_to(root), "Artifact must be run-local")
    if file:
        require(path.is_file(), f"Missing run-local file: {path}")
    return path


def checked_local_artifact(value, raw):
    path = run_local_path(value, raw, file=True)
    canonical_run_artifact(value, path)
    return path


def canonical_run_artifact(value, path):
    declared = Path(value["_contract_path"]).parent
    root = declared.resolve()
    if path.is_relative_to(declared):
        candidate = root / path.relative_to(declared)
    else:
        candidate = path
    require(candidate.is_relative_to(root) and candidate.resolve() == candidate,
            "Bound artifact cannot be a symlink or non-canonical path")
    return candidate


def local_snapshot_source(raw, value):
    path = Path(raw)
    policies = read_policy(value)
    source = state.guard_excluded_paths(path, policies, pinned=bool(policies))
    if value.get("external_stores") and not policies:
        source = source.parent.resolve() / source.name
    require(not path.is_symlink(), f"Snapshot source cannot be a symlink: {path}")
    require_local_mode(source, value)
    require(source.is_file(), f"Missing absolute file: {path}")
    return source


def snapshot_reads_pinned(value):
    return bool(read_policy(value) or value.get("external_stores"))


def local_digest(raw, value):
    source = local_snapshot_source(raw, value)
    if snapshot_reads_pinned(value):
        with state.capture_open(source) as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    return sha(source)


def snapshot_deliverables(contract_path, repo, out_dir, criterion_ids=None, *, slice_item=None):
    value = contract(contract_path)
    require_unreserved(value, contract_path, repo)
    entries, pending = deliverable_selection(value, repo, slice_item, criterion_ids)
    staged = staged_deliverables(value, repo)
    require(entries or (staged and pending), "No deliverables declared")
    output = run_local_path(value, out_dir)
    if staged:
        output = canonical_run_artifact(value, output)
    require(not output.exists(), "Snapshot directory already exists")
    payload = []
    for entry in entries:
        row = {"id": entry["id"], "path": entry["path"]}
        if "store" in entry:
            name = entry["store"]
            row.update(store=name, **store.observe(entry["path"], store_file(value, entry["path"], name),
                                                   excluded_guard(value)))
            payload.append((row, None))
        else:
            source = local_snapshot_source(entry["path"], value)
            read = state.capture_bytes if snapshot_reads_pinned(value) else Path.read_bytes
            data = read(source)
            require(data == read(source), "Deliverable changed while snapshotting")
            row["sha256"] = hashlib.sha256(data).hexdigest()
            payload.append((row, data))
    if staged:
        with state.open_directory(output.parent, create=True) as parent:
            os.mkdir(output.name, dir_fd=parent)
    else:
        output.mkdir(parents=True)
    rows = []
    for index, (row, data) in enumerate(payload):
        if data is not None:
            snapshot = output / f"{index}.snapshot"
            if staged:
                with state.open_directory(output) as parent:
                    fd = os.open(snapshot.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o666, dir_fd=parent)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
            else:
                with snapshot.open("xb") as handle:
                    handle.write(data)
            row["snapshot"] = str(snapshot)
        rows.append(row)
    result = {"schema_version": VERSION, "run_id": value["run_id"], "repo": repo, "deliverables": rows}
    if staged:
        result.update(scope=deliverable_scope_record(slice_item), pending=pending)
    state.write_once(output / "manifest.json", result, pinned=staged)
    return str(output / "manifest.json")


def snapshot_inputs(paths, out_dir, repo_root, read_excludes, *, value=None, pinned_out_dir=None):
    if not paths:
        return []
    output = Path(out_dir)
    policy = value or {"_read_policy": ((repo_root, read_excludes),) if read_excludes else ()}
    physical_output = Path(pinned_out_dir) if pinned_out_dir is not None else guarded_resolve(output.absolute(), policy)
    try:
        with state.open_directory(physical_output):
            raise ValueError("Input snapshot directory already exists")
    except FileNotFoundError:
        pass
    payload = []
    for item in paths:
        source = local_snapshot_source(item, policy)
        read = state.capture_bytes if snapshot_reads_pinned(policy) else Path.read_bytes
        data = read(source)
        require(data == read(source), "Check input changed while snapshotting")
        payload.append((str(Path(item)), data))
    rows = []
    with state.open_directory(physical_output, create=True) as directory:
        for index, (source, data) in enumerate(payload):
            target = output / f"{index}.input"
            fd = os.open(target.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666, dir_fd=directory)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            rows.append({"path": source, "snapshot": str(target), "sha256": hashlib.sha256(data).hexdigest()})
    registry = output / "manifest.json"
    state.write_once(physical_output / "manifest.json", rows, pinned=True)
    return [str(registry), *[row["snapshot"] for row in rows]]


def check_inputs_unchanged(paths, value=None):
    if not paths:
        return
    for row in (guarded_load(paths[0], value) if value is not None else state.load(paths[0])):
        require(local_digest(row["path"], value or {}) == row["sha256"],
                "Check input changed during execution")


def external_input_rows(value, check, *, command_input):
    guard = excluded_guard(value)
    return [{"path": path, "store": name, **store.observe(path, store_file(value, path, name), guard,
                                                         command_input=command_input)}
            for path, name in sorted(check.get("external_input_stores", {}).items())]


def artifact_reference(path):
    return {"path": str(path), "sha256": sha(path)}


def before_check_inputs(value, repo, check, out_dir, *, pinned_out_dir=None):
    external = check.get("external_input_stores", {})
    if external:
        out_dir = run_local_path(value, str(out_dir))
        out_dir = canonical_run_artifact(value, out_dir)
    paths = snapshot_inputs([path for path in check.get("input_paths", []) if path not in external],
                            out_dir, value["repo_paths"][repo], repo_read_excludes(value, repo), value=value,
                            pinned_out_dir=pinned_out_dir)
    if not external:
        return paths, None
    before = {"schema_version": VERSION, "run_id": value["run_id"], "repo": repo,
              "check_id": check["id"], "boundary": "before",
              "inputs": external_input_rows(value, check, command_input=True)}
    path = Path(out_dir) / "external-before.json"
    state.write_once(Path(pinned_out_dir) / path.name if pinned_out_dir is not None else path, before, pinned=True)
    return paths, ({"path": str(path), "sha256": guarded_sha(path, value)}, before)


def after_check_inputs(value, check, paths, observation):
    check_inputs_unchanged(paths, value)
    if observation is None:
        return paths, None
    reference, before = observation
    require(reference == {"path": reference["path"], "sha256": guarded_sha(reference["path"], value)},
            "External input observation changed")
    rows = external_input_rows(value, check, command_input=True)
    after = Path(reference["path"]).with_name("external-after.json")
    state.write_once(after, {**before, "boundary": "after", "inputs": rows}, pinned=True)
    require(rows == before["inputs"], "External check input changed during execution")
    return [*paths, reference["path"], str(after)], {"before": reference,
            "after": {"path": str(after), "sha256": guarded_sha(after, value)}}


def checked_reference(value, reference):
    require(isinstance(reference, dict) and set(reference) == {"path", "sha256"}, "Invalid artifact binding")
    path = checked_local_artifact(value, reference["path"])
    require(reference == {"path": str(path), "sha256": guarded_sha(path, value)}, "Bound artifact hash changed")
    return path


def validate_input_binding(value, repo, record, *, historical=False):
    checks = {check["id"]: check for group in ("preflight_checks", "pre_review_checks", "required_gates")
              for check in value.get(group, {}).get(repo, []) if check.get("external_input_stores")}
    claimed = {claim["id"] for claim in record["claims"]}.intersection(checks)
    binding = record.get("input_bindings")
    if binding is None and not claimed:
        return
    require(isinstance(binding, dict) and set(binding) == {"before", "after"},
            "External check needs input bindings before and after execution")
    execution_path = checked_local_artifact(value, record.get("execution_record"))
    require(str(execution_path) in record["artifact_paths"], "External input execution record must be sealed")
    execution = guarded_load(execution_path, value)
    check_id = execution.get("check_id")
    require(check_id in checks and claimed <= {check_id}, "Evidence has the wrong external input check")
    check = checks[check_id]
    require(record.get("command") == check_command(check), "Wrong command for external check inputs")
    by_boundary = {}
    for boundary, reference in binding.items():
        path = checked_reference(value, reference)
        require(str(path) in record["artifact_paths"], "External input manifest must be sealed")
        observation = guarded_load(path, value)
        bound(value, repo, observation)
        require(observation.get("check_id") == check_id and observation.get("boundary") == boundary,
                "Wrong external input check or observation boundary")
        by_boundary[boundary] = observation.get("inputs")
    rows = by_boundary["before"]
    require(isinstance(rows, list) and rows == by_boundary["after"], "External input observations differ")
    require(all(isinstance(row, dict) for row in rows)
            and [(row.get("path"), row.get("store")) for row in rows] == sorted(check["external_input_stores"].items()),
            "Missing or unexpected external check inputs")
    if not historical:
        persistent = [{**row, "directories": [entry[:2] for entry in row["directories"]]} for row in rows]
        require(persistent == external_input_rows(value, check, command_input=False), "External check input is stale")
    local = [path for path in check["input_paths"] if path not in check["external_input_stores"]]
    if local:
        registry = checked_local_artifact(value, str(Path(binding["before"]["path"]).with_name("manifest.json")))
        require(str(registry) in record["artifact_paths"], "Local input registry must be sealed")
        snapshots = guarded_load(registry, value)
        require([row["path"] for row in snapshots] == local, "Missing or unexpected local check inputs")
        for row in snapshots:
            path = checked_local_artifact(value, row["snapshot"])
            require(str(path) in record["artifact_paths"] and guarded_sha(path, value) == row["sha256"],
                    "Local input snapshot must match its seal")
        if not historical:
            check_inputs_unchanged([registry], value)


def read_deliverable_manifest(value, repo, path, *, slice_item=None, criterion_ids=None):
    entries, pending = deliverable_selection(value, repo, slice_item, criterion_ids)
    staged = staged_deliverables(value, repo)
    if not entries and (not path or not staged):
        return None, []
    require(path, "Missing required deliverable manifest for " + ", ".join(entry["id"] for entry in entries))
    manifest_path = (checked_local_artifact(value, path) if staged
                     else run_local_path(value, path, file=True))
    record = state.load(manifest_path)
    bound(value, repo, record)
    if staged:
        require(record.get("scope") == deliverable_scope_record(slice_item)
                and record.get("pending") == pending, "Wrong deliverable scope or pending IDs")
    rows = record.get("deliverables")
    unique_ids(rows, "deliverable manifest")
    indexed = {row["id"]: row for row in rows}
    require(set(indexed) == {e["id"] for e in entries}, "Incomplete deliverable manifest or wrong IDs")
    return manifest_path, [(entry, indexed[entry["id"]]) for entry in entries]


def validate_deliverable_row(value, entry, row, *, historical=False):
    require(row.get("path") == entry["path"], "Wrong deliverable path")
    if "store" in entry:
        name = entry["store"]
        require(row.get("store") == name and "snapshot" not in row, "Wrong STORE deliverable mode")
        if not historical:
            actual = store.observe(entry["path"], store_file(value, entry["path"], name), excluded_guard(value))
            require(row == {"id": entry["id"], "path": entry["path"], "store": name, **actual},
                    f"Published deliverable differs from snapshot: {entry['id']}")
        return []
    require("store" not in row, "Wrong local deliverable mode")
    snapshot = run_local_path(value, row.get("snapshot"), file=True)
    require(sha(snapshot) == row.get("sha256"), "Deliverable snapshot changed")
    if not historical:
        require(local_digest(entry["path"], value) == row["sha256"], "Published deliverable differs from snapshot")
    return [str(snapshot)]


def validate_deliverables(value, repo, path, criterion_ids=None, *, slice_item=None, historical=False):
    manifest_path, rows = read_deliverable_manifest(value, repo, path, slice_item=slice_item,
                                                    criterion_ids=criterion_ids)
    if manifest_path is None:
        return []
    sealed = [str(manifest_path)]
    for entry, row in rows:
        sealed += validate_deliverable_row(value, entry, row, historical=historical)
    return sealed


def deliverable_binding(value, repo, path, *, slice_item=None):
    paths = validate_deliverables(value, repo, path, slice_item=slice_item)
    selected, _ = deliverable_selection(value, repo, slice_item)
    reference = artifact_reference(paths[0]) if selected and staged_deliverables(value, repo) else None
    return reference, paths


def validate_delivery_reference(value, repo, record, *, historical=False):
    reference = record.get("deliverable_binding")
    if reference is None:
        return
    path = checked_reference(value, reference)
    require(str(path) in record["artifact_paths"], "Deliverable manifest must be sealed in evidence")
    scope = state.load(path).get("scope")
    require(isinstance(scope, dict), "Evidence needs a staged deliverable manifest")
    item = None
    if scope.get("scope") == "slice":
        item = selected_slice(value, repo, scope.get("slice_id"), value["slice_schedules"][repo]["path"])
    validate_deliverables(value, repo, str(path), slice_item=item, historical=historical)


def match_delivery_reference(record, expected):
    require(record.get("deliverable_binding") == expected, "Proof does not bind the current deliverable manifest")


def execution_references(artifacts, inputs, delivery):
    fields = {}
    if inputs is not None:
        fields["input_bindings"] = inputs
    if delivery is not None:
        artifacts.append(delivery["path"])
        fields["deliverable_binding"] = delivery
    return fields


def bound(value, repo, record):
    require(isinstance(record, dict), "Bound artifact must be an object")
    require(record.get("schema_version") == VERSION and record.get("run_id") == value["run_id"]
            and record.get("repo") == repo, "Wrong artifact version/run/repo")


def seal_files(paths, value=None):
    return {str(absolute_file(p, value)): (guarded_sha(p, value) if value is not None else sha(p)) for p in paths}


def extend_seals(value, previous, paths, delivery):
    additions = seal_files(paths, value if value.get("external_stores") else None)
    if delivery is not None:
        require(additions.get(delivery["path"]) == delivery["sha256"], "Deliverable manifest changed during validation")
    prior = previous.get("artifact_hashes", {})
    if value.get("external_stores") or staged_deliverables(value, previous.get("repo")):
        root = Path(value["_contract_path"]).parent
        require(all(prior.get(path, digest) == digest for path, digest in additions.items()
                    if Path(path).is_relative_to(root)), "Previously sealed run artifact changed during validation")
    return {**prior, **additions}


def check_seals(seals, value=None):
    for path, expected in seals.items():
        source = absolute_file(path, value)
        require((guarded_sha(source, value) if value is not None else sha(source)) == expected,
                f"Immutable artifact changed: {path}")


def check_event_seals(value, repo, event, inherited_seals=None):
    """Keep proof files immutable while source snapshots advance through authorized edits."""
    seals = event.get("artifact_hashes", {})
    source_manifest_path = event.get("source_manifest")
    for path in seals:
        guard_path(path, value)
    if not source_manifest_path:
        check_seals(seals, value)
        return list(seals)

    historical = manifest(value, repo, source_manifest_path)
    names = source_names(value, repo)
    repo_root = names.root
    historical_files = historical["files"]
    inherited_seals = inherited_seals or {}
    immutable = {}
    for path, expected in seals.items():
        candidate = Path(path)
        try:
            relative = candidate.resolve(strict=False).relative_to(repo_root).as_posix()
        except (OSError, ValueError):
            immutable[path] = expected
            continue

        source_entry = historical_files.get(relative)
        if source_entry is None:
            candidates = {p: record for p, record in historical_files.items()
                          if state.spelling_key(p) == state.spelling_key(relative)}
            if candidates:
                source_entry = names.files(candidates).get(names.key(relative))
        if source_entry and source_entry.get("kind") == "file" and source_entry.get("sha256"):
            historically_bound = source_entry["sha256"] == expected
            inherited_bound = inherited_seals.get(path) == expected
            require(historically_bound or inherited_bound,
                    f"Source artifact seal does not match its historical manifest or prior state: {path}")
            continue
        immutable[path] = expected

    check_seals(immutable, value)
    return list(immutable)


def execution_binding(record, value=None):
    if not record.get("execution_record"):
        return
    execution_path = str(absolute_file(record["execution_record"], value))
    require(execution_path in record.get("artifact_paths", []), "Execution record must be sealed")
    execution = guarded_load(execution_path, value) if value is not None else state.load(execution_path)
    for field in ("producer", "command", "exit_code"):
        require(execution.get(field) == record.get(field), "Execution record differs: " + field)
    if "input_bindings" in execution or "input_bindings" in record:
        require(execution.get("input_bindings") == record.get("input_bindings"), "Execution input bindings differ")
    if "deliverable_binding" in execution or "deliverable_binding" in record:
        match_delivery_reference(record, execution.get("deliverable_binding"))
    require(execution.get("log") in record.get("artifact_paths", []), "Execution log must be sealed")


def proof(value, repo, path, final, identifier, producer=None, expected_status="passed", *, historical_seals=None,
          delivery=UNSPECIFIED_DELIVERY):
    record = state.load(absolute_file(path, value))
    bound(value, repo, record)
    claims = record.get("claims")
    require(isinstance(claims, list) and claims, "Evidence needs explicit claims/assertions")
    unique_ids(claims, "evidence claims")
    for claim in claims:
        state.text(claim.get("assertion"), "claim assertion")
    require(identifier in {x["id"] for x in claims}, "Evidence does not prove requested criterion")
    if requirement_amendment_suffix(value, identifier):
        require(record.get("amendment_sha256") in requirement_amendment_suffix(value, identifier),
                "Evidence lacks amendment binding")
    expected = spec(value, repo, identifier)
    require(record.get("kind") == expected["kind"], "Evidence changes required verification kind")
    if producer:
        require(record.get("producer") == producer, "Check was not independently executed")
    require(record.get("status") == expected_status, "Required evidence did not pass")
    if expected.get("command") or expected.get("argv"):
        require(record.get("command") == check_command(expected), "Wrong check command")
    execution_binding(record, value)
    for field in ("environment", "data_scope"):
        if expected.get(field):
            require(record.get(field) == expected[field], f"Wrong evidence {field}")
    state.evidence(record, final, repo_read_excludes(value, repo),
                   historical=historical_seals is not None, guard=lambda p: guard_path(p, value))
    paths = [path, record["before_manifest"], record["after_manifest"], *record["artifact_paths"]]
    hashes = record.get("artifact_hashes")
    require(isinstance(hashes, dict) and set(hashes) == set(paths[1:]), "Missing evidence artifact hashes")
    if historical_seals is None:
        check_seals(hashes, value)
    else:
        require(all(historical_seals.get(str(Path(p))) == digest for p, digest in hashes.items()),
                "Historical evidence does not match its state seals")
    validate_input_binding(value, repo, record, historical=historical_seals is not None)
    if delivery is not UNSPECIFIED_DELIVERY:
        match_delivery_reference(record, delivery)
        if delivery is not None:
            require(hashes.get(delivery["path"]) == delivery["sha256"], "Deliverable manifest must be sealed in evidence")
    else:
        validate_delivery_reference(value, repo, record, historical=historical_seals is not None)
    for dependency, dependency_path in record.get("dependency_manifests", {}).items():
        require(dependency in value["repos"], "Unknown evidence dependency")
        dep = manifest(value, dependency, dependency_path)
        if historical_seals is None:
            _, excludes = (capture_policy(value["_contract_path"], dependency)
                           if value.get("_contract_path") else ([], []))
            fresh(value, dependency, dependency_path, list(dep["files"]), excludes)
        paths.append(dependency_path)
    # A criterion spanning repositories must name the source versions it depends on.
    dependencies = set(expected.get("repos", [])) - {repo}
    require(dependencies <= set(record.get("dependency_manifests", {})), "Missing cross-repo source binding")
    return paths


def gate_failure_proof(value, repo, path, previous, *, historical_seals=None):
    failure = state.load(absolute_file(path, value))
    claims = failure.get("claims", [])
    require(claims, "Gate fix needs recorded failed check")
    identifier = claims[0]["id"]
    allowed = {x["id"] for group in ("required_gates", "pre_review_checks") for x in value[group][repo]}
    require(identifier in allowed, "Gate failure does not name a declared gate")
    paths = proof(value, repo, path, event_manifest(value, repo, previous), identifier,
                  "orchestrator", expected_status="failed", historical_seals=historical_seals)
    if historical_seals is not None:
        require({str(Path(path)) for path in paths} <= historical_seals.keys(),
                "Historical gate failure proof is not sealed")
    return paths


def finding_proof(value, repo, path, final, identifier, kind, historical_seals=None):
    record = state.load(absolute_file(path, value))
    bound(value, repo, record)
    require(record.get("kind") == kind and record.get("status") == "passed", "Finding proof unverified")
    claims = record.get("claims", [])
    unique_ids(claims, "finding claims")
    require(identifier in {c["id"] for c in claims}, "Wrong finding proof")
    for claim in claims:
        state.text(claim.get("assertion"), "finding assertion")
    state.evidence(record, final, repo_read_excludes(value, repo),
                   historical=historical_seals is not None, guard=lambda p: guard_path(p, value))
    paths = [record["before_manifest"], record["after_manifest"], *record["artifact_paths"]]
    hashes = record.get("artifact_hashes")
    require(isinstance(hashes, dict) and set(hashes) == set(paths), "Missing finding artifact hashes")
    if historical_seals is None:
        check_seals(hashes, value)
    else:
        require(all(historical_seals.get(p) == digest for p, digest in hashes.items()),
                "Historical finding proof does not match its state seals")
    if "input_bindings" in record or "deliverable_binding" in record:
        execution_binding(record, value)
        validate_input_binding(value, repo, record, historical=historical_seals is not None)
        validate_delivery_reference(value, repo, record, historical=historical_seals is not None)
    return [path, *paths]


def applied_findings(value, repo, path, review_path, final, historical_seals=None):
    result = state.load(absolute_file(path, value))
    bound(value, repo, result)
    findings = state.load(absolute_file(review_path, value))
    state.applied(result, findings)
    kinds = {item["id"]: item["verification_kind"] for item in findings["findings"]}
    sealed = [path]
    for item in result["applied"]:
        kind = kinds[item["id"]]
        require(item.get("verification_kind") == kind, "Applied kind mismatch")
        require(item.get("evidence_paths"), "Applied finding needs evidence")
        for proof_path in item["evidence_paths"]:
            sealed += finding_proof(value, repo, proof_path, final, item["id"], kind, historical_seals)
    return result, sealed


def check_set(value, repo, group, paths, final, *, delivery=UNSPECIFIED_DELIVERY):
    require(isinstance(paths, list), f"Missing {group} evidence")
    sealed = []
    for item in value[group][repo]:
        matches = [p for p in paths if item["id"] in
                   {x.get("id") for x in state.load(absolute_file(p, value)).get("claims", [])}]
        require(matches, f"Missing check: {item['id']}")
        sealed += proof(value, repo, matches[-1], final, item["id"], "orchestrator", delivery=delivery)
    return sealed


def has_bound_pre_review(value, repo):
    return staged_deliverables(value, repo) or any(
        check.get("external_input_stores") for check in value["pre_review_checks"][repo])


def has_bound_gates(value, repo):
    return has_bound_pre_review(value, repo) or any(
        check.get("external_input_stores") for check in value["required_gates"][repo])


def recheck_pre_review(value, repo, event, final, delivery):
    if has_bound_pre_review(value, repo):
        return check_set(value, repo, "pre_review_checks", event.get("pre_review_evidence_paths"), final,
                         delivery=delivery)
    return []


def stale_binding(value, repo, event, *, slice_item=None):
    if not (has_bound_pre_review(value, repo) if slice_item is not None else has_bound_gates(value, repo)):
        return False
    try:
        delivery, _ = deliverable_binding(value, repo, event.get("deliverable_manifest_path"), slice_item=slice_item)
        recheck_pre_review(value, repo, event, event_manifest(value, repo, event), delivery)
        if slice_item is None:
            check_slice_delivery(value, repo, event.get("slice_gate_path"), event.get("deliverable_manifest_path"))
            for proof_path in event.get("gate_evidence_paths", []):
                proof_record = state.load(absolute_file(proof_path, value))
                validate_input_binding(value, repo, proof_record)
    except (OSError, ValueError):
        return True
    return False


def recorded_binding_recheck(value, repo, event, previous, *, slice=False):
    if event.get("status") != "edited-unverified" or event.get("action") != "binding-recheck":
        return False
    allowed = {"pre-review-passed", "reviewed"} if slice else {"pre-review-passed", "reviewed", "gates-passed", "verified"}
    require(previous.get("status") in allowed and not previous.get("review_acceptance_path")
            and (has_bound_pre_review(value, repo) if slice else has_bound_gates(value, repo)),
            "Invalid historical binding recheck boundary")
    require(state.same_source(event_manifest(value, repo, previous), event_manifest(value, repo, event),
                              repo_read_excludes(value, repo)), "Historical binding recheck changed source")
    return True


def check_delivery(value, repo, group, path=None, *, slice_item=None):
    if group == "preflight_checks":
        require(path is None and slice_item is None, "Preflight has no deliverable scope")
        return None
    if group == "required_gates":
        require(slice_item is None, "Required gates apply to the whole repo")
        if has_bound_pre_review(value, repo):
            events = history(Path(value["_contract_path"]).parent / repo, value)
            require(events and events[-1][2]["status"] in {"reviewed", "reviewed-with-findings", "gates-passed"},
                    "Required gates need current whole-repo review")
            latest = events[-1][2]
            expected = latest.get("deliverable_manifest_path")
            require(path is None or path == expected, "Required gates need the reviewed deliverable manifest")
            checked = state.load(absolute_file(latest["review_path"], value))
            reference, _ = deliverable_binding(value, repo, expected)
            if reference is not None:
                match_delivery_reference(checked, reference)
                require(latest.get("artifact_hashes", {}).get(expected) == reference["sha256"],
                        "Reviewed deliverable manifest seal changed")
            recheck_pre_review(value, repo, latest, event_manifest(value, repo, latest), reference)
            check_slice_delivery(value, repo, latest.get("slice_gate_path"), expected)
            return reference
    if path is not None or staged_deliverables(value, repo):
        return deliverable_binding(value, repo, path, slice_item=slice_item)[0]
    return None


def preflight(value, repo, path, initial):
    result = state.load(absolute_file(path, value))
    bound(value, repo, result)
    require(result.get("status") in {"passed", "blocked"}, "Invalid preflight status")
    sealed = [path]
    if result["status"] == "passed":
        sealed += check_set(value, repo, "preflight_checks", result.get("evidence_paths"), initial)
        for item in value.get("prerequisites", []):
            if item["repo"] == repo:
                require(item["evidence_paths"], "Missing prerequisite evidence")
                sealed += proof(value, repo, item["evidence_paths"][-1], initial,
                                item["id"], "orchestrator")
    else:
        state.text(result.get("reason"), "preflight blocker")
    return result, sealed


def criteria_for(value, repo):
    return [x for x in value["criteria"] if repo in x["repos"] and x["mandatory"]
            and x.get("assessment_repo", repo) == repo]


def matrix(value, repo, path, final):
    record = state.load(absolute_file(path, value))
    bound(value, repo, record)
    entries = record.get("criteria")
    require(isinstance(entries, list), "Missing completion matrix")
    ids = [e.get("criterion_id") for e in entries]
    require(len(set(ids)) == len(ids), "Duplicate matrix criteria")
    required = {c["id"] for c in criteria_for(value, repo)}
    require(required <= set(ids), "Incomplete implementation matrix")
    sealed = [path]
    for entry in entries:
        spec(value, repo, entry["criterion_id"])
        require(entry.get("status") in {"verified", "unverified"}, "Invalid matrix status")
        for field in ("implementation", "assertion", "reason"):
            state.text(entry.get(field), "matrix " + field)
        require(isinstance(entry.get("evidence_paths"), list), "Missing matrix evidence list")
        if entry["status"] == "verified":
            require(entry["evidence_paths"], "Verified matrix row lacks proof")
        for evidence_path in entry["evidence_paths"]:
            evidence_status = state.load(absolute_file(evidence_path, value)).get("status")
            if entry["status"] == "verified":
                evidence_status = "passed"
            require(evidence_status in {"passed", "failed", "unverified"}, "Invalid evidence status")
            sealed += proof(value, repo, evidence_path, final, entry["criterion_id"], expected_status=evidence_status)
    return sealed


def submission_evidence_errors(value, repo, path, final, key):
    record = state.load(absolute_file(path, value))
    bound(value, repo, record)
    entries = record.get(key)
    require(isinstance(entries, list), "Missing matrix rows")
    errors = []
    for entry in entries:
        identifier = entry.get("criterion_id")
        try:
            spec(value, repo, identifier)
            paths = entry.get("evidence_paths")
            require(isinstance(paths, list), "Missing matrix evidence list")
            require(entry.get("status") != "verified" or paths, "Verified matrix row lacks proof")
        except (ValueError, TypeError, KeyError) as error:
            errors.append(f"{identifier}: {error}")
            continue
        for path in paths:
            try:
                status = state.load(absolute_file(path, value)).get("status")
                if entry.get("status") == "verified":
                    status = "passed"
                require(status in {"passed", "failed", "unverified"}, "Invalid evidence status")
                proof(value, repo, path, final, identifier, expected_status=status)
            except (ValueError, TypeError, KeyError, OSError) as error:
                errors.append(f"{identifier} / {path}: {error}")
    return errors


def review(value, repo, path, final, scope, prior_paths, run_root=None, evidence_seals=None):
    result = state.load(absolute_file(path, value))
    bound(value, repo, result)
    prior = [state.load(absolute_file(p, value)) for p in prior_paths]
    state.findings(result, repo, result.get("round"), final, prior)
    require(result.get("accepted") is True, "Review was not accepted on stable source")
    coverage = result["coverage"]
    reads, skips = coverage["read"], coverage["skipped"]
    require(all(isinstance(p, str) for p in reads), "Coverage read entries must be paths")
    for entry in skips:
        require(isinstance(entry, dict), "Skipped coverage needs path/reason")
        state.relative_path(entry.get("path"))
        state.text(entry.get("reason"), "skipped coverage reason")
    for p in reads:
        state.relative_path(p)
    covered = reads + [e["path"] for e in skips]
    require(len(set(covered)) == len(covered), "Duplicate review coverage")
    names = source_names(value, repo)
    scope_keys = names.keys(scope)
    require(scope_keys <= names.keys(covered), "Review omits scoped paths")
    passes = coverage.get("mandatory_passes")
    unique_ids(passes, "mandatory passes")
    require(set(value["required_passes"][repo]) <= {p["id"] for p in passes}, "Missing rubric pass")
    for item in passes:
        require(item.get("status") in {"inspected", "not-applicable"}, "Invalid rubric pass")
        state.text(item.get("reason"), "rubric pass reason")
        require(isinstance(item.get("sites"), list), "Rubric pass needs sites")
        if item["status"] == "inspected":
            require(item["sites"], "Inspected pass has no sites")
    sealed = [path]
    old = {f["id"] for f in prior[-1]["findings"]} if prior else set()
    current = {f["id"] for f in result["findings"]}
    dispositions = result.get("dispositions", [])
    ids = [d.get("id") for d in dispositions]
    require(len(set(ids)) == len(ids) and set(ids) == old - current, "Missing closed-finding adjudication")
    for item in dispositions:
        require(item.get("status") in {"resolved", "dismissed"}, "Invalid finding disposition")
        state.text(item.get("reason"), "finding disposition reason")
        require(item.get("evidence_paths"), "Disposition requires evidence")
        original = next(f for f in prior[-1]["findings"] if f["id"] == item["id"])
        kind = original["verification_kind"] if item["status"] == "resolved" else "static"
        for p in item["evidence_paths"]:
            sealed += finding_proof(value, repo, p, final, item["id"], kind)
    for item in result["findings"]:
        require(item.get("verification_kind") in {"static", "empirical"}, "Finding lacks verification kind")
        if names.key(item["file"]) not in scope_keys:
            require(run_root is not None and result.get("finding_path_root") == str(run_root),
                    "Finding must anchor scoped cause or sealed run-local evidence")
            anchor = (run_root / item["file"]).resolve()
            require(anchor.is_relative_to(run_root) and str(anchor) in (evidence_seals or {}),
                    "Finding evidence anchor must be a previously sealed run-local artifact")
            require(anchor.is_file() and sha(anchor) == evidence_seals[str(anchor)],
                    "Finding evidence anchor seal mismatch")
            sealed.append(str(anchor))
        for identifier in item["criterion_ids"]:
            spec(value, repo, identifier)
    return result, sealed


def assessment(value, repo, path, final, initial=None, *, delivery=None):
    result = state.load(absolute_file(path, value))
    bound(value, repo, result)
    if delivery is not None:
        match_delivery_reference(result, delivery)
    delivered_criteria = {identifier for entry in value.get("deliverables", {}).get(repo, [])
                          for identifier in entry["criterion_ids"]}
    allowed = {"checker"} if value["workflow"] == "ship" else {"checker", "orchestrator"}
    require(result.get("producer") in allowed, "Acceptance cannot be self-certified by implementer")
    require(result.get("requirements_match") is True, "Approved requirements not semantically confirmed")
    if value.get("_amendment"):
        require(result.get("amendment_sha256") == value["_amendment"]["sha256"],
                "Checker must independently assess the amended requirement")
    rows = result.get("criteria", [])
    ids = [r.get("criterion_id") for r in rows]
    require(len(set(ids)) == len(ids), "Duplicate acceptance assessments")
    require({c["id"] for c in criteria_for(value, repo)} <= set(ids), "Missing acceptance assessment")
    sealed = [path]
    for row in rows:
        spec(value, repo, row["criterion_id"])
        require(row.get("implementation") == "met" and row.get("verification") == "verified",
                "Acceptance criterion not met and verified")
        state.text(row.get("reason"), "acceptance reasoning")
        require(row.get("evidence_paths"), "Missing acceptance proof")
        for p in row["evidence_paths"]:
            if value["workflow"] == "fix" and row["criterion_id"] in value["acceptance"]["criterion_ids"]:
                require(state.load(absolute_file(p)).get("scenario") == value["acceptance"]["original_scenario"],
                        "Acceptance check exercised a different original scenario")
            sealed += proof(value, repo, p, final, row["criterion_id"], delivery=(
                delivery if delivery is not None and row["criterion_id"] in delivered_criteria else UNSPECIFIED_DELIVERY))
    if value["workflow"] == "fix" and value["acceptance"]["mode"] == "reproduced-bug":
        baseline_path = value["acceptance"].get("before_evidence", {}).get(repo)
        if any(c["id"] in value["acceptance"]["criterion_ids"] for c in criteria_for(value, repo)):
            require(baseline_path, "Reproduced bug needs recorded before failure")
            record = state.load(absolute_file(baseline_path))
            baseline = manifest(value, repo, record["after_manifest"])
            require(initial is not None, "Missing initial source for before failure")
            state.match(baseline, initial)
            require(record.get("scenario") == value["acceptance"]["original_scenario"],
                    "Before check exercised a different scenario")
            scenario_ids = set(value["acceptance"]["criterion_ids"]) & {c["id"] for c in criteria_for(value, repo)}
            for identifier in scenario_ids:
                sealed += proof(value, repo, baseline_path, baseline, identifier, expected_status="failed")
    return sealed


def history(directory, value=None):
    if value is not None and read_policy(value):
        return guarded_history(directory, value)
    events = []
    for path in guard_path(directory, value).glob("state-*.json"):
        match = re.fullmatch(r"state-(\d+)\.json", path.name)
        if match:
            record = state.load(guard_path(path, value))
            require(isinstance(record, dict), "Malformed lifecycle event")
            events.append((int(match[1]), path, record))
    events.sort()
    require([e[0] for e in events] == list(range(1, len(events) + 1)), "Broken lifecycle event sequence")
    return events


def capture_policy(contract_path, repo):
    """Use only the sealed initialized source scope and exclusions for later captures."""
    events = history(Path(contract_path).resolve().parent / repo)
    if not events:
        return [], []
    initial = events[0][2]
    require(initial.get("status") == "initialized", "Missing initialized source state")
    return initial.get("source_scope", []), initial.get("excludes", [])


def continuity_history_seals(item):
    if "recovery_sources" in item:
        sources = item["recovery_sources"]
        require(isinstance(sources, list) and sources and all(isinstance(source, dict) for source in sources),
                "Budget recovery requires source histories")
        seals = [history_seal(source, "history_sha256") for source in sources]
        require(not any(seals) or all(seals), "Budget recovery has incomplete source history seals")
        return seals
    return [history_seal(item, "source_history_sha256")]


class GateFixLineage:
    """Collect validated predecessor histories for one repository and one operation."""

    def __init__(self, value, repo, cache=None):
        self.value = value
        self.repo = repo
        self.cache = {} if cache is None else cache
        self.direct = {}
        self.runs = {}

    def cache_key(self, resolved):
        return resolved, self.repo, read_policy(self.value)

    def audit(self, path, visited, *, work_item=None, expected=None):
        guard_path(path, self.value)
        resolved = guarded_resolve(path, self.value)
        require(resolved not in visited, "Historical budget reference cycle/current-run ancestry detected")
        repo = self.repo
        root = self.value["repo_paths"][repo]
        inherited = read_policy(self.value)
        source = with_read_policy(guarded_load(path, {"_read_policy": inherited}), inherited)
        guard_path(path, source)
        require(guard_path(source.get("repo_paths", {}).get(repo, ""), source).resolve()
                == guard_path(root, self.value).resolve(),
                "Gate-fix source repository mismatch")
        work_item = work_item or self.value.get("budget_work_items", {}).get(repo)
        source_items = {entry.get("work_item") for entry in source.get("budget_continuity", [])
                        if entry.get("repo") == repo and entry.get("slice_id") is None}
        source_item = source.get("budget_work_items", {}).get(repo)
        if source_item:
            source_items.add(source_item)
        require(not work_item or not source_items or source_items == {work_item}, "Gate-fix source work item mismatch")
        current_id = (self.value.get("workflow"), self.value.get("run_id"))
        source_id = (source["workflow"], source["run_id"])
        require(source_id != current_id, "Gate-fix source duplicates the current run")
        key = self.cache_key(resolved)
        if key not in self.cache:
            ancestors = {}
            snapshot = self.snapshot(path, repo, source)
            audited = validate_history(path, repo, visited, inherited_policy=inherited,
                                       lineage_cache=self.cache, lineage_out=ancestors)
            require(snapshot == self.snapshot(path, repo, source), "Gate-fix source changed during audit")
            self.cache[key] = audited, ancestors, snapshot, source
        audited, ancestors, _, _ = self.cache[key]
        require(not any(snapshot[0] in visited for snapshot in ancestors.values()),
                "Historical budget reference cycle/current-run ancestry detected")
        require(current_id not in ancestors, "Gate-fix ancestry duplicates the current run")
        guard_path(audited["state_path"], source)
        history_hash = audited["history_sha256"]
        if expected is not None:
            require(history_hash == expected, "Gate-fix source history SHA-256 mismatch")
        require(self.direct.setdefault(str(resolved), history_hash) == history_hash,
                "Conflicting gate-fix source snapshots")
        snapshot = (resolved, history_hash, audited["gate_fix_budget"]["current_used_attempts"],
                    frozenset(source_items))
        for identity, record in [*ancestors.items(), (source_id, snapshot)]:
            _, _, _, recorded_items = record
            require(not work_item or not recorded_items or recorded_items == {work_item},
                    "Gate-fix source work item mismatch")
            require(self.runs.setdefault(identity, record) == record, "Duplicate or conflicting gate-fix run import")
        return audited

    def summary(self, current):
        historical = sum(snapshot[2] for snapshot in self.runs.values())
        return {"limit": GATE_FIX_LIMIT, "historical_used_attempts": historical, "current_used_attempts": current,
                "cumulative_used_attempts": historical + current,
                "remaining_attempts": max(0, GATE_FIX_LIMIT - historical - current)}

    def source_hash(self):
        return state.digest(sorted(self.direct.items()))

    def history_hash(self, contract_hash, states):
        return state.digest({"contract_sha256": contract_hash, "states": states,
                             "sources": sorted(self.direct.items())})

    @staticmethod
    def snapshot(path, repo, value):
        return (guarded_sha(guard_path(path, value), value), [(event_path.name, guarded_sha(event_path, value))
                                              for _, event_path, _ in history(path.parent / repo, value)])


def check_gate_sources_current(cache):
    for (path, repo, _), (_, _, snapshot, value) in cache.items():
        require(GateFixLineage.snapshot(path, repo, value) == snapshot, "Gate-fix source changed during audit")


def check_gate_budget_event(events, index, contract_seal, lineage, expect_budget):
    sequence, path, event = events[index]
    require(path.resolve().parent == path.parent.resolve(), "Gate-fix state is outside its repo history")
    previous = events[index - 1][2] if index else {}
    repair = event["status"] == "edited-unverified" and event.get("action") == "gate-fix"
    if repair:
        require(index > 0, "Gate fix needs a preceding lifecycle state")
        gate_failure_proof(lineage.value, lineage.repo, event.get("gate_failure_path"), previous,
                           historical_seals=event.get("artifact_hashes", {}))
    current = event.get("gate_fix_count", 0)
    require(type(current) is int and current == previous.get("gate_fix_count", 0) + int(repair),
            "Historical gate-fix counter is not derived from repair events")
    require(current <= GATE_FIX_LIMIT, "Historical local gate-fix budget exceeded")
    budget = lineage.summary(current)
    if expect_budget or "gate_fix_budget" in event or "gate_fix_sources_sha256" in event:
        require(state.digest(event.get("gate_fix_budget")) == state.digest(budget)
                and event.get("gate_fix_sources_sha256") == lineage.source_hash(),
                "Historical gate-fix budget disagrees with audited history")
        require(not repair or budget["cumulative_used_attempts"] <= GATE_FIX_LIMIT,
                "Historical cumulative gate-fix budget exceeded")
        if index:
            previous_path = str(events[index - 1][1])
            require(event.get("artifact_hashes", {}).get(previous_path) == sha(previous_path),
                    "Missing or changed gate-fix predecessor seal")
    contract_path, contract_hash = contract_seal
    require(event.get("sequence") == sequence
            and event.get("artifact_hashes", {}).get(contract_path) == contract_hash,
            "Gate-fix history does not bind its sequence and contract")
    return budget


def historical_budget_sources(sources, repo, workflow, current_contract=None, visited_contracts=None,
                              gate_lineage=None, *, value=None):
    """Revalidate prior-run counters cited to carry budget usage into this run."""
    require(isinstance(sources, list) and sources, "Historical budget needs source runs")
    seen, validated, total = set(), [], 0
    for source in sources:
        require(isinstance(source, dict), "Invalid historical budget source")
        contract_path = absolute_file(source.get("contract_path"), value)
        require(current_contract is None or contract_path.resolve() != Path(current_contract).resolve(),
                "Historical budget cannot reference its current run")
        require(contract_path.resolve() not in (visited_contracts or ()),
                "Historical budget reference cycle/current-run ancestry detected")
        require(contract_path.resolve() not in seen, "Duplicate historical budget contract")
        seen.add(contract_path.resolve())
        require(sha(contract_path) == source.get("contract_sha256"),
                "Historical budget contract hash mismatch")
        historical_contract = state.load(contract_path)
        require(contract_path.parent.name == historical_contract.get("run_id"),
                "Historical budget contract/run directory mismatch")
        require(historical_contract.get("workflow") == workflow
                and repo in historical_contract.get("repos", []),
                "Historical budget workflow/repo mismatch")
        inherited = read_policy(value)
        historical_contract = with_read_policy(historical_contract, inherited)
        guard_path(contract_path, historical_contract)
        state_path = absolute_file(source.get("state_path"), historical_contract)
        directory = guard_path(contract_path.parent / repo, historical_contract)
        require(state_path.parent.resolve() == directory.resolve(),
                "Historical budget state is outside its repo history")
        prior_events = history(directory, historical_contract)
        require(prior_events and state_path.resolve() == prior_events[-1][1].resolve()
                and sha(state_path) == source.get("state_sha256"),
                "Historical budget state hash/path mismatch")
        audited = (gate_lineage.audit(contract_path, visited_contracts or set()) if gate_lineage is not None
                   else validate_history(contract_path, repo, visited_contracts, inherited_policy=inherited))
        require(state_path.resolve() == Path(audited["state_path"]).resolve()
                and audited["state_sha256"] == source.get("state_sha256")
                and sha(state_path) == source.get("state_sha256"),
                "Historical budget state hash/path mismatch")
        require(audited["run_id"] == source.get("run_id")
                and audited["review_fix_count"] == source.get("used_passes")
                and audited["review_fix_limit"] == source.get("effective_limit"),
                "Historical budget usage differs from audited run")
        used = audited["review_fix_count"]
        require(type(used) is int and used >= 0, "Invalid historical review-fix usage")
        total += used
        validated.append({"run_id": audited["run_id"], "contract_path": str(contract_path),
                          "contract_sha256": sha(contract_path), "state_path": str(state_path),
                          "state_sha256": sha(state_path), "used_passes": used,
                          "effective_limit": audited["review_fix_limit"]})
    return total, validated


def historical_budget_used(grants):
    """Carry non-shrinking historical usage through later grants."""
    return max((item.get("historical_used_passes", 0) for item in grants), default=0)


def audit_slice_history(contract_path, repo, slice_id, *, inherited_policy=(), visited_contracts=(), strict=False,
                        cache=None):
    """Audit a slice and bind every state and continuity source to one history hash."""
    source = absolute_file(contract_path, {"_read_policy": inherited_policy})
    resolved = guarded_resolve(source, {"_read_policy": inherited_policy})
    visited = set(visited_contracts)
    require(resolved not in visited, "Historical slice reference cycle/current run")
    visited.add(resolved)
    cache = {} if cache is None else cache
    cache_key = (resolved, repo, slice_id, tuple(inherited_policy), strict)
    if cache_key in cache:
        return cache[cache_key]
    old = with_read_policy(guarded_load(source, {"_read_policy": inherited_policy}), inherited_policy)
    guard_path(source, old)
    require(old.get("schema_version") == VERSION and old.get("workflow") in {"ship", "fix"}
            and source.parent.name == old.get("run_id") and repo in old.get("repos", []),
            "Invalid historical slice contract")
    if old.get("setup_replacement") is not None:
        replacement_chain(old, source)
        pending = reserve_predecessors(old, source, guarded_sha(source, old), old)
        require(not pending, "Predecessor reservation is missing")
    state.relative_path(repo)
    state.relative_path(slice_id)
    events = guarded_history(source.parent / repo / "slices" / slice_id, old)
    require(events, "Continuity source history missing")
    inherited, prior_count, prior_status = {}, 0, None
    for index, (_, _, event) in enumerate(events):
        bound(old, repo, event)
        require(event.get("artifact_hashes", {}).get(str(source)) == guarded_sha(source, old),
                "Continuity history does not seal its contract")
        require(event.get("slice_id") == slice_id, "Continuity slice mismatch")
        status = event.get("status")
        require((prior_status is None and status == "implemented") or
                (prior_status is not None and (status in SLICE_TRANSITIONS.get(prior_status, set())
                    or status in {"blocked", "capped"}
                    or recorded_binding_recheck(old, repo, event, events[index - 1][2], slice=True)
                    or (prior_status == "capped" and status == "needs-fix"
                        and event.get("is_continuation") is True))), "Invalid continuity slice transition")
        prior_status = status
        if status == "edited-unverified" and event.get("action") == "review-fix":
            prior_count += 1
        require(event.get("review_fix_count", 0) == prior_count,
                "Continuity slice counter is inconsistent with events")
        check_event_seals(old, repo, event, inherited)
        inherited = event.get("artifact_hashes", {})
    prior = budget_continuity(old, repo, source, slice_id, visited_contracts=visited - {resolved}, slice_cache=cache)
    if strict:
        limit = prior["effective_limit"] - prior["used_passes"] if prior else SLICE_REVIEW_FIX_BUDGET
        used_authorizations = set()
        for index, (_, path, event) in enumerate(events):
            if event.get("is_continuation"):
                require(index > 0 and events[index - 1][2].get("status") == "capped"
                        and event.get("status") == "needs-fix", "Invalid slice budget continuation")
                authorization = absolute_file(event.get("continuation_path"), old)
                require(authorization.resolve().is_relative_to(source.parent.resolve())
                        and authorization not in used_authorizations
                        and event.get("artifact_hashes", {}).get(str(authorization)) == guarded_sha(authorization, old),
                        "Invalid slice continuation authorization seal")
                used_authorizations.add(authorization)
                record = guarded_load(authorization, old)
                bound(old, repo, record)
                previous_path, previous = events[index - 1][1:]
                require(record.get("slice_id") == slice_id and record.get("previous_state_path") == str(previous_path)
                        and record.get("previous_state_sha256") == guarded_sha(previous_path, old)
                        and record.get("author_agent_id") == previous.get("author_agent_id"),
                        "Invalid slice continuation authorization binding")
                state.text(record.get("user_authorization"), "slice continuation authorization")
                require(timestamp(previous["at"]) <= timestamp(record.get("approved_at")) <= timestamp(event["at"]),
                        "Invalid historical slice continuation approval time")
                review = guarded_load(absolute_file(previous.get("review_path"), old), old)
                require(set(record.get("finding_ids", [])) == {finding["id"] for finding in review["findings"]},
                        "Slice continuation finding scope differs")
                extra = record.get("additional_passes")
                require(type(extra) is int and extra > 0, "Invalid slice continuation allowance")
                limit = max(limit, previous.get("review_fix_count", 0)) + extra
            require(event.get("review_fix_limit", limit) == limit
                    and event.get("review_fix_count", 0) <= limit, "Slice review budget differs from history")
            if index:
                previous_path = str(events[index - 1][1])
                require(event.get("artifact_hashes", {}).get(previous_path) == guarded_sha(previous_path, old),
                        "Slice history does not seal its predecessor")
    sources = []
    if prior:
        previous = audit_slice_history(prior["source_contract_path"], repo, prior["source_slice_id"],
                                       inherited_policy=read_policy(old), visited_contracts=visited, strict=strict,
                                       cache=cache)
        sources.append([str(guarded_resolve(prior["source_contract_path"], old)), previous["history_sha256"]])
    latest = events[-1][2]
    result = {"run_id": old["run_id"], "repo": repo, "slice_id": slice_id,
            "state_path": str(events[-1][1]), "state_sha256": guarded_sha(events[-1][1], old),
            "review_fix_count": prior_count, "historical_used_passes": prior["used_passes"] if prior else 0,
            "review_fix_limit": latest.get("review_fix_limit", SLICE_REVIEW_FIX_BUDGET),
            "history_sha256": state.digest({"contract_sha256": guarded_sha(source, old),
                "states": [(path.name, guarded_sha(path, old)) for _, path, _ in events], "sources": sources}),
            "states": len(events), "status": "valid"}
    cache[cache_key] = result
    return result


def budget_continuity(value, repo, contract_path, slice_id=None, *, visited_contracts=(), gate_lineage=None,
                      slice_cache=None):
    """Validate an explicitly approved carryover without changing historical counters."""
    entries = value.get("budget_continuity", [])
    require(isinstance(entries, list), "Invalid budget continuity")
    keys = [(item.get("repo"), item.get("slice_id")) for item in entries if isinstance(item, dict)]
    require(len(keys) == len(entries) and len(set(keys)) == len(keys), "Duplicate budget continuity scope")
    matches = [item for item in entries if item.get("repo") == repo and item.get("slice_id") == slice_id]
    if not matches:
        return None
    item = matches[0]
    visited = set(visited_contracts) | {guarded_resolve(contract_path, value)}
    if "recovery_sources" in item:
        return recovery_continuity(value, repo, item, slice_id, visited, gate_lineage)
    require(item.get("approved_by") == "user", "Budget continuity requires user approval")
    for key in ("work_item", "user_authorization", "reason"):
        state.text(item.get(key), key)
    timestamp(item.get("approved_at"))
    used, limit = item.get("used_passes"), item.get("effective_limit")
    require(type(used) is int and type(limit) is int and 0 <= used <= limit,
            "Invalid continuity usage or limit")
    source = absolute_file(item.get("source_contract_path"), value)
    require(guarded_resolve(source, value) not in visited, "Budget continuity cycle/current run reference")
    require(guarded_sha(source, value) == item.get("source_contract_sha256"), "Continuity contract seal mismatch")
    old = guarded_load(source, value)
    require(old.get("workflow") == value["workflow"] and repo in old.get("repos", [])
            and source.parent.name == old.get("run_id"), "Continuity source run mismatch")
    old = with_read_policy(old, read_policy(value))
    guard_path(source, old)
    source_slice = item.get("source_slice_id")
    require((slice_id is None) == (source_slice is None), "Continuity scope kind mismatch")
    if source_slice is not None:
        state.relative_path(source_slice)
    directory = source.parent / repo
    if source_slice is not None:
        directory = directory / "slices" / source_slice
    events = history(directory, old)
    require(events, "Continuity source history missing")
    source_state = absolute_file(item.get("source_state_path"), old)
    require(guarded_resolve(source_state, old) == guarded_resolve(events[-1][1], old)
            and guarded_sha(source_state, old) == item.get("source_state_sha256"), "Continuity state seal mismatch")
    decision = absolute_file(item.get("decision_path"), value)
    require(guarded_sha(decision, value) == item.get("decision_sha256"), "Continuity decision seal mismatch")
    require(used >= events[-1][2].get("review_fix_count", 0), "Continuity cannot reduce recorded usage")
    if source_slice is None:
        prior = [entry for entry in old.get("budget_continuity", [])
                 if entry.get("repo") == repo and entry.get("slice_id") is None]
        require(len(prior) <= 1, "Duplicate prior continuity")
        if prior:
            require(prior[0].get("work_item") == item["work_item"], "Continuity work item mismatch")
            require(used >= prior[0]["used_passes"] + events[-1][2].get("review_fix_count", 0),
                    "Continuity cannot reduce cumulative usage")
        lineage = gate_lineage if gate_lineage is not None else GateFixLineage(value, repo)
        audited = lineage.audit(source, visited, work_item=item["work_item"],
                                expected=history_seal(item, "source_history_sha256"))
        require(guarded_resolve(audited["state_path"], old) == guarded_resolve(source_state, old)
                and audited["state_sha256"] == item["source_state_sha256"],
                "Continuity state seal mismatch")
        return item
    audited = audit_slice_history(source, repo, source_slice, inherited_policy=read_policy(value),
                                  visited_contracts=visited, cache=slice_cache)
    expected = history_seal(item, "source_history_sha256")
    require(expected is None or expected == audited["history_sha256"], "Slice history SHA-256 mismatch")
    if expected is not None:
        require(guarded_resolve(old.get("repo_paths", {}).get(repo, ""), old)
                == guarded_resolve(value.get("repo_paths", {}).get(repo, ""), old),
                "Slice continuity repository mismatch")
        old_item = old.get("budget_work_items", {}).get(repo)
        require(not old_item or old_item == item["work_item"], "Continuity work item mismatch")
    prior = next((entry for entry in old.get("budget_continuity", [])
                  if entry.get("repo") == repo and entry.get("slice_id") == source_slice), None)
    if prior:
        require(prior["work_item"] == item["work_item"], "Continuity work item mismatch")
        require(used >= prior["used_passes"] + audited["review_fix_count"],
                "Continuity cannot reduce cumulative usage")
    return item


def history_seal(source, key):
    if key not in source:
        return None
    digest = source[key]
    require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest),
            "Invalid gate-fix source history SHA-256")
    return digest


def recovery_continuity(value, repo, item, slice_id, visited, gate_lineage):
    """Reconcile independent, audited repo histories without rewriting their events."""
    require(slice_id is None, "Budget recovery supports repository histories only")
    require(item.get("approved_by") == "user", "Budget recovery requires user approval")
    for key in ("work_item", "user_authorization", "approval_question", "reason"):
        state.text(item.get(key), key)
    timestamp(item.get("approved_at"))
    extra = item.get("additional_passes")
    require(type(extra) is int and extra > 0, "Budget recovery requires positive additional passes")
    sources = item.get("recovery_sources")
    history_seals = continuity_history_seals(item)
    root = value["repo_paths"][repo]
    state.text(item.get("decision_path"), "budget recovery decision path")
    decision = absolute_file(item.get("decision_path"), value)
    require(guarded_sha(decision, value) == item.get("decision_sha256"), "Budget recovery decision seal mismatch")
    approved = guarded_load(decision, value)
    for key in ("work_item", "repo", "approved_by", "user_authorization", "approval_question",
                "approved_at", "reason", "additional_passes", "recovery_sources", "used_passes", "effective_limit"):
        require(approved.get(key) == item.get(key), "Budget recovery decision does not match accounting")
    seen, used, prior_extra = set(), 0, 0
    lineage = gate_lineage
    for source, expected in zip(sources, history_seals):
        state.text(source.get("contract_path"), "budget recovery source path")
        source_path = absolute_file(source.get("contract_path"), value)
        resolved = guarded_resolve(source_path, value)
        require(resolved not in visited and resolved not in seen,
                "Duplicate or cyclic budget recovery source")
        seen.add(resolved)
        require(guarded_sha(source_path, value) == source.get("contract_sha256"), "Budget recovery contract seal mismatch")
        old = guarded_load(source_path, value)
        require(repo in old.get("repos", []) and
                guarded_resolve(old.get("repo_paths", {}).get(repo, ""), value) == guarded_resolve(root, value),
                "Budget recovery repository mismatch")
        require(old.get("budget_work_items", {}).get(repo, item["work_item"]) == item["work_item"],
                "Budget recovery work item mismatch")
        # A previously carried allowance cannot be safely summed with independent histories.
        require(not old.get("budget_continuity"), "Nested continuity requires separate accounting; recovery refused")
        if lineage is not None or expected is not None:
            if lineage is None:
                lineage = GateFixLineage(value, repo)
            audited = lineage.audit(source_path, visited, work_item=item["work_item"], expected=expected)
            require(not lineage.cache[lineage.cache_key(resolved)][1],
                    "Nested historical grants require separate accounting; recovery refused")
        else:
            audited = validate_history(source_path, repo, visited_contracts=visited,
                                       inherited_policy=read_policy(value))
        require(audited["run_id"] == source.get("run_id")
                and audited["state_path"] == source.get("state_path")
                and audited["state_sha256"] == source.get("state_sha256")
                and audited["review_fix_count"] == source.get("used_passes"),
                "Budget recovery source differs from audited history")
        require(not any(grant.get("historical_budget_sources") for grant in audited["grants"]),
                "Nested historical grants require separate accounting; recovery refused")
        used += audited["review_fix_count"]
        prior_extra += audited["granted_additional_passes"]
    limit = REVIEW_FIX_BUDGET + prior_extra + extra
    require(item.get("used_passes") == used and item.get("effective_limit") == limit and used <= limit,
            "Budget recovery totals do not match audited histories and approval")
    return item


def check_related_budgets(value, repo, contract_path):
    """Fail closed when a locally identified related run has not been carried forward."""
    work_item = value.get("budget_work_items", {}).get(repo)
    if not work_item:
        work_item = next((item.get("work_item") for item in value.get("budget_continuity", [])
                          if item.get("repo") == repo and item.get("slice_id") is None), None)
    if not work_item:
        return
    root = Path(contract_path).parent.parent.parent
    current = guarded_resolve(contract_path, value)
    lineage = GateFixLineage(value, repo)
    events = history(Path(contract_path).parent / repo, value)
    if any(event.get("budget_grant_applied") for _, _, event in events):
        validate_history(contract_path, repo, lineage_cache=lineage.cache, lineage_out=lineage.runs)
    else:
        budget_continuity(value, repo, contract_path, gate_lineage=lineage)
    inherited_policies = tuple(policy for (_, _, policies) in lineage.cache for policy in policies)
    value = with_read_policy(value, inherited_policies)
    covered = {snapshot[0] for snapshot in lineage.runs.values()}
    replacement_records = list(value.get("_setup_replacements", []))
    has_replacement = bool(replacement_records)
    for replacement in replacement_records:
        path = absolute_file(replacement["contract_path"], value)
        ancestor = with_read_policy(guarded_load(path, value), read_policy(value))
        value = with_read_policy(value, read_policy(ancestor))
    covered.update(guarded_resolve(item["contract_path"], value) for item in replacement_records)
    for source in list(covered):
        if source in {guarded_resolve(item["contract_path"], value) for item in value.get("_setup_replacements", [])}:
            continue
        ancestor = with_read_policy(guarded_load(absolute_file(source, value), value), read_policy(value))
        replacements = replacement_chain(ancestor, source)
        value = with_read_policy(value, read_policy(ancestor))
        for replacement in replacements:
            path = absolute_file(replacement["contract_path"], value)
            replaced = with_read_policy(guarded_load(path, value), read_policy(value))
            value = with_read_policy(value, read_policy(replaced))
        replacement_records.extend(replacements)
        has_replacement = has_replacement or bool(replacements)
        covered.update(guarded_resolve(item["contract_path"], value) for item in replacements)
    for item in value.get("budget_continuity", []):
        if item.get("repo") != repo or item.get("slice_id") is None:
            continue
        current_slice = item
        inherited = read_policy(value)
        visited = set()
        while current_slice:
            source = absolute_file(current_slice.get("source_contract_path"), {"_read_policy": inherited})
            source_key = (guarded_resolve(source, {"_read_policy": inherited}), current_slice.get("source_slice_id"))
            require(source_key not in visited, "Historical slice reference cycle")
            visited.add(source_key)
            ancestor = with_read_policy(guarded_load(source, {"_read_policy": inherited}), inherited)
            inherited = read_policy(ancestor)
            value = with_read_policy(value, inherited)
            replacements = replacement_chain(ancestor, source)
            replacement_records.extend(replacements)
            has_replacement = has_replacement or bool(replacements)
            covered.update(guarded_resolve(record["contract_path"], value) for record in replacements)
            slice_id = current_slice.get("source_slice_id")
            current_slice = next((entry for entry in ancestor.get("budget_continuity", [])
                                  if entry.get("repo") == repo and entry.get("slice_id") == slice_id), None)
    if has_replacement:
        for item in value.get("budget_continuity", []):
            if item.get("repo") != repo or item.get("slice_id") is None:
                continue
            require(history_seal(item, "source_history_sha256"), "Replacement slice requires a history SHA-256")
            budget_continuity(value, repo, contract_path, item["slice_id"])
            source_item = item
            while source_item:
                source = absolute_file(source_item.get("source_contract_path"), value)
                old = with_read_policy(guarded_load(source, value), read_policy(value))
                value = with_read_policy(value, read_policy(old))
                if not guarded_history(source.parent / repo, old):
                    telemetry = source.parent / "telemetry"
                    for name in guarded_names(telemetry, old):
                        if not name.endswith(".json"):
                            continue
                        event_path = telemetry / name
                        policies = read_policy(old)
                        guarded = state.guard_excluded_paths(event_path.absolute(), policies, pinned=bool(policies))
                        if not policies:
                            guarded = guarded.resolve()
                        with state.capture_open(guarded) as handle:
                            event = json.load(handle)
                        require(not (event.get("repo") in {None, repo} and event.get("slice_id") is None
                                     and event.get("phase") in {"review-fix", "gate-fix"}),
                                "Repo repair telemetry exists without repository history")
                    covered.add(guarded_resolve(source, value))
                old_slice = source_item.get("source_slice_id")
                prior = [entry for entry in old.get("budget_continuity", [])
                         if entry.get("repo") == repo and entry.get("slice_id") == old_slice]
                require(len(prior) <= 1, "Duplicate prior slice continuity")
                source_item = prior[0] if prior else None
    related_paths = []
    for run_group in (root / ".ship-runs", root / ".fix-runs"):
        for name in guarded_names(run_group, value):
            directory = run_group / name
            try:
                names = guarded_names(directory, value)
            except NotADirectoryError:
                continue
            candidate = directory / "contract.json"
            guard_path(candidate, value)
            if "contract.json" not in names:
                continue
            if guarded_resolve(candidate, value) == current:
                continue
            other = guarded_load(candidate, value)
            other_work = dict(other.get("budget_work_items", {}))
            for item in other.get("budget_continuity", []):
                if item.get("slice_id") is None:
                    other_work.setdefault(item["repo"], item["work_item"])
            related = any(identifier == work_item and
                          guarded_resolve(other.get("repo_paths", {}).get(name, ""), value)
                          == guarded_resolve(value["repo_paths"][repo], value)
                          for name, identifier in other_work.items())
            if related:
                require(guarded_resolve(candidate, value) in covered,
                        f"Related run is missing from budget continuity: {candidate}")
                related_paths.append(candidate)
    if has_replacement:
        related_paths.extend(path for path in covered if path != current)
        carried = []
        for replacement in replacement_records:
            old_path = absolute_file(replacement["contract_path"], value)
            old = with_read_policy(guarded_load(old_path, value), read_policy(value))
            declarations = old.get("budget_continuity", [])
            require(isinstance(declarations, list), "Invalid replaced setup continuity")
            for entry in declarations:
                require(isinstance(entry, dict), "Invalid replaced setup continuity entry")
                if entry.get("repo") == repo:
                    budget_continuity(old, repo, old_path, entry.get("slice_id"))
                    carried.append((old, entry))
        slices = check_replacement_slice_coverage(value, repo, contract_path, related_paths, carried)
        repo_sources = {snapshot[0] for snapshot in lineage.runs.values()}
        for old, entry in carried:
            require(entry.get("work_item") == work_item, "Replaced setup continuity work item mismatch")
            if "recovery_sources" in entry:
                sources = entry["recovery_sources"]
                require(isinstance(sources, list) and sources, "Invalid replaced setup recovery sources")
                references = [(source.get("contract_path"), source.get("contract_sha256")) for source in sources]
            else:
                references = [(entry.get("source_contract_path"), entry.get("source_contract_sha256"))]
            for raw, expected in references:
                source = absolute_file(raw, old)
                require(guarded_sha(source, old) == expected, "Replaced setup predecessor contract seal mismatch")
                identity = guarded_resolve(source, old)
                if entry.get("slice_id") is None:
                    require(identity in repo_sources,
                            "Replaced setup predecessor is missing from budget continuity")
                    successor = budget_continuity(value, repo, contract_path)
                else:
                    require((identity, entry.get("source_slice_id")) in slices,
                            "Replaced setup slice predecessor is missing from budget continuity")
                    successor = slices[(identity, entry.get("source_slice_id"))]
                require(successor is not None and successor["used_passes"] >= entry["used_passes"]
                        and successor["effective_limit"] >= entry["effective_limit"],
                        "Replacement cannot reduce carried budget usage or allowance")
        if value.get("setup_replacement") is not None and not value.get("_pending_replacement"):
            pending = reserve_predecessors(value, Path(contract_path), guarded_sha(contract_path, value), value,
                                           lineage_cache=lineage.cache)
            require(not pending, "Predecessor reservation is missing")


def check_replacement_slice_coverage(value, repo, contract_path, related_paths, carried):
    """Require every related started slice to appear in a validated continuity chain."""
    covered = {}
    for item in value.get("budget_continuity", []):
        if item.get("repo") != repo or item.get("slice_id") is None:
            continue
        require(item.get("work_item") == value.get("budget_work_items", {}).get(repo),
                "Replacement slice work item mismatch")
        require(history_seal(item, "source_history_sha256"), "Replacement slice requires a history SHA-256")
        budget_continuity(value, repo, contract_path, item["slice_id"])
        schedule = value.get("slice_schedules", {}).get(repo)
        require(isinstance(schedule, dict), "Replacement slice requires an approved schedule")
        selected_slice(value, repo, item["slice_id"], schedule.get("path"))
        source = absolute_file(item["source_contract_path"], value)
        ancestor = with_read_policy(guarded_load(source, value), read_policy(value))
        require(guarded_resolve(ancestor.get("repo_paths", {}).get(repo, ""), ancestor)
                == guarded_resolve(value["repo_paths"][repo], ancestor), "Slice continuity repository mismatch")
        audited = audit_slice_history(source, repo, item["source_slice_id"], inherited_policy=read_policy(value),
                                      strict=True)
        direct_totals = (item["used_passes"] == audited["historical_used_passes"] + audited["review_fix_count"]
                         and item["effective_limit"] == audited["historical_used_passes"] + audited["review_fix_limit"])
        fields = ("source_contract_sha256", "source_state_sha256", "used_passes", "effective_limit",
                  "approved_by", "user_authorization", "reason", "approved_at", "decision_path", "decision_sha256")
        carried_totals = any(previous.get("slice_id") is not None
                             and guarded_resolve(previous.get("source_contract_path", ""), value) == guarded_resolve(source, value)
                             and previous.get("source_slice_id") == item["source_slice_id"]
                             and all(previous.get(key) == item.get(key) for key in fields)
                             for _, previous in carried)
        require(direct_totals or carried_totals,
                "Slice continuity totals differ from audited history")
        current = item
        inherited = read_policy(value)
        while current:
            source = absolute_file(current.get("source_contract_path"), {"_read_policy": inherited})
            slice_id = state.relative_path(current.get("source_slice_id"))
            key = (guarded_resolve(source, {"_read_policy": inherited}), slice_id)
            require(key not in covered, "Duplicate or cyclic slice history import")
            covered[key] = item
            ancestor = with_read_policy(guarded_load(source, {"_read_policy": inherited}), inherited)
            inherited = read_policy(ancestor)
            prior = [entry for entry in ancestor.get("budget_continuity", [])
                     if entry.get("repo") == repo and entry.get("slice_id") == slice_id]
            require(len(prior) <= 1, "Duplicate prior slice continuity")
            require(not prior or prior[0].get("work_item") == item["work_item"], "Slice ancestor work item mismatch")
            current = prior[0] if prior else None
    for source in related_paths:
        slices = source.parent / repo / "slices"
        for name, mode in guarded_entries(slices, value):
            directory = slices / name
            guard_path(directory, value)
            require(not stat.S_ISLNK(mode), "Slice history directory cannot be a symlink")
            if not stat.S_ISDIR(mode):
                continue
            events = guarded_history(directory, value)
            if events:
                require((guarded_resolve(source, value), directory.name) in covered,
                        f"Related slice history is missing from budget continuity: {directory}")
    return covered


def collect_setup_replacements(value, contract_path, repo, *, lineage=None, lineage_cache=None):
    """Collect replacement links from this contract and audited predecessor contracts."""
    if lineage is None:
        lineage = GateFixLineage(value, repo, lineage_cache)
        events = history(Path(contract_path).parent / repo, value)
        review_budget(events, contract_path, value, repo, gate_lineage=lineage)
    records = list(replacement_chain(value, contract_path))
    seen = {item["closure_path"]: item for item in records}
    sources = {}
    for (source, cached_repo, policy), _ in lineage.cache.items():
        if cached_repo == repo:
            sources.setdefault(source, []).extend(policy)
    for item in value.get("budget_continuity", []):
        if item.get("repo") != repo or item.get("slice_id") is None:
            continue
        current = item
        visited = set()
        inherited = read_policy(value)
        while current:
            source = absolute_file(current.get("source_contract_path"), {"_read_policy": inherited})
            key = (guarded_resolve(source, {"_read_policy": inherited}), current.get("source_slice_id"))
            require(key not in visited, "Historical slice reference cycle")
            visited.add(key)
            old = with_read_policy(guarded_load(source, {"_read_policy": inherited}), inherited)
            inherited = read_policy(old)
            sources.setdefault(guarded_resolve(source, {"_read_policy": inherited}), []).extend(inherited)
            source_slice = current.get("source_slice_id")
            current = next((entry for entry in old.get("budget_continuity", [])
                            if entry.get("repo") == repo and entry.get("slice_id") == source_slice), None)
    for source, inherited in sorted(sources.items()):
        inherited = tuple(inherited)
        old = with_read_policy(guarded_load(absolute_file(source, {"_read_policy": inherited}),
                                            {"_read_policy": inherited}), inherited)
        for item in replacement_chain(old, source):
            previous = seen.setdefault(item["closure_path"], item)
            require(previous == item, "Conflicting setup replacement snapshots")
            if item not in records:
                records.append(item)
    return records


def all_setup_replacements(value, contract_path, *, lineage_cache=None):
    if lineage_cache is None:
        lineage_cache = {}
    records, seen = [], {}
    for repo in value["repos"]:
        for item in collect_setup_replacements(value, contract_path, repo, lineage_cache=lineage_cache):
            previous = seen.setdefault(item["closure_path"], item)
            require(previous == item, "Conflicting setup replacement snapshots")
            if item not in records:
                records.append(item)
    return records


def review_budget(events, contract_path=None, value=None, repo=None, visited_contracts=None, *,
                  gate_lineage=None, certificate=None):
    """Derive repo-level review budget only from grants applied in immutable states."""
    grants, seen_paths, predecessors = [], set(), set()
    active_contracts = set(visited_contracts or ())
    if contract_path:
        active_contracts.add(Path(contract_path).resolve())
    continuity = (budget_continuity(value, repo, contract_path, visited_contracts=active_contracts,
                                    gate_lineage=gate_lineage) if value is not None and contract_path else None)
    base_limit = (continuity["effective_limit"] - continuity["used_passes"]) if continuity else REVIEW_FIX_BUDGET
    carried_over, carried_sources = 0, []
    expect_budget = bool(continuity and any(continuity_history_seals(continuity)))
    certificate_checked = False
    state_hashes = []
    contract_seal = (str(contract_path), sha(contract_path)) if gate_lineage is not None else None
    run_root = Path(events[0][1]).parent.parent.resolve() if events else None
    for index, (_, event_path, event) in enumerate(events):
        grant = event.get("budget_grant")
        require(bool(event.get("budget_grant_applied")) == (grant is not None),
                "Review-budget grant application marker mismatch")
        if grant is not None:
            require(isinstance(grant, dict), "Malformed applied review-budget grant")
            path = absolute_file(grant.get("path"), value)
            require(path.resolve().is_relative_to(run_root), "Review-budget grant must be run-local")
            require(path not in seen_paths, "Review-budget grant already applied")
            seen_paths.add(path)
            data = state.load(path)
            require(isinstance(data, dict) and sha(path) == grant.get("sha256"),
                    "Review-budget grant seal mismatch")
            require(index > 0 and data.get("previous_state_path") == str(events[index - 1][1])
                    and data.get("previous_state_sha256") == sha(events[index - 1][1]),
                    "Review-budget grant predecessor binding mismatch")
            state.text(data.get("reason"), "review-budget grant reason")
            state.text(data.get("user_authorization"), "explicit user authorization")
            state.text(data.get("approval_question"), "approval question")
            approved_at = timestamp(data.get("approved_at"))
            require(timestamp(events[index - 1][2]["at"]) <= approved_at <= timestamp(event["at"]),
                    "Review-budget grant approval time is outside its state boundary")
            require(event.get("budget_grant_applied") is True, "Grant summary lacks application marker")
            purpose = data.get("purpose")
            require(purpose in {"review-fix", "plan-gap"}, "Invalid review-budget grant purpose")
            predecessor_event = events[index - 1][2]
            context_event = (events[index - 2][2] if predecessor_event.get("status") == "capped"
                             and index >= 2 else predecessor_event)
            recovery_path = event.get("slice_repair_recovery_path")
            if recovery_path:
                recovery_hash = sha(absolute_file(recovery_path, value))
                prior_limit = max(0, base_limit + sum(item["additional_passes"] for item in grants)
                                  - carried_over)
                require(predecessor_event.get("status") == "needs-fix" and purpose == "review-fix"
                        and event.get("status") == "edited-unverified" and event.get("action") == "review-fix"
                        and predecessor_event.get("review_fix_count", 0) >= prior_limit
                        and event.get("review_fix_count") == predecessor_event.get("review_fix_count", 0) + 1
                        and data.get("slice_repair_recovery_sha256") == recovery_hash
                        and event.get("artifact_hashes", {}).get(recovery_path) == recovery_hash
                        and not data.get("historical_budget_sources"),
                        "Grant does not bind an exhausted slice repair recovery")
            else:
                require("slice_repair_recovery_sha256" not in data and
                        event.get("status") == context_event.get("status")
                        and event.get("source_fingerprint") == predecessor_event.get("source_fingerprint"),
                        "Grant event changed its review context or source")
            require(data.get("review_path") == context_event.get("review_path")
                    and data.get("review_round") == context_event.get("latest_review_round"),
                    "Grant review context binding mismatch")
            review_path = absolute_file(data.get("review_path"), value)
            require(sha(review_path) == data.get("review_sha256"), "Grant review seal mismatch")
            review_data = state.load(review_path)
            if value is not None:
                bound(value, repo, review_data)
            require(purpose == ("review-fix" if context_event.get("status") == "needs-fix" else "plan-gap"),
                    "Grant purpose does not match bound lifecycle state")
            if purpose == "review-fix":
                ids = sorted(item["id"] for item in review_data.get("findings", []))
                require(ids and data.get("finding_ids") == ids and not data.get("criterion_ids"),
                        "Grant finding scope differs from bound review")
            else:
                criteria = data.get("criterion_ids")
                require(isinstance(criteria, list) and criteria and not data.get("finding_ids"),
                        "Plan-gap grant lacks criterion scope")
                if value is not None:
                    known = {item["id"] for item in value["criteria"] if repo in item["repos"]}
                    require(set(criteria) <= known, "Plan-gap grant references unknown criteria")
                assessment_path, assessment_hash = data.get("assessment_path"), data.get("assessment_sha256")
                require((assessment_path is None) == (assessment_hash is None),
                        "Plan-gap assessment reference needs both path and SHA-256")
                if assessment_path is not None:
                    assessment_file = absolute_file(assessment_path, value)
                    require(assessment_file.resolve().is_relative_to(run_root)
                            and sha(assessment_file) == assessment_hash,
                            "Plan-gap assessment binding mismatch")
            if value is not None:
                require(data.get("schema_version") == VERSION and data.get("run_id") == value["run_id"]
                        and data.get("workflow") == value["workflow"] and data.get("repo") == repo
                        and data.get("contract_sha256") == sha(contract_path),
                        "Review-budget grant run/contract binding mismatch")
            require(data.get("approved_by") == "user", "Review-budget grant lacks user approval")
            state.text(data.get("user_authorization"), "explicit user authorization")
            state.text(data.get("reason"), "review-budget grant reason")
            extra = data.get("additional_passes")
            require(type(extra) is int and extra > 0, "Invalid applied review-budget grant")
            historical_sources = data.get("historical_budget_sources", [])
            if historical_sources:
                require(continuity is None, "Cannot combine continuity and legacy historical sources")
                historical_used, validated_sources = historical_budget_sources(
                    historical_sources, repo,
                    value["workflow"] if value is not None else data["workflow"], contract_path,
                    active_contracts, gate_lineage, value=value)
                require(data.get("historical_used_passes") == historical_used,
                        "Historical budget usage total mismatch")
                require(historical_sources == validated_sources,
                        "Historical budget source snapshot mismatch")
                require(historical_used >= carried_over, "Historical budget carryover cannot shrink")
                carried_over = historical_used
                carried_sources = validated_sources
            predecessor = data.get("previous_state_path")
            require(predecessor not in predecessors, "Multiple grants bound to one predecessor state")
            predecessors.add(predecessor)
            summary = {**grant, "additional_passes": extra,
                       "reason": data.get("reason"), "approved_by": data.get("approved_by"),
                       "approved_at": data.get("approved_at"),
                       "user_authorization": data.get("user_authorization"),
                       "approval_question": data.get("approval_question"),
                       "purpose": data.get("purpose"), "finding_ids": data.get("finding_ids", []),
                       "criterion_ids": data.get("criterion_ids", []),
                       "assessment_path": data.get("assessment_path"),
                       "assessment_sha256": data.get("assessment_sha256")}
            if carried_over:
                summary.update({"historical_used_passes": carried_over,
                                "historical_budget_sources": carried_sources})
            grants.append(summary)
        expected_limit = max(0, base_limit + sum(item["additional_passes"] for item in grants)
                             - carried_over)
        if "review_fix_limit" in event:
            require(event["review_fix_limit"] == expected_limit, "Review-fix limit disagrees with applied grants")
        if "review_fix_grants" in event:
            require(event["review_fix_grants"] == grants,
                    "Review-fix grant history disagrees with applied grants")
        if gate_lineage is not None:
            budget = check_gate_budget_event(events, index, contract_seal, gate_lineage, expect_budget)
            expect_budget = expect_budget or "gate_fix_budget" in event
            if certificate is not None:
                state_hashes.append((event_path.name, sha(event_path)))
                if certificate.get("state_path") == str(event_path):
                    require(event["status"] == "verified"
                            and state.digest(certificate.get("gate_fix_budget")) == state.digest(budget)
                            and certificate.get("gate_fix_history_sha256") ==
                            gate_lineage.history_hash(contract_seal[1], state_hashes),
                            "Completion certificate gate-fix budget/history mismatch")
                    certificate_checked = True
    require(certificate is None or certificate_checked, "Missing gate-fix certificate state")
    return max(0, base_limit + sum(item["additional_passes"] for item in grants)
               - carried_over), grants


def apply_budget_grant(contract_path, value, repo, request, events, previous, data, recovery_path=None, *,
                       gate_lineage=None):
    """Validate a user's one-time run-local grant and return its sealed summary."""
    require(events and (request.get("status") != "edited-unverified" or recovery_path),
            "Grant application needs an existing state")
    path = absolute_file(request.get("budget_grant_path"))
    run_root = Path(contract_path).parent.resolve()
    require(path.resolve().is_relative_to(run_root), "Review-budget grant must be run-local")
    grant = state.load(path)
    bound(value, repo, grant)
    require(grant.get("workflow") == value["workflow"], "Wrong grant workflow")
    require(grant.get("contract_sha256") == sha(contract_path), "Grant contract binding mismatch")
    require(grant.get("approved_by") == "user", "Review-budget grant must be approved by the user")
    state.text(grant.get("reason"), "review-budget grant reason")
    state.text(grant.get("user_authorization"), "explicit user authorization")
    state.text(grant.get("approval_question"), "approval question")
    extra = grant.get("additional_passes")
    require(type(extra) is int and extra > 0, "Grant needs positive integer additional_passes")
    historical_sources = grant.get("historical_budget_sources", [])
    if recovery_path:
        require(grant.get("slice_repair_recovery_sha256") == sha(recovery_path) and not historical_sources,
                "Recovery grant must bind the recovery proof and preserve historical budget sources")
    else:
        require("slice_repair_recovery_sha256" not in grant, "Recovery grant needs a slice repair recovery")
    requested_historical, validated_sources = (historical_budget_sources(
        historical_sources, repo, value["workflow"], contract_path,
        {Path(contract_path).resolve()}, gate_lineage, value=value) if historical_sources else (0, []))
    require(grant.get("historical_used_passes", 0) == requested_historical,
            "Historical budget usage total mismatch")
    require(historical_sources == validated_sources, "Historical budget source snapshot mismatch")
    approved = timestamp(grant.get("approved_at"))
    prior_path = events[-1][1]
    require(grant.get("previous_state_path") == str(prior_path)
            and grant.get("previous_state_sha256") == sha(prior_path), "Grant is bound to another state")
    require(timestamp(previous["at"]) <= approved <= datetime.now(timezone.utc),
            "Grant approval timestamp is stale or in the future")
    current_limit, prior_grants = review_budget(events, contract_path, value, repo)
    continuity = budget_continuity(value, repo, contract_path)
    require(not (continuity and historical_sources), "Cannot combine continuity and legacy historical sources")
    prior_historical = historical_budget_used(prior_grants)
    if historical_sources:
        require(requested_historical >= prior_historical, "Historical budget carryover cannot shrink")
    else:
        requested_historical = prior_historical
    historical_used = max(requested_historical, prior_historical)
    if not historical_sources and prior_historical:
        validated_sources = next(item["historical_budget_sources"] for item in reversed(prior_grants)
                                 if item.get("historical_used_passes", 0))
    prior_grants_used = sum(item["additional_passes"] for item in prior_grants)
    require((previous.get("review_fix_count", 0) >= current_limit if continuity else
             previous.get("review_fix_count", 0) + historical_used >= REVIEW_FIX_BUDGET + prior_grants_used),
            "Cumulative review-fix budget is not exhausted")
    require(all(item.get("previous_state_path") != str(prior_path) for item in prior_grants),
            "A grant was already issued for this state")
    require(recovery_path or data["source_fingerprint"] == previous["source_fingerprint"],
            "Grant cannot accompany source edits")
    for key in ("scope", "scope_path", "source_scope", "source_scope_path", "initial_manifest", "baseline_dir",
                "current_author_agent_id", "original_agent_id", "gate_fix_count", "latest_review_round"):
        require(data.get(key, previous.get(key)) == previous.get(key), f"Grant cannot change {key}")
    require(not {"applied_path", "gate_failure_path", "capsule_handoff", "matrix_path", "assessment_path",
                 "gate_evidence_paths", "pre_review_evidence_paths"} & set(request),
            "Grant application cannot include repair or replacement artifacts")

    before = previous["status"]
    if before == "needs-fix":
        target = "needs-fix"
        require(grant.get("purpose") == "review-fix", "needs-fix continuation requires review-fix grant")
        context = previous
    elif before in {"reviewed", "reviewed-with-findings", "gates-passed", "verified"}:
        target = before
        require(grant.get("purpose") == "plan-gap", "Plan-gap continuation needs a plan-gap grant")
        context = previous
    elif before == "capped":
        require(len(events) >= 2, "Capped state has no resumable context")
        context = events[-2][2]
        target = context.get("status")
        require(target in {"needs-fix", "reviewed", "reviewed-with-findings", "gates-passed", "verified"},
                "Capped state has no resumable review or plan-gap context")
        require(grant.get("purpose") == ("review-fix" if target == "needs-fix" else "plan-gap"),
                "Grant purpose does not match capped state")
    else:
        raise ValueError("Review-budget grant can only continue needs-fix or capped state")
    if recovery_path:
        require(before == "needs-fix", "Recovery grant must start at needs-fix")
        target = "edited-unverified"
    require(request.get("status") == target, "Grant transition must preserve its review context")
    review_path = context.get("review_path")
    require(grant.get("review_path") == review_path and review_path, "Grant review binding mismatch")
    require(grant.get("review_sha256") == sha(review_path), "Grant review seal mismatch")
    require(grant.get("review_round") == context.get("latest_review_round"), "Grant review round mismatch")
    if grant["purpose"] == "review-fix":
        review = state.load(absolute_file(review_path))
        finding_ids = sorted(item["id"] for item in review["findings"])
        require(finding_ids and grant.get("finding_ids") == finding_ids,
                "Grant finding scope differs from open findings")
        require(not grant.get("criterion_ids"), "Review-fix grant cannot claim plan-gap criteria")
    else:
        criterion_ids = grant.get("criterion_ids")
        require(isinstance(criterion_ids, list) and criterion_ids
                and all(item in {c["id"] for c in value["criteria"] if repo in c["repos"]}
                        for item in criterion_ids), "Plan-gap grant needs valid criterion_ids")
        require(not grant.get("finding_ids"), "Plan-gap grant cannot claim review findings")
        assessment_path = grant.get("assessment_path")
        assessment_hash = grant.get("assessment_sha256")
        require((assessment_path is None) == (assessment_hash is None),
                "Plan-gap assessment reference needs both path and SHA-256")
        if assessment_path is not None:
            assessment_file = absolute_file(assessment_path)
            require(assessment_file.resolve().is_relative_to(run_root)
                    and sha(assessment_file) == assessment_hash, "Plan-gap assessment binding mismatch")
            require(context.get("assessment_path") in {None, str(assessment_file)},
                    "Plan-gap assessment differs from current lifecycle context")
    require(all(old.get("budget_grant", {}).get("path") != str(path) for _, _, old in events),
            "Review-budget grant already consumed")
    return target, {"path": str(path), "sha256": sha(path), "additional_passes": extra,
                    "reason": grant["reason"], "approved_by": grant["approved_by"],
                    "approved_at": grant["approved_at"], "user_authorization": grant["user_authorization"],
                    "approval_question": grant["approval_question"], "purpose": grant["purpose"],
                    "finding_ids": grant.get("finding_ids", []),
                    "criterion_ids": grant.get("criterion_ids", []),
                    "assessment_path": grant.get("assessment_path"),
                    "assessment_sha256": grant.get("assessment_sha256"),
                    **({"historical_used_passes": historical_used,
                        "historical_budget_sources": validated_sources} if historical_used else {})}


def repair_phase(contract_path, value, repo, request, events, expected_current_agent=None):
    """Validate recorded repair boundaries, not dispatch order or successful proof."""
    identifier = request.get("repair_phase_id")
    require(isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", identifier)
            and identifier not in {".", ".."}, "Repair needs current request repair_phase_id")
    for included in value["repos"]:
        for _, _, event in history(Path(contract_path).parent / included):
            require(not (event.get("status") == "edited-unverified"
                         and event.get("repair_phase_id") == identifier), "Repair phase already used")
    previous_path, previous = events[-1][1:]
    implementations = [(path, event) for _, path, event in events if event.get("status") == "implemented"]
    require(implementations, "Repair needs original implementation state")
    original_path, original = implementations[0]
    agent = original.get("original_agent_id")
    state.text(agent, "original implementer agent ID")
    require(previous.get("original_agent_id") == agent, "Repair original implementer identity changed")
    # Without rotation the current author is the original; a capsule handoff moves the repair to a new author.
    current = expected_current_agent or previous.get("current_author_agent_id") or agent
    handoff = request.get("capsule_handoff")
    # The start is recorded before the spawn, so it names the predecessor and the capsule; only the end
    # names the new author. Without a handoff both events name the current author.
    start_agent = current
    end_agent = handoff["new_agent_id"] if handoff else current
    if "slice_id" in request:
        # Sliced runs: the fix goes to the author of that slice, whose identity was recorded on implementation.
        start_agent = end_agent = (original.get("slice_authors") or {}).get(request["slice_id"])
        state.text(start_agent, "slice author agent ID")
    directory = Path(contract_path).parent / "telemetry"
    paths = [directory / f"{identifier}-{suffix}.json" for suffix in ("start", "end")]
    start, end = [state.load(absolute_file(p)) for p in paths]
    action = request["action"]
    expected = {"schema_version": VERSION, "run_id": value["run_id"], "workflow": value["workflow"],
                "repo": repo, "phase_id": identifier, "action": action,
                "phase": "gate-fix" if action == "gate-fix" else "review-fix",
                "previous_state_path": str(previous_path), "previous_state_sha256": sha(previous_path)}
    if handoff:
        expected["capsule_path"] = str(absolute_file(handoff["capsule_path"]))
    counter = "attempt" if action == "gate-fix" else "round"
    expected[counter] = (previous.get("gate_fix_count", 0) + 1 if counter == "attempt"
                         else previous.get("latest_review_round", 0))
    for record, event, agent_id in ((start, "phase-start", start_agent), (end, "phase-end", end_agent)):
        binding = {**expected, "agent_id": agent_id}
        require(isinstance(record, dict) and all(record.get(k) == v for k, v in binding.items())
                and record.get("event") == event, "Repair phase binding mismatch")
    require(end.get("start_sha256") == sha(paths[0]), "Repair phase start hash mismatch")
    require(timestamp(previous["at"]) <= timestamp(start.get("at")) <= timestamp(end.get("at"))
            <= datetime.now(timezone.utc), "Repair phase timestamp is stale, future or reversed")
    require(end.get("status") in {"passed", "failed", "blocked", "interrupted"},
            "Repair phase is not closed with an actual result status")
    prior_boundaries = start.get("prior_repair_boundaries", {})
    require(isinstance(prior_boundaries, dict), "Invalid prior repair boundary bindings")
    for path, expected_hash in prior_boundaries.items():
        boundary = absolute_file(path, value)
        require(boundary.parent == directory and guarded_sha(boundary, value) == expected_hash,
                "Prior repair boundary changed")
    return [str(p) for p in paths] + [str(previous_path), str(original_path), *prior_boundaries]


def capsule_handoff(contract_path, value, repo, request, previous, events, current, fingerprint, slice_id=None):
    """Validate a fresh-author handoff from a run-local capsule; returns (sealed paths, new author)."""
    handoff = request["capsule_handoff"]
    require(value.get("author_rotation") == "capsule", "Capsule handoff needs author_rotation: capsule")
    require(isinstance(handoff, dict) and set(handoff) == {"capsule_path", "new_agent_id"},
            "Capsule handoff needs capsule_path and new_agent_id")
    state.text(handoff["new_agent_id"], "new author agent ID")
    require(handoff["new_agent_id"] != current, "Handoff must change the author")
    capsule_file = absolute_file(handoff["capsule_path"])
    require(capsule_file.resolve().is_relative_to(Path(contract_path).parent.resolve()),
            "Capsule must be run-local")
    capsule = state.load(capsule_file)
    bound(value, repo, capsule)
    require(capsule.get("written_by") == current, "Capsule must be written by the current author")
    require(capsule.get("source_fingerprint") == fingerprint, "Capsule is stale for the recorded source")
    if slice_id is not None:
        require(capsule.get("slice_id") == slice_id, "Capsule belongs to another slice")
    require(not any(event.get("capsule_handoff", {}).get("capsule_path") == str(capsule_file)
                    for _, _, event in events), "Capsule already consumed by a handoff")
    manifest_file = absolute_file(capsule["manifest_path"])
    require(state.checked_manifest(manifest_file)["fingerprint"] == fingerprint,
            "Capsule manifest differs from the recorded source")
    return [str(capsule_file), str(manifest_file)], handoff["new_agent_id"]


def phase_binding_recovery(contract_path, value, repo, request, events, final):
    """Recover one malformed fresh-author phase pair without editing immutable telemetry."""
    path = absolute_file(request.get("phase_binding_recovery_path"))
    root = Path(contract_path).parent.resolve()
    require(path.resolve().is_relative_to(root), "Phase-binding recovery must be run-local")
    recovery = state.load(path)
    bound(value, repo, recovery)
    require(recovery.get("kind") == "ship-phase-binding-recovery-v1"
            and recovery.get("reason") == "fresh-author handoff capsule_path was omitted from immutable phase events"
            and isinstance(recovery.get("user_authorization"), str) and recovery["user_authorization"].strip(),
            "Invalid phase-binding recovery declaration")
    require(len(events) >= 3 and events[-1][2].get("status") == "blocked"
            and events[-2][2].get("status") == "needs-fix", "Recovery requires blocked state after needs-fix")
    blocked_path, blocked = events[-1][1], events[-1][2]
    previous_path, previous = events[-2][1], events[-2][2]
    require(recovery.get("blocked_state_path") == str(blocked_path)
            and recovery.get("blocked_state_sha256") == sha(blocked_path)
            and recovery.get("preceding_state_path") == str(previous_path)
            and recovery.get("preceding_state_sha256") == sha(previous_path),
            "Recovery state binding mismatch")
    identifier = recovery.get("phase_id")
    require(isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", identifier)
            and identifier not in {".", ".."} and recovery.get("capsule_path"),
            "Recovery must name the recorded repair phase and capsule")
    start_path = root / "telemetry" / f"{identifier}-start.json"
    end_path = root / "telemetry" / f"{identifier}-end.json"
    start, end = state.load(absolute_file(start_path)), state.load(absolute_file(end_path))
    action = "review-fix"
    expected = {"schema_version": VERSION, "run_id": value["run_id"], "workflow": value["workflow"],
                "repo": repo, "phase_id": identifier, "action": action, "phase": "review-fix",
                "previous_state_path": str(previous_path), "previous_state_sha256": sha(previous_path),
                "round": previous.get("latest_review_round", 0)}
    for record, event, agent in ((start, "phase-start", previous.get("current_author_agent_id")),
                                 (end, "phase-end", recovery.get("new_author_agent_id"))):
        require(isinstance(record, dict) and record.get("event") == event and record.get("agent_id") == agent
                and all(record.get(key) == val for key, val in expected.items()),
                "Recovery phase records differ from immutable observed boundaries")
    require(end.get("status") == "passed" and end.get("start_sha256") == sha(start_path),
            "Recovery requires a completed phase with matching start hash")
    require(timestamp(previous["at"]) <= timestamp(start["at"]) <= timestamp(end["at"])
            <= datetime.now(timezone.utc), "Recovery phase timestamps invalid")
    handoff = {"capsule_path": recovery["capsule_path"], "new_agent_id": recovery["new_author_agent_id"]}
    current = previous.get("current_author_agent_id") or previous.get("original_agent_id")
    capsule_paths, author = capsule_handoff(contract_path, value, repo, {"capsule_handoff": handoff},
                                            previous, events[:-1], current, previous["source_fingerprint"])
    require(author == end.get("agent_id") and final["fingerprint"] == blocked["source_fingerprint"],
            "Recovery source differs from the blocked repair source")
    require(blocked.get("review_path") == previous.get("review_path")
            and blocked.get("review_fingerprint") == previous.get("review_fingerprint"),
            "Blocked state changed review context")
    result_capsule_path = absolute_file(recovery.get("result_capsule_path"))
    result_capsule = state.load(result_capsule_path)
    bound(value, repo, result_capsule)
    require(result_capsule.get("written_by") == author
            and result_capsule.get("source_fingerprint") == final["fingerprint"],
            "Recovery result capsule does not bind the completed repair source")
    result_manifest = absolute_file(result_capsule.get("manifest_path"))
    require(state.checked_manifest(result_manifest)["fingerprint"] == final["fingerprint"],
            "Recovery result capsule manifest differs from repaired source")
    return [str(path), str(start_path), str(end_path), str(blocked_path), str(previous_path),
            *capsule_paths, str(result_capsule_path), str(result_manifest)], handoff


def author_identity_recovery(contract_path, value, repo, request, events, final):
    """Recover author identity from a sealed earlier capsule when lifecycle metadata lagged it."""
    path = absolute_file(request.get("author_identity_recovery_path"))
    root = Path(contract_path).parent.resolve()
    require(path.resolve().is_relative_to(root), "Author-identity recovery must be run-local")
    recovery = state.load(path)
    bound(value, repo, recovery)
    require(recovery.get("kind") == "ship-author-identity-recovery-v1"
            and recovery.get("reason") == "prior capsule recovery did not advance current_author_agent_id"
            and isinstance(recovery.get("user_authorization"), str) and recovery["user_authorization"].strip(),
            "Invalid author-identity recovery declaration")
    require(events and events[-1][2].get("status") == "needs-fix", "Identity recovery requires needs-fix state")
    latest_path, latest = events[-1][1], events[-1][2]
    anchor_path = absolute_file(recovery.get("handoff_state_path"))
    require(anchor_path.resolve().is_relative_to(root), "Identity recovery anchor must be run-local")
    require(recovery.get("handoff_state_sha256") == sha(anchor_path), "Identity recovery anchor hash mismatch")
    anchor_matches = [(state_path, event) for _, state_path, event in events if state_path == anchor_path]
    require(len(anchor_matches) == 1 and anchor_matches[0][1].get("status") == "edited-unverified",
            "Identity recovery handoff state is not in history")
    anchor = anchor_matches[0][1]
    require(recovery.get("latest_state_path") == str(latest_path)
            and recovery.get("latest_state_sha256") == sha(latest_path), "Identity recovery latest state mismatch")
    capsule_handoff = anchor.get("capsule_handoff")
    require(isinstance(capsule_handoff, dict), "Identity recovery anchor has no capsule handoff")
    capsule_path = absolute_file(capsule_handoff.get("capsule_path"))
    require(str(capsule_path) == recovery.get("capsule_path"), "Identity recovery capsule mismatch")
    capsule = state.load(capsule_path)
    bound(value, repo, capsule)
    manifest_path = absolute_file(capsule.get("manifest_path"))
    require(capsule.get("written_by") == anchor.get("original_agent_id")
            and state.checked_manifest(manifest_path)["fingerprint"] == capsule.get("source_fingerprint"),
            "Identity recovery capsule does not prove its author/source binding")
    handoff_phase_id = recovery.get("handoff_phase_id")
    require(isinstance(handoff_phase_id, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", handoff_phase_id)
            and handoff_phase_id not in {".", ".."}, "Invalid identity recovery handoff phase ID")
    handoff_records = [state.load(root / "telemetry" / f"{handoff_phase_id}-{suffix}.json")
                       for suffix in ("start", "end")]
    handoff_previous_path = absolute_file(handoff_records[0].get("previous_state_path"))
    handoff_previous = next((event for _, event_path, event in events if event_path == handoff_previous_path), None)
    require(handoff_records[0].get("event") == "phase-start" and handoff_records[1].get("event") == "phase-end"
            and handoff_records[0].get("agent_id") == capsule.get("written_by")
            and handoff_records[1].get("agent_id") == capsule_handoff.get("new_agent_id")
            and handoff_previous is not None and handoff_previous.get("status") == "needs-fix"
            and capsule.get("source_fingerprint") == handoff_previous.get("source_fingerprint")
            and handoff_records[0].get("previous_state_sha256") == sha(handoff_previous_path)
            and handoff_records[1].get("previous_state_path") == str(handoff_previous_path)
            and handoff_records[1].get("previous_state_sha256") == sha(handoff_previous_path)
            and handoff_records[1].get("start_sha256") == sha(root / "telemetry" / f"{handoff_phase_id}-start.json")
            and handoff_records[1].get("status") == "passed", "Identity recovery handoff phase is invalid")
    author_capsule_path = absolute_file(recovery.get("author_capsule_path"))
    author_capsule = state.load(author_capsule_path)
    bound(value, repo, author_capsule)
    author_manifest = absolute_file(author_capsule.get("manifest_path"))
    require(author_capsule.get("written_by") == capsule_handoff.get("new_agent_id")
            and author_capsule.get("source_fingerprint") == anchor.get("source_fingerprint")
            and state.checked_manifest(author_manifest)["fingerprint"] == anchor.get("source_fingerprint"),
            "Identity recovery author capsule is invalid")
    result_capsule_path = absolute_file(recovery.get("result_capsule_path"))
    result_capsule = state.load(result_capsule_path)
    bound(value, repo, result_capsule)
    result_manifest = absolute_file(result_capsule.get("manifest_path"))
    require(result_capsule.get("written_by") == author_capsule.get("written_by")
            and result_capsule.get("source_fingerprint") == final["fingerprint"]
            and state.checked_manifest(result_manifest)["fingerprint"] == final["fingerprint"],
            "Identity recovery result capsule does not bind the repaired source")
    phase_id = recovery.get("phase_id")
    require(isinstance(phase_id, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", phase_id)
            and phase_id not in {".", ".."}, "Invalid identity recovery phase ID")
    phase_records = [state.load(root / "telemetry" / f"{phase_id}-{suffix}.json") for suffix in ("start", "end")]
    require(all(record.get("phase_id") == phase_id and record.get("agent_id") == author_capsule.get("written_by")
                and record.get("previous_state_path") == str(latest_path)
                and record.get("previous_state_sha256") == sha(latest_path)
                and record.get("action") == "review-fix" and record.get("round") == latest.get("latest_review_round")
                for record in phase_records), "Identity recovery does not match the current repair phase")
    require(phase_records[0].get("event") == "phase-start" and phase_records[1].get("event") == "phase-end"
            and phase_records[1].get("start_sha256") == sha(root / "telemetry" / f"{phase_id}-start.json")
            and phase_records[1].get("status") == "passed", "Identity recovery repair phase is not complete")
    require(request.get("repair_phase_id") == phase_id,
            "Identity recovery must be tied to the edited-unverified repair phase")
    require(recovery.get("current_author_agent_id") == author_capsule.get("written_by")
            and recovery.get("current_author_agent_id") != latest.get("current_author_agent_id"),
            "Identity recovery author does not differ from stale metadata")
    return str(path), recovery["current_author_agent_id"], [str(path), str(anchor_path), str(capsule_path),
                                                             str(manifest_path),
                                                             str(root / "telemetry" / f"{handoff_phase_id}-start.json"),
                                                             str(root / "telemetry" / f"{handoff_phase_id}-end.json"),
                                                             str(author_capsule_path), str(author_manifest), str(result_capsule_path),
                                                             str(result_manifest), *(str(p) for p in (root / "telemetry").glob(f"{phase_id}*.json"))]


def accept_review_findings(contract_path, value, repo, events, request, final, slice_id=None):
    require(len(events) >= 2 and events[-1][2].get("status") == "capped"
            and events[-2][2].get("status") == "needs-fix", "Acceptance requires capped review findings")
    previous, reviewed = events[-1][2], events[-2][2]
    require(previous.get("review_fix_count", 0) >= previous.get("review_fix_limit", 3),
            "Review budget is not exhausted")
    path = absolute_file(request.get("review_acceptance_path"), value)
    require(path.resolve().is_relative_to(Path(contract_path).parent.resolve()), "Acceptance must be run-local")
    decision = state.load(path)
    bound(value, repo, decision)
    require(decision.get("workflow") == value["workflow"] and decision.get("slice_id") == slice_id,
            "Wrong acceptance scope")
    require(decision.get("contract_sha256") == sha(contract_path) and decision.get("approved_by") == "user",
            "Acceptance requires user approval and current contract")
    state.text(decision.get("approval_question"), "approval question")
    state.text(decision.get("user_authorization"), "user authorization")
    require(timestamp(previous["at"]) <= timestamp(decision.get("approved_at")) <= datetime.now(timezone.utc),
            "Invalid acceptance time")
    require(decision.get("previous_state_path") == str(events[-1][1])
            and decision.get("previous_state_sha256") == sha(events[-1][1]), "Wrong acceptance state")
    review_path = absolute_file(reviewed.get("review_path"), value)
    require(decision.get("review_path") == str(review_path) and decision.get("review_sha256") == sha(review_path)
            and decision.get("review_round") == reviewed.get("latest_review_round"), "Wrong acceptance review")
    findings = state.load(review_path)["findings"]
    require(findings and decision.get("finding_ids") == sorted(f["id"] for f in findings),
            "Acceptance finding scope differs")
    require(final["fingerprint"] == previous["source_fingerprint"] == reviewed["review_fingerprint"]
            and decision.get("source_fingerprint") == final["fingerprint"], "Acceptance source changed")
    return str(path)


def transition(contract_path, repo, request_path, out):
    value = contract(contract_path)
    require_unreserved(value, contract_path, repo)
    require(repo in value["repos"], "Unknown transition repo")
    check_related_budgets(value, repo, contract_path)
    directory = Path(contract_path).parent / repo
    events = history(directory)
    previous = events[-1][2] if events else {}
    inherited_seals = {}
    for _, path, event in events:
        bound(value, repo, event)
        check_event_seals(value, repo, event, inherited_seals)
        inherited_seals = event.get("artifact_hashes", {})
    lineage = GateFixLineage(value, repo)
    budget_limit, applied_grants = review_budget(events, contract_path, value, repo, gate_lineage=lineage)
    request = state.load(absolute_file(request_path))
    require(isinstance(request, dict), "Transition request must be an object")
    protected = {"review_fingerprint", "artifact_hashes", "initial_manifest", "code_only", "schema_version",
                 "run_id", "repo", "sequence", "at", "current_author_agent_id", "review_fix_limit",
                 "review_fix_grants", "budget_grant", "amendment_sha256", "amendment_applied",
                 "budget_grant_applied", "gate_fix_budget", "gate_fix_sources_sha256"}
    require(not protected & set(request), "Request overrides certified lifecycle fields")
    status = request.get("status")
    if has_bound_pre_review(value, repo):
        require("pre_review_evidence_paths" not in request or status == "pre-review-passed",
                "Pre-review evidence can change only before review")
        require("slice_gate_path" not in request or status in {"implemented", "pre-review-passed", "edited-unverified"},
                "Slice gate cannot change after pre-review")
    require("original_agent_id" not in request
            or (status == "implemented" and "original_agent_id" not in previous),
            "Request overrides original implementer agent ID")
    require("repair_phase_id" not in request or status == "edited-unverified",
            "Repair phase input requires edited-unverified")
    require("capsule_handoff" not in request or status == "edited-unverified",
            "Capsule handoff requires edited-unverified")
    require("phase_binding_recovery_path" not in request or status == "edited-unverified",
            "Phase-binding recovery requires edited-unverified")
    require("author_identity_recovery_path" not in request or status == "edited-unverified",
            "Author-identity recovery requires edited-unverified")
    policy = value.get("telemetry_policy", {}).get("repair_phase_required", False)
    if status not in {"needs-fix", "reviewed"}:
        require("review_path" not in request, "Review input is only accepted by a review transition")
    if status != "gates-passed":
        require("gate_evidence_paths" not in request, "Gate evidence requires gates-passed transition")
    if status != "verified":
        require("assessment_path" not in request, "Assessment requires verified transition")
    if "deliverable_manifest_path" in request:
        require(status == "pre-review-passed", "Deliverable manifest can change only before review")
    before = previous.get("status")
    binding_recheck = status == "edited-unverified" and request.get("action") == "binding-recheck"
    if binding_recheck:
        require(before in {"pre-review-passed", "reviewed", "gates-passed", "verified"}
                and not previous.get("review_acceptance_path")
                and set(request) <= {"status", "action", "source_manifest", "scope_path", "baseline_dir"}
                and all(request.get(key, previous.get(key)) == previous.get(key)
                        for key in ("scope_path", "baseline_dir"))
                and stale_binding(value, repo, previous), "Binding recheck needs a stale binding and a clean boundary")
    sliced = repo in value.get("slice_schedules", {})
    recovery_path = request.get("slice_repair_recovery_path")
    if "slice_repair_recovery_path" in request:
        require(recovery_path and sliced and before == "needs-fix" and status == "edited-unverified"
                and request.get("action") == "review-fix" and not value.get("from_review"),
                "Slice repair recovery requires needs-fix -> edited-unverified for a sliced repo")
        require(set(request) <= {"status", "action", "source_manifest", "slice_repair_recovery_path",
                                 "budget_grant_path"},
                "Slice repair recovery cannot override lifecycle fields or include a repair phase")
        validate_history(contract_path, repo)
    source_anchor = next((event for _, _, event in reversed(events)
                          if event["status"] in {"implemented", "edited-unverified"}), None)
    recovery_anchor = source_anchor if source_anchor and source_anchor.get("slice_repair_recovery_path") else None
    requirement_amendment_path = request.get("amendment_path")
    is_requirement_amendment = ("amendment_path" in request
                                and requirement_amendment_path != previous.get("amendment_path"))
    if "amendment_path" in request:
        require(before == status and status in {"ready", "blocked"},
                "Amendment registration requires an unchanged ready or blocked state")
        require(is_requirement_amendment, "Amendment registration must present a new amendment_path")
        require(set(request) <= {"status", "source_manifest", "scope_path", "source_scope_path", "baseline_dir",
                                 "preflight_path", "amendment_path", "reason"},
                "Amendment cannot change lifecycle fields")
        for field in ("scope_path", "source_scope_path", "baseline_dir", "preflight_path"):
            require(request.get(field, previous.get(field)) == previous.get(field),
                    "Amendment cannot change " + field)
        applied = requirement_amendment(value, contract_path, requirement_amendment_path, events[-1][1])
        change = applied["changes"][0]
        item = next(c for c in value["criteria"] if c["id"] == change["criterion_id"])
        item["requirement"] = change["new_requirement"]
        item["verification"] = change["new_verification"]
        if "new_environment" in change:
            item["environment"] = change["new_environment"]
        if "new_data_scope" in change:
            item["data_scope"] = change["new_data_scope"]
        value.setdefault("_amendments", []).append({"path": requirement_amendment_path,
                                                    "sha256": sha(requirement_amendment_path),
                                                    "criterion_id": change["criterion_id"]})
        value["_amendment"] = value["_amendments"][-1]
    grant_path = request.get("budget_grant_path")
    identity_correction = request.get("author_identity_recovery_path") is not None
    is_grant_transition = grant_path is not None
    valid_grant_transition = (is_grant_transition and (
        (before in {"needs-fix", "reviewed", "reviewed-with-findings", "gates-passed", "verified"} and status == before) or
        (before == "capped" and len(events) >= 2 and status == events[-2][2].get("status"))))
    gate_correction = before == "gates-passed" and status == "gates-passed" and not is_grant_transition
    if gate_correction:
        state.text(request.get("gate_correction_reason"), "gate correction reason")
        require(request.get("gate_evidence_paths") and
                request["gate_evidence_paths"] != previous.get("gate_evidence_paths"),
                "Gate correction needs replacement evidence")
        require(not {"matrix_path", "assessment_path", "pre_review_evidence_paths"} & set(request),
                "Gate correction cannot replace other evidence")
    else:
        require("gate_correction_reason" not in request, "Gate correction reason requires gate correction")
    accepting = before == "capped" and status == "reviewed-with-findings"
    require("review_acceptance_path" not in request or accepting, "Unexpected acceptance path")
    require(gate_correction or valid_grant_transition or accepting or is_requirement_amendment or identity_correction or binding_recheck
            or status in TERMINAL
            or status in TRANSITIONS.get(before, set()),
            f"Illegal transition: {before} -> {status}")
    require(not identity_correction or before == "needs-fix" and not is_grant_transition
            and request.get("action") == "review-fix"
            and not {"review_path", "capsule_handoff", "gate_evidence_paths", "assessment_path"} & set(request),
            "Author-identity recovery cannot modify review, source, or grant context")
    require(before != "capped" or valid_grant_transition or accepting,
            "Exhausted run needs an explicit user-approved budget grant")
    require(not is_grant_transition or status not in TERMINAL,
            "Grant application must resume a concrete review/plan-gap state")
    require(before is not None or status == "initialized", "Run must be initialized first")
    if before == "ready" and status == "pre-review-passed":
        require(value.get("from_review") is True, "Implementation must precede pre-review")
    expected_out = directory / f"state-{len(events) + 1}.json"
    require(Path(out).resolve() == expected_out.resolve(), "Transition output must be next immutable state")
    scope_path = request.get("scope_path", previous.get("scope_path"))
    scope = scope_input(scope_path, value)
    source_path = request.get("source_manifest")
    exclusions = previous.get("excludes", request.get("excludes", []))
    dependency_scope_path = request.get("source_scope_path", previous.get("source_scope_path", scope_path))
    dependency_scope = scope_input(dependency_scope_path, value)
    require(set(previous.get("scope", [])) <= set(scope), "Touched scope must not shrink")
    final = fresh(value, repo, source_path, sorted(set(scope) | set(dependency_scope)), exclusions)
    if binding_recheck:
        require(scope == previous["scope"] and dependency_scope == previous["source_scope"]
                and state.same_source(final, event_manifest(value, repo, previous), repo_read_excludes(value, repo)),
                "Binding recheck cannot change repository source")
    if sliced and before == "needs-fix" and status in {"reviewed", "capped"}:
        previous_source = event_manifest(value, repo, previous)
        require(state.same_source(final, previous_source, value.get("read_excludes", {}).get(repo, [])),
                "Repo source changed since needs-fix. Record the repair or use slice repair recovery before continuing")
    if accepting:
        require(set(request) <= {"status", "source_manifest", "scope_path", "baseline_dir", "review_acceptance_path"},
                "Acceptance cannot change lifecycle context")
        require(scope == previous["scope"] and request.get("baseline_dir", previous.get("baseline_dir")) == previous.get("baseline_dir"),
                "Acceptance cannot change scope or baseline")
        acceptance_path = accept_review_findings(contract_path, value, repo, events, request, final)
    initial_path = previous.get("initial_manifest", source_path)
    initial = manifest(value, repo, initial_path)
    names = source_names(value, repo)
    if previous.get("scope_amendment_path"):
        require(request.get("scope_amendment_path", previous["scope_amendment_path"]) ==
                previous["scope_amendment_path"], "Cannot remove or replace scope amendment")
    amendment_path = request.get("scope_amendment_path", previous.get("scope_amendment_path"))
    amendment_seals = []
    if amendment_path:
        require(repo in value.get("slice_schedules", {}), "Scope amendment requires sliced repo")
        if previous.get("scope_amendment_path"):
            require(amendment_path == previous["scope_amendment_path"], "Cannot replace scope amendment")
        additions, amendment_seals = scope_amendment(contract_path, value, repo, amendment_path, events, initial)
        anchor = state.load(state.load(amendment_path)["previous_state_path"])
        require(set(scope) == set(anchor["scope"]) | set(additions), "Scope differs from approved amendment")
        require(dependency_scope == anchor["source_scope"] and exclusions == anchor["excludes"],
                "Scope amendment cannot change dependencies or exclusions")
        if not previous.get("scope_amendment_path"):
            require(events[-1][2]["status"] == "reviewed" and status == "edited-unverified" and
                    request.get("action") == "plan-gap", "Scope amendment requires reviewed plan-gap repair")
            require(str(events[-1][1]) == state.load(amendment_path)["previous_state_path"],
                    "Scope amendment anchor is not current")
            before_amendment = manifest(value, repo, anchor["source_manifest"])
            delta_paths = names.changed(before_amendment, final)
            require(delta_paths <= names.keys(additions), "Scope amendment changed unapproved source")
            gate_path = absolute_file(request.get("slice_gate_path"))
            gate = state.load(gate_path)
            bound(value, repo, gate)
            check_seals(gate["artifact_hashes"])
            require(gate.get("source_fingerprint") == final["fingerprint"] and
                    gate.get("scope_amendment_path") == str(amendment_path),
                    "Scope amendment needs its current slice gate")
            amendment_seals.append(str(gate_path))
    elif repo in value.get("slice_schedules", {}) and before in {"implemented", "pre-review-passed", "needs-fix", "reviewed", "gates-passed", "verified"}:
        require(set(scope) == set(previous["scope"]), "Sliced repo scope expansion requires amendment")
    changed = names.changed(initial, final)
    if not recovery_path:
        require(changed <= names.keys(scope), "Changes outside touched scope; resolve ownership first")
    if policy or recovery_anchor:
        certification = {"pre-review-passed", "needs-fix", "reviewed", "gates-passed", "verified"}
        if status == "implemented":
            require(not source_anchor and not any(event["status"] in certification for _, _, event in events),
                    "Source repairs cannot restart implementation; record edited-unverified")
        if status in certification:
            # A blocker/handoff may truthfully capture partially edited source, but must not
            # silently make that source the accepted implementation on unblock. Only initial
            # implementation or an explicitly recorded repair can advance this version anchor.
            anchor = event_manifest(value, repo, source_anchor) if source_anchor else initial
            require(state.same_source(final, anchor, names.read_excludes),
                    "Source changed without recorded repair; record edited-unverified with repair_phase_id "
                    "(or explicit from-review author_handoff)")
    sealed = [contract_path, source_path, request_path, *amendment_seals]
    if accepting:
        sealed.append(acceptance_path)
    for applied in value.get("_amendments", []):
        sealed.append(applied["path"])
    if events:
        sealed.append(str(events[-1][1]))
    if value["workflow"] == "ship":
        sealed.append(value["approved_snapshot"])
    data = {**previous, **request, "schema_version": VERSION, "run_id": value["run_id"],
            "repo": repo, "sequence": len(events) + 1, "at": datetime.now(timezone.utc).isoformat(),
            "initial_manifest": initial_path, "scope_path": scope_path, "scope": scope,
            "source_scope_path": dependency_scope_path, "source_scope": dependency_scope,
            "excludes": exclusions, "source_fingerprint": final["fingerprint"]}
    data.pop("amendment_applied", None)
    if is_requirement_amendment:
        require(final["fingerprint"] == previous["source_fingerprint"] and scope == previous["scope"],
                "Amendment cannot change source or scope")
        data["amendment_path"] = requirement_amendment_path
        data["amendment_sha256"] = sha(requirement_amendment_path)
        data["amendment_applied"] = True
    data.pop("budget_grant", None)
    data.pop("budget_grant_applied", None)
    data.pop("slice_repair_recovery_path", None)
    if recovery_path:
        require(scope == previous["scope"] and dependency_scope == previous["source_scope"],
                "Slice repair recovery requires the recorded repo and dependency scope")
        references, recovery_seals = slice_repair_recovery(
            contract_path, value, repo, events, recovery_path, source_path, final)
        require(changed <= names.keys(scope), "Changes outside touched scope; resolve ownership first")
        data["slice_repair_recovery_path"] = recovery_path
        data["slice_gate_path"] = references["slice_gate"]
        data["applied_path"] = references["applied"]
        sealed += recovery_seals
    if is_grant_transition:
        target, grant_summary = apply_budget_grant(
            contract_path, value, repo, request, events, previous, data, recovery_path, gate_lineage=lineage)
        require(status == target, "Grant transition status changed during validation")
        continuity = budget_continuity(value, repo, contract_path)
        base_limit = (continuity["effective_limit"] - continuity["used_passes"]) if continuity else REVIEW_FIX_BUDGET
        budget_limit = max(0, base_limit
                           + sum(item["additional_passes"] for item in applied_grants)
                           + grant_summary["additional_passes"]
                           - grant_summary.get("historical_used_passes", 0))
        applied_grants = [*applied_grants, grant_summary]
        data["budget_grant"] = grant_summary
        data["budget_grant_applied"] = True
        sealed.append(grant_path)
    data["review_fix_limit"] = budget_limit
    data["review_fix_grants"] = applied_grants
    data.pop("budget_grant_path", None)
    # Counters are derived from history, never accepted from caller input.
    # Set only once an implementer exists; earlier events must not carry a null author.
    current_author = previous.get("current_author_agent_id") or data.get("original_agent_id")
    if current_author:
        data["current_author_agent_id"] = current_author
    for key in ("review_fix_count", "gate_fix_count", "latest_review_round"):
        data[key] = previous.get(key, 0)
    if changed and not value.get("from_review"):
        baseline_dir = data.get("baseline_dir")
        require(isinstance(baseline_dir, str) and Path(baseline_dir).is_absolute(), "Missing first-touch baseline directory")
        delta = state.delta(value["repo_paths"][repo], baseline_dir, scope)
        initial_files = names.files(initial["files"])
        for entry in delta["changes"]:
            require(entry["before"] == initial_files.get(names.key(entry["path"]), state.MISSING),
                    "First-touch baseline differs from run start; resolve concurrent edits")
    if status == "initialized":
        require(not events and not changed, "Initialization must precede implementation")
        validate_store_roots(value)
        if sliced and has_bound_pre_review(value, repo):
            checked_slice_schedule(value, repo, value["slice_schedules"][repo]["path"])
    if status in {"ready", "preflight-blocked"}:
        require(state.same_source(final, initial, names.read_excludes), "Preflight must precede source edits")
        pre, paths = preflight(value, repo, request.get("preflight_path"), initial)
        require((pre["status"] == "passed") == (status == "ready"), "Wrong preflight transition")
        sealed += paths
    if sliced and status in {"implemented", "pre-review-passed"}:
        gate_path = request.get("slice_gate_path", previous.get("slice_gate_path"))
        gate = state.load(absolute_file(gate_path))
        bound(value, repo, gate)
        require(gate.get("source_fingerprint") == final["fingerprint"],
                "Slice gate is stale; re-run slice-gate after any slice edit")
        check_seals(gate["artifact_hashes"])
        if value.get("_amendment"):
            require(gate.get("amendment_sha256") == value["_amendment"]["sha256"]
                    and gate.get("artifact_hashes", {}).get(value["_amendment"]["path"])
                    == value["_amendment"]["sha256"], "Slice gate predates amendment")
        sealed.append(gate_path)
        data["slice_gate_path"] = gate_path
        if status == "implemented":
            require(request.get("slice_authors") == gate["slice_authors"],
                    "slice_authors must equal the authors recorded by the accepted slices")
            sealed += check_slice_delivery(value, repo, gate_path)
        if recovery_anchor and status == "pre-review-passed":
            if gate_path != recovery_anchor["slice_gate_path"]:
                schedule = value["slice_schedules"][repo]["path"]
                expected_gate = prepare_slice_gate(
                    contract_path, repo, schedule, initial_path, source_path, amendment_path)
                require(gate == expected_gate, "Replacement slice gate must certify current acceptance")
            for identifier, expected in gate["slices"].items():
                slice_events = history(directory / "slices" / identifier)
                require(slice_events and slice_events[-1][2]["status"] == "accepted"
                        and sha(slice_events[-1][1]) == expected,
                        f"Slice {identifier} changed after the recovery gate. Run slice-gate again")
    if status == "implemented":
        if policy and not value.get("from_review"):
            state.text(request.get("original_agent_id"), "original implementer agent ID")
        if before == "preflight-blocked":
            auth = state.load(absolute_file(request.get("code_only_authorization")))
            bound(value, repo, auth)
            state.text(auth.get("user_authorization"), "explicit code-only authorization")
            data["code_only"] = True
            sealed.append(request["code_only_authorization"])
        sealed += matrix(value, repo, request.get("matrix_path"), final)
    if binding_recheck:
        for key in ("deliverable_manifest_path", "pre_review_evidence_paths", "review_fingerprint",
                    "gate_evidence_paths", "assessment_path"):
            data.pop(key, None)
    elif status == "edited-unverified":
        data.pop("review_acceptance_path", None)
        action = request.get("action")
        require(action in EDITS - {"implement"}, "Missing edit cause")
        if value.get("from_review"):
            require(action == "plan-gap" and request.get("author_handoff"), "From-review fixes need author handoff")
        elif policy and not recovery_path:
            if request.get("phase_binding_recovery_path"):
                require(before == "blocked" and action == "review-fix",
                        "Phase-binding recovery only resumes a blocked review-fix")
                recovery_paths, recovered_handoff = phase_binding_recovery(
                    contract_path, value, repo, request, events, final)
                require(request.get("capsule_handoff") == recovered_handoff,
                        "Recovery capsule handoff differs from the recorded author handoff")
                sealed += recovery_paths
                data["current_author_agent_id"] = recovered_handoff["new_agent_id"]
            elif identity_correction:
                recovery_path, recovered_author, recovery_seals = author_identity_recovery(
                    contract_path, value, repo, request, events, final)
                sealed += recovery_seals
                data["current_author_agent_id"] = recovered_author
                data["author_identity_recovery_path"] = recovery_path
                sealed += repair_phase(contract_path, value, repo, request, events,
                                       expected_current_agent=recovered_author)
            else:
                sealed += repair_phase(contract_path, value, repo, request, events)
        if request.get("capsule_handoff") is not None and not request.get("phase_binding_recovery_path"):
            require(policy and not value.get("from_review"), "Capsule handoff needs a policy-enabled repair phase")
            current = previous.get("current_author_agent_id") or previous.get("original_agent_id")
            paths, new_author = capsule_handoff(contract_path, value, repo, request, previous, events, current,
                                                previous["source_fingerprint"])
            sealed += paths
            data["current_author_agent_id"] = new_author
        if action == "review-fix":
            recovered = bool(request.get("phase_binding_recovery_path"))
            require((before == "needs-fix" or recovered and before == "blocked")
                    and previous.get("review_path"), "Review fix needs findings")
            require(data["review_fix_count"] < budget_limit,
                    "Review-fix budget exhausted. Bind a user-approved grant to the recovery"
                    if recovery_path else "Review-fix budget exhausted")
            data["review_fix_count"] += 1
            if not recovery_path:
                _, proofs = applied_findings(value, repo, request.get("applied_path"), previous["review_path"], final)
                sealed += proofs
        if action == "plan-gap":
            require(data["review_fix_count"] < budget_limit, "Plan-gap repair budget exhausted")
            data["review_fix_count"] += 1
        if action == "gate-fix":
            require(lineage.summary(data["gate_fix_count"])["remaining_attempts"] > 0, "Gate-fix budget exhausted")
            sealed += gate_failure_proof(value, repo, request.get("gate_failure_path"), previous)
            data["gate_fix_count"] += 1
        for key in ("gate_evidence_paths", "assessment_path"):
            data.pop(key, None)
        if recovery_path:
            for key in ("pre_review_evidence_paths", "repair_phase_id", "slice_id", "capsule_handoff",
                        "phase_binding_recovery_path", "author_identity_recovery_path"):
                data.pop(key, None)
    delivery = None
    if status == "pre-review-passed":
        check_paths = request.get("pre_review_evidence_paths")
        if recovery_anchor and isinstance(check_paths, list):
            require(not {sha(absolute_file(path)) for path in check_paths} & set(inherited_seals.values()),
                    "Slice repair recovery requires fresh repo pre-review check evidence")
        sealed += matrix(value, repo, request.get("matrix_path", previous.get("matrix_path")), final)
        delivery, paths = deliverable_binding(value, repo, data.get("deliverable_manifest_path"))
        sealed += paths
        sealed += check_slice_delivery(value, repo, data.get("slice_gate_path"), data.get("deliverable_manifest_path"))
        sealed += check_set(value, repo, "pre_review_checks", check_paths, final, delivery=delivery)
    if status in {"needs-fix", "reviewed"} and not is_grant_transition and not identity_correction:
        if recovery_anchor:
            require(sha(absolute_file(request.get("review_path"))) not in inherited_seals.values(),
                    "Slice repair recovery needs a fresh independent repo review")
        delivery, paths = deliverable_binding(value, repo, previous.get("deliverable_manifest_path"))
        sealed += paths + recheck_pre_review(value, repo, previous, final, delivery)
        sealed += check_slice_delivery(value, repo, data.get("slice_gate_path"), data.get("deliverable_manifest_path"))
        prior_paths = [e[2]["review_path"] for e in events
                       if e[2]["status"] in {"needs-fix", "reviewed"} and not e[2].get("budget_grant_applied")]
        result, paths = review(value, repo, request.get("review_path"), final, scope, prior_paths,
                               Path(contract_path).parent.resolve(), previous.get("artifact_hashes", {}))
        if delivery is not None:
            match_delivery_reference(result, delivery)
        require(result["round"] == data["latest_review_round"] + 1, "Review rounds must increase by one")
        require(bool(result["findings"]) == (status == "needs-fix"), "Findings/status mismatch")
        data["latest_review_round"] = result["round"]
        data["review_fingerprint"] = final["fingerprint"]
        sealed += paths
    if status in {"gates-passed", "verified"} or (accepting and has_bound_pre_review(value, repo)):
        delivery, paths = deliverable_binding(value, repo, previous.get("deliverable_manifest_path"))
        sealed += paths + recheck_pre_review(value, repo, previous, final, delivery)
        sealed += check_slice_delivery(value, repo, data.get("slice_gate_path"), data.get("deliverable_manifest_path"))
        require(review_source_matches(value, repo, previous, events, final), "Review is stale")
        accepted_review = state.load(absolute_file(data["review_path"], value))
        if delivery is not None:
            match_delivery_reference(accepted_review, delivery)
        require(not accepted_review["findings"] or data.get("review_acceptance_path"), "Review has open findings")
        if not accepting:
            sealed += check_set(value, repo, "required_gates", data.get("gate_evidence_paths"), final, delivery=delivery)
    if status == "verified":
        require(not data.get("code_only"), "Code-only authorization cannot certify completion")
        sealed += assessment(value, repo, request.get("assessment_path", data.get("assessment_path")), final, initial,
                             delivery=delivery)
    if status in TERMINAL:
        state.text(request.get("reason"), "blocker/handoff reason")
    data["gate_fix_budget"] = lineage.summary(data["gate_fix_count"])
    data["gate_fix_sources_sha256"] = lineage.source_hash()
    data["artifact_hashes"] = extend_seals(value, previous, sealed, delivery)
    if recovery_path:
        fresh(value, repo, source_path, sorted(set(scope) | set(dependency_scope)), exclusions)
    # Scope is intentionally mutable; the event captures its values, not a mutable-file seal.
    check_gate_sources_current(lineage.cache)
    state.write_once(out, data)
    return data


def require_run_local(value, root, paths):
    """A certificate must not seal mutable documents (stage status/handoff notes edited after completion)."""
    run_dir = Path(root).resolve()
    repo_roots = [Path(p).resolve() for p in value["repo_paths"].values()]
    for path in paths:
        resolved = Path(path).resolve()
        require(resolved.is_relative_to(run_dir) or any(resolved.is_relative_to(r) for r in repo_roots),
                f"Certificate seals a document outside the run directory: {path}; "
                "seal a run-local immutable snapshot instead")


SLICE_TRANSITIONS = {
    None: {"implemented"},
    "implemented": {"pre-review-passed"},
    "edited-unverified": {"pre-review-passed"},
    "pre-review-passed": {"needs-fix", "reviewed"},
    "needs-fix": {"edited-unverified", "reviewed"},
    "reviewed-with-findings": {"accepted", "edited-unverified"},
    "reviewed": {"accepted", "edited-unverified"},
    "accepted": {"edited-unverified"},
}
REVIEW_FIX_BUDGET = 3
GATE_FIX_BUDGET = 2
SLICE_REVIEW_FIX_BUDGET = 3


def glob_regex(pattern):
    """Translate a path glob: **/ matches zero or more segments, ** matches any remainder."""
    out, index = [], 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append(r"(?:[^/]+/)*")
            index += 3
        elif pattern.startswith("**", index):
            out.append(r".*")
            index += 2
        elif pattern[index] == "*":
            out.append(r"[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append(r"[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return "".join(out)


def in_scope(path, patterns, names=None):
    for pattern in patterns:
        if not re.search(r"[*?]", pattern):
            literal = pattern.rstrip("/")
            if (path == literal or path.startswith(literal + "/")
                    or names is not None and names.intersects(path, [literal])):
                return True
            continue
        if re.fullmatch(glob_regex(pattern), path):
            return True
    return False


def slice_matrix(value, repo, item, path, final):
    record = state.load(absolute_file(path, value))
    bound(value, repo, record)
    entries = record.get("rows")
    require(isinstance(entries, list), "Missing slice matrix rows")
    wanted = {(c["criterion_id"], c["part"]) for c in item["contributions"]}
    seen = {(e.get("criterion_id"), e.get("part")) for e in entries}
    require(len(seen) == len(entries) and wanted <= seen, "Slice matrix must cover every slice contribution once")
    sealed = [path]
    for entry in entries:
        require(entry.get("status") in {"verified", "unverified"}, "Invalid slice matrix status")
        for field in ("assertion", "reason"):
            state.text(entry.get(field), "slice matrix " + field)
        require(isinstance(entry.get("evidence_paths"), list), "Missing slice matrix evidence list")
        if entry["status"] == "verified":
            require(entry["evidence_paths"], "Verified slice row lacks proof")
        for evidence_path in entry["evidence_paths"]:
            evidence_status = state.load(absolute_file(evidence_path)).get("status")
            if entry["status"] == "verified":
                evidence_status = "passed"
            require(evidence_status in {"passed", "failed", "unverified"}, "Invalid evidence status")
            sealed += proof(value, repo, evidence_path, final, entry["criterion_id"], expected_status=evidence_status)
    return sealed


def integration_resume(value, repo, run, authorization, start, events, slice_id):
    """A human-bound partial-slice repair may resume a paused operation without rewriting it."""
    path = authorization.get("resume_authorization_path")
    if not path:
        return None, []
    path = absolute_file(path, value)
    require(path.resolve().is_relative_to(run), "Integration resume authorization must be run-local")
    require(authorization.get("resume_authorization_sha256") == sha(path), "Integration resume authorization changed")
    resume = state.load(path)
    bound(value, repo, resume)
    require(resume.get("slice_id") == slice_id, "Wrong integration resume slice")
    state.text(resume.get("user_authorization"), "human integration resume authorization")
    paused_path = absolute_file(resume.get("paused_state_path"), value)
    require(resume.get("paused_state_sha256") == sha(paused_path), "Wrong paused integration state")
    found = [event for _, p, event in events if str(p) == str(paused_path)]
    require(len(found) == 1 and found[0]["status"] == "edited-unverified"
            and found[0].get("action") == "integration-fix", "Resume needs a paused integration repair")
    paused = found[0]
    require(resume.get("author_agent_id") == paused["author_agent_id"], "Wrong integration resume author")
    prior_path = absolute_file(resume.get("superseded_checkpoint_path"), value)
    require(paused.get("integration_path") == str(prior_path)
            and resume.get("superseded_checkpoint_sha256") == sha(prior_path), "Wrong superseded checkpoint")
    prior = state.load(prior_path)
    prior_auth_path = absolute_file(prior.get("authorization_path"), value)
    require(prior.get("authorization_sha256") == sha(prior_auth_path), "Superseded authorization changed")
    prior_auth = state.load(prior_auth_path)
    require(all(authorization.get(key) == prior_auth.get(key) for key in
                ("starting_slices", "edit_paths", "recertify_slices", "partial_slices",
                 "contract_sha256", "schedule_sha256")), "Resume cannot change alignment permissions")
    require(resume.get("before_manifest") == paused["source_manifest"]
            and resume.get("before_manifest_sha256") == sha(guard_path(paused["source_manifest"], value)),
            "Resume must bind the paused source manifest")
    before = manifest(value, repo, paused["source_manifest"])
    permitted = resume.get("partial_repair_paths")
    require(isinstance(permitted, dict) and permitted, "Resume needs explicit partial repair paths")
    paths = set()
    for owner, names in permitted.items():
        require(owner != slice_id and not history(run / repo / "slices" / state.relative_path(owner), value),
                "Resume repair must belong to an unsubmitted partial slice")
        require(isinstance(names, list) and names and set(names) <= set(prior_auth.get("partial_slices", {}).get(owner, [])),
                "Resume repair exceeds the checkpointed partial paths")
        paths.update(names)
    names = source_names(value, repo)
    changed = names.changed(before, start, missing=state.MISSING)
    require(changed <= names.keys(paths), "Resume changed source outside the authorized partial repair")
    require(before["head"] == start["head"], "Integration resume HEAD changed")
    require(timestamp(paused["at"]) <= timestamp(resume.get("approved_at")) <=
            timestamp(authorization.get("approved_at")), "Invalid integration resume approval time")
    require(manifest(value, repo, authorization["start_manifest_path"])["fingerprint"] == start["fingerprint"],
            "Resume checkpoint must match the authorized repaired snapshot")
    return (str(paused_path), paused), [str(path), str(paused_path), str(prior_path),
                                      str(prior_auth_path), paused["source_manifest"]]


def integration_checkpoint(value, repo, contract_path, plan, items, slice_id, previous, events, request, final,
                           sibling_histories=None):
    """Bind a sequential reopen to certified sibling versions, never a replacement base."""
    path = request.get("integration_path") or previous.get("integration_path")
    if not path:
        return set(), [], {}
    names = source_names(value, repo)
    path = absolute_file(path, value)
    run = Path(contract_path).parent.resolve()
    require(path.resolve().is_relative_to(run), "Integration checkpoint must be run-local")
    checkpoint = state.load(path)
    bound(value, repo, checkpoint)
    require(checkpoint.get("slice_id") == slice_id, "Wrong integration slice")
    require(checkpoint.get("schedule_sha256") == plan["schedule_sha256"]
            and checkpoint.get("contract_sha256") == sha(contract_path), "Wrong integration contract/schedule")
    state.text(checkpoint.get("user_authorization"), "integration authorization")
    authorization_path = absolute_file(checkpoint.get("authorization_path"), value)
    require(authorization_path.resolve().is_relative_to(run), "Integration authorization must be run-local")
    authorization = state.load(authorization_path)
    bound(value, repo, authorization)
    state.text(authorization.get("user_authorization"), "human integration authorization")
    require(checkpoint.get("authorization_sha256") == sha(authorization_path), "Integration authorization changed")
    require(authorization.get("contract_sha256") == sha(contract_path)
            and authorization.get("schedule_sha256") == plan["schedule_sha256"], "Foreign integration authorization")
    anchor = absolute_file(checkpoint.get("previous_state_path"), value)
    require(checkpoint.get("previous_state_sha256") == sha(anchor), "Wrong integration state binding")
    anchors = [event for _, event_path, event in events if str(event_path) == str(anchor)]
    require(len(anchors) == 1 and anchors[0]["status"] in {"accepted", "reviewed"},
            "Integration needs an existing reviewed/accepted anchor")
    anchored = anchors[0]
    original = authorization.get("starting_slices", {}).get(slice_id, {})
    require(original.get("author_agent_id") == anchored.get("author_agent_id")
            and any(str(event_path) == original.get("state_path")
                    and sha(event_path) == original.get("state_sha256") for _, event_path, _ in events),
            "Integration anchor is not descended from the authorized slice")
    mode = checkpoint.get("mode")
    require(mode in {"repair", "dependency-recheck"}, "Invalid integration mode")
    allowed = authorization.get("edit_paths", {}).get(slice_id, [])
    write_scope = items[slice_id]["write_scope"]
    require(isinstance(allowed, list) and all(in_scope(name, write_scope, names) for name in allowed),
            "Invalid authorized integration paths")
    if mode == "repair":
        require(allowed, "Slice has no authorized integration edits")
    else:
        require(slice_id in authorization.get("recertify_slices", []), "Dependency recheck is not authorized")
    require(checkpoint.get("author_agent_id") == anchored.get("author_agent_id")
            and request.get("author_agent_id", anchored["author_agent_id"]) == anchored["author_agent_id"],
            "Wrong integration author")
    start_path = absolute_file(checkpoint.get("start_manifest"), value)
    require(start_path.resolve().is_relative_to(run), "Integration manifest must be run-local")
    require(checkpoint.get("start_manifest_sha256") == sha(start_path), "Integration start manifest changed")
    start = manifest(value, repo, str(start_path))
    approved = timestamp(authorization.get("approved_at"))
    require(timestamp(anchored["at"]) <= approved <= timestamp(checkpoint.get("at")) <= datetime.now(timezone.utc),
            "Invalid integration authorization time")
    auth_start_path = absolute_file(authorization.get("start_manifest_path"), value)
    require(auth_start_path.resolve().is_relative_to(run), "Integration authorization snapshot must be run-local")
    require(authorization.get("start_manifest_sha256") == sha(auth_start_path),
            "Integration authorization snapshot changed")
    auth_start = manifest(value, repo, str(auth_start_path))
    anchored_source = manifest(value, repo, anchored["source_manifest"])
    require(auth_start["head"] == anchored_source["head"] == start["head"] == final["head"],
            "Integration HEAD changed")
    start_files, final_files = names.files(start["files"]), names.files(final["files"])
    spellings = names.spellings(start["files"].keys() | final["files"].keys() | set(anchored["touched"]))

    def target_path(name):
        return any(in_scope(path, write_scope, names) for path in spellings.get(name, {name}))

    changes = names.changed(start, final)
    own_changes = {name for name in changes if target_path(name)}
    require(names.changed(start, final, missing=state.MISSING) <= own_changes,
            "Integration foreign source changed outside target scope")
    resumed, resume_seals = integration_resume(value, repo, run, authorization, start, events, slice_id)
    if resumed is not None:
        prior_checkpoint = state.load(absolute_file(resumed[1]["integration_path"], value))
        require(all(checkpoint.get(key) == prior_checkpoint.get(key) for key in
                    ("previous_state_path", "previous_state_sha256", "author_agent_id", "slice_id", "mode")),
                "Resume cannot replace the original integration anchor")
    entering = previous.get("integration_path") != str(path) or previous.get("status") == "accepted"
    if entering:
        require((resumed is not None and resumed[0] == str(events[-1][1])) or str(anchor) == str(events[-1][1]),
                "Integration checkpoint must bind the immediately preceding state")
        require(all(event.get("integration_path") != str(path) for _, _, event in events),
                "Integration checkpoint already consumed")
        require((resumed is not None and mode == "repair" and request.get("status") == "pre-review-passed"
                 and request.get("action") == "integration-resume") or
                (resumed is None and mode == "repair" and request.get("status") == "edited-unverified"
                 and request.get("action") == "integration-fix") or
                (resumed is None and mode == "dependency-recheck" and request.get("status") == "pre-review-passed"
                 and request.get("action") == "dependency-recheck"),
                "Integration starts with its authorized repair or dependency recheck")
        origin = resumed[1] if resumed is not None else anchored
        origin_files = names.files(manifest(value, repo, origin["source_manifest"])["files"])
        for name in names.keys(anchored["touched"]):
            require(start_files.get(name, state.MISSING) == origin_files.get(name, state.MISSING),
                    "Integration start differs from the anchored slice source")
            if resumed is not None:
                require(final_files.get(name, state.MISSING) == start_files.get(name, state.MISSING),
                        "Integration resume cannot add target source edits")
    require(request.get("base_manifest", previous["base_manifest"]) == previous["base_manifest"],
            "Integration cannot replace the original slice base")
    require(set(request.get("touched", previous["touched"])) == set(previous["touched"]),
            "Integration checkpoint cannot expand the slice touched scope")
    require(own_changes <= names.keys(allowed) if mode == "repair" else not own_changes,
            "Source changed outside the authorized integration edits")
    foreign = set()
    sealed = [str(path), str(authorization_path), str(anchor), str(start_path), str(auth_start_path), *resume_seals]
    bindings = checkpoint.get("slice_states")
    require(isinstance(bindings, dict) and slice_id not in bindings, "Invalid integration sibling bindings")
    require(set(bindings) == set(authorization.get("starting_slices", {})) - {slice_id},
            "Integration checkpoint omits an authorized sibling binding")
    verified_bindings = {}
    for identifier, binding in bindings.items():
        require(identifier in items, "Unknown integration sibling")
        sibling_events = (history(run / repo / "slices" / identifier, value) if sibling_histories is None
                          else sibling_histories[identifier])
        require(sibling_events, "Integration sibling has no history")
        event_path, event = sibling_events[-1][1:]
        require(guard_path(binding["path"], value).resolve() == event_path.resolve()
                and binding["sha256"] == sha(event_path),
                "Integration sibling state changed")
        require(event.get("status") == "accepted", "Integration sibling is not accepted")
        verified_bindings[identifier] = binding
        inherited = {}
        for _, _, historical in sibling_events:
            bound(value, repo, historical)
            require(historical.get("slice_id") == identifier
                    and historical.get("schedule_sha256") == plan["schedule_sha256"], "Foreign sibling history")
            check_event_seals(value, repo, historical, inherited)
            inherited = historical.get("artifact_hashes", {})
        for name, recorded in event["accepted_files"].items():
            require(in_scope(name, items[identifier]["write_scope"], names), "Sibling path outside its approved scope")
            key = names.key(name)
            require(start_files.get(key, state.MISSING) == recorded,
                    "Integration sibling does not match its acceptance")
            require(final_files.get(key, state.MISSING) == recorded,
                    "Integration sibling source changed")
            foreign.add(key)
        sealed.append(str(event_path))
    partial = checkpoint.get("partial_slices", {})
    require(isinstance(partial, dict), "Invalid partial integration slices")
    authorized_partial = authorization.get("partial_slices", {})
    authorized_files = names.files(auth_start["files"])
    require(partial == authorized_partial, "Integration checkpoint omits authorized partial provenance")
    for identifier, paths in partial.items():
        require(identifier in items and identifier != slice_id and identifier not in bindings,
                "Invalid partial slice owner")
        require(not history(run / repo / "slices" / identifier, value), "Partial slice already has lifecycle history")
        require(isinstance(paths, list) and paths == authorized_partial.get(identifier),
                "Partial integration paths lack explicit authorization")
        for name in paths:
            require(in_scope(name, items[identifier]["write_scope"], names), "Partial path outside its approved scope")
            key = names.key(name)
            recorded = authorized_files.get(key, state.MISSING)
            require(recorded != state.MISSING, "Partial integration path missing from authorized snapshot")
            require(start_files.get(key) == recorded and final_files.get(key) == recorded,
                    "Partial integration source changed")
            foreign.add(key)
        sealed.append(authorization["start_manifest_path"])
    base = manifest(value, repo, anchored["base_manifest"])
    foreign_at_start = {name for name in names.changed(base, start) if not target_path(name)}
    require(foreign_at_start <= foreign, "Integration checkpoint omits foreign source provenance")
    return foreign, sealed, verified_bindings if entering else {}


def predecessor_provenance(value, repo, contract_path, items, slice_id, final, request):
    """First implementation may include source accepted by its dependencies, pinned to each accepted state."""
    bindings = request.get("predecessor_states")
    require(isinstance(bindings, dict) and bindings, "Predecessor provenance needs predecessor_states")
    require(set(bindings) == set(items[slice_id]["depends_on"]),
            "Predecessor provenance must bind every dependency exactly")
    run = Path(contract_path).parent.resolve()
    names = source_names(value, repo)
    final_files = names.files(final["files"])
    covered, sealed = {}, []
    for identifier in sorted(bindings):
        binding = bindings[identifier]
        require(isinstance(binding, dict) and set(binding) == {"path", "sha256"}, "Invalid predecessor binding")
        events = history(run / repo / "slices" / identifier)
        require(events, "Predecessor has no lifecycle history")
        event_path, event = events[-1][1:]
        require(Path(binding["path"]).resolve() == event_path.resolve() and binding["sha256"] == sha(event_path),
                "Predecessor state changed")
        bound(value, repo, event)
        require(event.get("status") == "accepted", "Predecessor is not accepted")
        for name, recorded in event["accepted_files"].items():
            require(in_scope(name, items[identifier]["write_scope"], names), "Predecessor path outside its approved scope")
            key = names.key(name)
            require(key not in covered or covered[key] == recorded, "Conflicting predecessor file states")
            covered[key] = recorded
        sealed.append(str(event_path))
    # Only paths whose current bytes equal the predecessor's accepted bytes are foreign-provenanced.
    matched = {name for name, recorded in covered.items()
               if final_files.get(name, state.MISSING) == recorded}
    return matched, sealed


def slice_transition(contract_path, repo, slice_id, schedule_path, request_path, out):
    """Advance one slice through its own bounded lifecycle (own review budget), sequentially.

    The repo lifecycle is unchanged: it starts after every slice is accepted (see slice_gate) and keeps its own
    final review/fix budget over the whole repo diff.
    """
    value = contract(contract_path)
    require_unreserved(value, contract_path, repo)
    plan = slice_schedule(contract_path, repo, schedule_path)
    items = {i["id"]: i for i in state.load(absolute_file(schedule_path))["slices"]}
    require(slice_id in items, "Unknown slice")
    continuity = budget_continuity(value, repo, contract_path, slice_id)
    item = items[slice_id]
    directory = Path(contract_path).parent / repo / "slices" / slice_id
    events = history(directory) if directory.is_dir() else []
    previous = events[-1][2] if events else {}
    inherited_seals = {}
    for _, _, event in events:
        bound(value, repo, event)
        require(event.get("slice_id") == slice_id and event.get("schedule_sha256") == plan["schedule_sha256"],
                "Slice history belongs to a different slice or schedule")
        check_event_seals(value, repo, event, inherited_seals)
        inherited_seals = event.get("artifact_hashes", {})
    request = state.load(absolute_file(request_path))
    require(isinstance(request, dict), "Slice request must be an object")
    protected = {"review_fingerprint", "artifact_hashes", "accepted_files", "dependency_states", "schema_version",
                 "run_id", "repo", "slice_id", "sequence", "at", "schedule_sha256", "review_fix_count",
                 "latest_review_round", "review_fix_limit", "is_continuation", "historical_used_passes"}
    status = request.get("status")
    require(not protected & set(request), "Request overrides certified slice fields")
    if has_bound_pre_review(value, repo):
        require("pre_review_evidence_paths" not in request or status == "pre-review-passed",
                "Slice pre-review evidence can change only before review")
    if "deliverable_manifest_path" in request:
        require(status == "pre-review-passed", "Deliverable manifest can change only before review")
    require("capsule_handoff" not in request or request.get("status") == "edited-unverified",
            "Capsule handoff requires edited-unverified")
    before = previous.get("status")
    binding_recheck = status == "edited-unverified" and request.get("action") == "binding-recheck"
    if binding_recheck:
        require(before in {"pre-review-passed", "reviewed"} and not previous.get("review_acceptance_path")
                and set(request) <= {"status", "action", "source_manifest", "base_manifest", "touched"}
                and stale_binding(value, repo, previous, slice_item=item),
                "Binding recheck needs a stale binding and a clean slice boundary")
    accepting = before == "capped" and status == "reviewed-with-findings"
    require("review_acceptance_path" not in request or accepting, "Unexpected acceptance path")
    continuation_path = request.get("continuation_path")
    continuation_limit = None
    if continuation_path:
        require(before == "capped" and status == "needs-fix", "Continuation requires capped -> needs-fix")
        auth_path = absolute_file(continuation_path)
        require(auth_path.resolve().is_relative_to(Path(contract_path).parent.resolve()),
                "Continuation authorization must be run-local")
        auth = state.load(auth_path)
        bound(value, repo, auth)
        require(auth.get("slice_id") == slice_id, "Wrong continuation slice")
        state.text(auth.get("user_authorization"), "human continuation authorization")
        require(auth.get("previous_state_path") == str(events[-1][1])
                and auth.get("previous_state_sha256") == sha(events[-1][1]), "Wrong continuation state binding")
        require(timestamp(previous["at"]) <= timestamp(auth.get("approved_at")) <= datetime.now(timezone.utc),
                "Invalid continuation approval timestamp")
        extra = auth.get("additional_passes")
        require(type(extra) is int and extra > 0, "Continuation needs positive integer additional_passes")
        require(auth.get("author_agent_id") == previous.get("author_agent_id"), "Wrong continuation author")
        open_ids = {f["id"] for f in state.load(absolute_file(previous.get("review_path")))["findings"]}
        require(set(auth.get("finding_ids", [])) == open_ids, "Continuation finding scope differs")
        require(all(e.get("continuation_path") != str(auth_path) for _, _, e in events),
                "Continuation authorization already consumed")
        require(request.get("review_path", previous.get("review_path")) == previous.get("review_path"),
                "Continuation cannot replace the current review")
        require(request.get("author_agent_id", previous.get("author_agent_id")) == previous.get("author_agent_id"),
                "Continuation cannot replace the author")
        continuation_limit = max(previous.get("review_fix_limit", SLICE_REVIEW_FIX_BUDGET),
                                 previous.get("review_fix_count", 0)) + extra
    dependency_recheck = (before == "accepted" and status == "pre-review-passed"
                          and request.get("action") == "dependency-recheck" and request.get("integration_path"))
    if status in {"blocked", "capped"}:
        require(events, "Nothing to block yet")
        state.text(request.get("reason"), "blocker reason")
    elif continuation_limit is None and not dependency_recheck and not accepting and not binding_recheck:
        require(status in SLICE_TRANSITIONS.get(before, set()), f"Illegal slice transition: {before} -> {status}")
    require(before not in {"blocked", "capped"} or continuation_limit is not None or accepting,
            "Exhausted slice needs explicit human continuation")
    expected_out = directory / f"state-{len(events) + 1}.json"
    require(Path(out).resolve() == expected_out.resolve(), "Slice transition output must be next immutable state")
    names = source_names(value, repo)
    group = next(g for g in plan["groups"] if slice_id in g)
    barrier_path = request.get("batch_barrier_path") or previous.get("batch_barrier_path")
    siblings = set()
    if len(group) > 1:
        barrier = state.load(absolute_file(barrier_path))
        bound(value, repo, barrier)
        check_seals(barrier["artifact_hashes"])
        require(barrier["status"] == "closed" and sorted(barrier["members"]) == sorted(group),
                "A parallel group needs a closed barrier for exactly this group")
        require(barrier["results"][slice_id] == "passed", "This slice's writer did not finish passed")
        require(request.get("base_manifest", barrier["base_manifest"]) == barrier["base_manifest"],
                "A batch member's base is the batch start manifest")
        request.setdefault("base_manifest", barrier["base_manifest"])
        if status == "implemented":
            require(names.keys(request.get("touched", [])) == names.keys(barrier["owned"][slice_id]),
                    "Touched files must equal what the barrier attributed to this slice")
        for other, paths in barrier["owned"].items():
            if other != slice_id:
                siblings |= set(paths)
    base_path = request.get("base_manifest") or previous.get("base_manifest")
    base = manifest(value, repo, base_path)
    touched = sorted(set(previous.get("touched", [])) | set(request.get("touched", [])))
    require(set(previous.get("touched", [])) <= set(touched), "Slice touched files must not shrink")
    outside = [p for p in touched if not in_scope(p, item["write_scope"], names)]
    require(not outside, "Touched file outside slice write_scope: " + ", ".join(outside))
    final = fresh(value, repo, request.get("source_manifest"), touched)
    if binding_recheck:
        require(touched == previous["touched"] and base_path == previous["base_manifest"]
                and state.same_source(final, event_manifest(value, repo, previous), repo_read_excludes(value, repo)),
                "Binding recheck cannot change slice scope, base, or source")
    if accepting:
        require(set(request) <= {"status", "source_manifest", "base_manifest", "touched", "review_acceptance_path"},
                "Acceptance cannot change slice context")
        require(touched == previous["touched"] and base_path == previous["base_manifest"],
                "Acceptance cannot change slice scope or base")
        acceptance_path = accept_review_findings(contract_path, value, repo, events, request, final, slice_id)
    if continuation_limit is not None:
        require(final["fingerprint"] == previous["source_fingerprint"], "Continuation source must be unchanged")
        require(touched == previous["touched"] and base_path == previous["base_manifest"],
                "Continuation cannot change source scope or base")
    foreign, integration_seals, new_bindings = integration_checkpoint(
        value, repo, contract_path, plan, items, slice_id, previous, events, request, final)
    final_files = names.files(final["files"])
    require(not dependency_recheck or integration_seals, "Dependency recheck needs integration authorization")
    if dependency_recheck:
        require(all(final_files.get(names.key(name), state.MISSING) == recorded
                    for name, recorded in previous["accepted_files"].items()),
                "Dependency recheck cannot change this slice's source")
    bound_predecessors = previous.get("predecessor_states")
    if "predecessor_states" in request:
        require(status == "implemented" and not events,
                "Predecessor provenance only accompanies the first implementation")
        bound_predecessors = request["predecessor_states"]
    if bound_predecessors is not None and new_bindings:
        bound_predecessors = {identifier: new_bindings.get(identifier, binding)
                              for identifier, binding in bound_predecessors.items()}
    if integration_seals:
        require(set(items[slice_id]["depends_on"]) <= accepted_slices(
            contract_path, repo, set(items[slice_id]["depends_on"])),
            "Slice predecessors need current source and dependency acceptance")
    predecessor, predecessor_seals = set(), []
    if bound_predecessors is not None:
        predecessor, predecessor_seals = predecessor_provenance(
            value, repo, contract_path, items, slice_id, final, {"predecessor_states": bound_predecessors})
    changed = names.changed(base, final)
    unowned = changed - names.keys(touched) - names.keys(siblings) - foreign - predecessor
    require(not unowned,
            "Changes outside the slice's declared touched files: "
            + ", ".join(sorted(unowned)))
    data = {**previous, **request, "schema_version": VERSION, "run_id": value["run_id"], "repo": repo,
            "slice_id": slice_id, "sequence": len(events) + 1, "at": datetime.now(timezone.utc).isoformat(),
            "schedule_sha256": plan["schedule_sha256"], "base_manifest": base_path, "touched": touched,
            "batch_barrier_path": barrier_path,
            "source_fingerprint": final["fingerprint"]}
    if bound_predecessors is not None:
        data["predecessor_states"] = bound_predecessors
    if integration_seals:
        data["integration_path"] = integration_seals[0]
    for key in ("review_fix_count", "latest_review_round"):
        data[key] = previous.get(key, 0)
    if not events and continuity:
        data["review_fix_limit"] = continuity["effective_limit"] - continuity["used_passes"]
        data["historical_used_passes"] = continuity["used_passes"]
    data["is_continuation"] = continuation_limit is not None
    if continuation_limit is not None:
        data["review_fix_limit"] = continuation_limit
    sealed = [contract_path, request.get("source_manifest"), request_path, base_path, *integration_seals,
              *predecessor_seals]
    for applied in value.get("_amendments", []):
        sealed.append(applied["path"])
    if value.get("_amendment"):
        data["amendment_sha256"] = value["_amendment"]["sha256"]
    if continuation_limit is not None:
        sealed.append(continuation_path)
    if accepting:
        sealed.append(acceptance_path)
    if events:
        sealed.append(str(events[-1][1]))
    if status == "implemented":
        require(set(item["depends_on"]) <= accepted_slices(contract_path, repo, set(item["depends_on"])),
                "Slice predecessors are not accepted")
        state.text(request.get("author_agent_id"), "slice author agent ID")
        data["author_agent_id"] = request["author_agent_id"]
        sealed += slice_matrix(value, repo, item, request.get("matrix_path"), final)
    elif status not in {"blocked", "capped"}:
        data["author_agent_id"] = previous["author_agent_id"]
    if binding_recheck:
        for key in ("deliverable_manifest_path", "pre_review_evidence_paths", "review_fingerprint"):
            data.pop(key, None)
    elif status == "edited-unverified":
        data.pop("review_acceptance_path", None)
        action = request.get("action")
        require(action in {"review-fix", "integration-fix"}, "Missing slice edit cause")
        paths, new_author = (capsule_handoff(contract_path, value, repo, request, previous, events,
                                             previous["author_agent_id"], previous["source_fingerprint"], slice_id)
                             if request.get("capsule_handoff") is not None else ([], None))
        sealed += paths
        if new_author:
            data["author_agent_id"] = new_author
        else:
            require(request.get("author_agent_id", data["author_agent_id"]) == previous["author_agent_id"],
                    "Slice repair must go to the slice author; replacement needs a human-approved handoff")
        if action == "review-fix":
            require(before == "needs-fix" and previous.get("review_path"), "Slice review fix needs findings")
            require(data["review_fix_count"] < data.get("review_fix_limit", SLICE_REVIEW_FIX_BUDGET),
                    "Slice review-fix budget exhausted")
            if request.get("repair_phase_id") is not None:
                identifier = request["repair_phase_id"]
                require(isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", identifier)
                        and identifier not in {".", ".."}, "Invalid slice repair phase ID")
                directory = Path(contract_path).parent / "telemetry"
                paths = [directory / f"{identifier}-{suffix}.json" for suffix in ("start", "end")]
                start, end = [state.load(absolute_file(path)) for path in paths]
                expected = {"schema_version": VERSION, "run_id": value["run_id"], "workflow": value["workflow"],
                            "repo": repo, "slice_id": slice_id, "phase_id": identifier, "phase": "review-fix",
                            "action": action, "previous_state_path": str(events[-1][1]),
                            "previous_state_sha256": sha(events[-1][1]), "round": previous["latest_review_round"]}
                require(all(all(boundary.get(key) == val for key, val in expected.items())
                            for boundary in (start, end)) and start.get("event") == "phase-start"
                        and end.get("event") == "phase-end" and end.get("start_sha256") == sha(paths[0])
                        and end.get("status") in {"passed", "failed", "blocked", "interrupted"}
                        and timestamp(previous["at"]) <= timestamp(start["at"]) <= timestamp(end["at"])
                        <= datetime.now(timezone.utc), "Slice repair phase binding mismatch")
                require(start.get("agent_id") == previous["author_agent_id"]
                        and end.get("agent_id") == (new_author or previous["author_agent_id"]),
                        "Slice repair phase author mismatch")
                require(not any(event.get("repair_phase_id") == identifier for _, _, event in events),
                        "Slice repair phase already used")
                prior_boundaries = start.get("prior_repair_boundaries", {})
                require(isinstance(prior_boundaries, dict), "Invalid prior repair boundary bindings")
                for path, expected_hash in prior_boundaries.items():
                    boundary = absolute_file(path, value)
                    require(boundary.parent == directory and guarded_sha(boundary, value) == expected_hash,
                            "Prior repair boundary changed")
                sealed += [str(path) for path in paths] + list(prior_boundaries)
                if start.get("repair_recovery_path"):
                    recovery_path = run_local_evidence_path(start["repair_recovery_path"], Path(contract_path).parent,
                                                            value, "Partial repair recovery")
                    require(guarded_sha(recovery_path, value) == start.get("repair_recovery_sha256"),
                            "Partial repair recovery changed")
                    sealed.append(str(recovery_path))
                    recovery = guarded_load(recovery_path, value)
                    partial_id = recovery.get("phase_id")
                    require(isinstance(partial_id, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", partial_id)
                            and partial_id not in {".", ".."}, "Invalid partial repair phase ID")
                    partial_end = guarded_load(absolute_file(directory / f"{partial_id}-end.json", value), value)
                    checkpoint = run_local_evidence_path(partial_end.get("checkpoint_path"), Path(contract_path).parent,
                                                         value, "Partial repair checkpoint")
                    require(guarded_sha(checkpoint, value) == partial_end.get("checkpoint_sha256"),
                            "Partial repair checkpoint changed")
                    sealed.append(str(checkpoint))
                for path in prior_boundaries:
                    prior = guarded_load(path, value)
                    if prior.get("event") != "phase-start" or not prior.get("repair_recovery_path"):
                        continue
                    recovery_path = run_local_evidence_path(prior["repair_recovery_path"], Path(contract_path).parent,
                                                            value, "Partial repair recovery")
                    require(guarded_sha(recovery_path, value) == prior.get("repair_recovery_sha256"),
                            "Prior partial repair recovery changed")
                    recovery = guarded_load(recovery_path, value)
                    partial_id = recovery.get("phase_id")
                    require(isinstance(partial_id, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", partial_id)
                            and partial_id not in {".", ".."}, "Invalid partial repair phase ID")
                    partial_end = guarded_load(absolute_file(directory / f"{partial_id}-end.json", value), value)
                    checkpoint = run_local_evidence_path(partial_end.get("checkpoint_path"), Path(contract_path).parent,
                                                         value, "Partial repair checkpoint")
                    require(guarded_sha(checkpoint, value) == partial_end.get("checkpoint_sha256"),
                            "Prior partial repair checkpoint changed")
                    sealed += [str(recovery_path), str(checkpoint)]
            data["review_fix_count"] += 1
            applied_path = request.get("applied_path")
            result = state.load(absolute_file(applied_path))
            bound(value, repo, result)
            findings = state.load(previous["review_path"])
            state.applied(result, findings)
            for entry in result["applied"]:
                original = next(f for f in findings["findings"] if f["id"] == entry["id"])
                require(entry.get("verification_kind") == original["verification_kind"], "Applied kind mismatch")
                require(entry.get("evidence_paths"), "Applied finding needs evidence")
                for p in entry["evidence_paths"]:
                    sealed += finding_proof(value, repo, p, final, entry["id"], original["verification_kind"])
            sealed.append(applied_path)
        else:
            require(before in {"accepted", "reviewed"}, "Integration fix applies to a reviewed or accepted slice")
        for key in ("accepted_files", "dependency_states", "review_fingerprint"):
            data.pop(key, None)
    delivery = None
    if status == "pre-review-passed":
        sealed += slice_matrix(value, repo, item, request.get("matrix_path", previous.get("matrix_path")), final)
        delivery, paths = deliverable_binding(value, repo, data.get("deliverable_manifest_path"), slice_item=item)
        sealed += paths + check_set(value, repo, "pre_review_checks", request.get("pre_review_evidence_paths"),
                                    final, delivery=delivery)
    if status in {"needs-fix", "reviewed"} and continuation_limit is None:
        delivery, paths = deliverable_binding(value, repo, previous.get("deliverable_manifest_path"), slice_item=item)
        sealed += paths + recheck_pre_review(value, repo, previous, final, delivery)
        prior_paths = [e[2]["review_path"] for e in events
                       if e[2]["status"] in {"needs-fix", "reviewed"} and not e[2].get("is_continuation")]
        result, paths = review(value, repo, request.get("review_path"), final, touched, prior_paths,
                               Path(contract_path).parent.resolve(), previous.get("artifact_hashes", {}))
        if delivery is not None:
            match_delivery_reference(result, delivery)
        contributions = {c["criterion_id"] for c in item["contributions"]}
        for applied in value.get("_amendments", []):
            if applied["criterion_id"] not in contributions:
                continue
            require(result.get("amendment_sha256") in requirement_amendment_suffix(value, applied["criterion_id"]),
                    "Slice review must assess the amended requirement")
        require(result["round"] == data["latest_review_round"] + 1, "Slice review rounds must increase by one")
        require(bool(result["findings"]) == (status == "needs-fix"), "Findings/status mismatch")
        data["latest_review_round"] = result["round"]
        data["review_fingerprint"] = final["fingerprint"]
        sealed += paths
    if status == "accepted" or (accepting and has_bound_pre_review(value, repo)):
        delivery, paths = deliverable_binding(value, repo, previous.get("deliverable_manifest_path"), slice_item=item)
        sealed += paths + recheck_pre_review(value, repo, previous, final, delivery)
        require(review_source_matches(value, repo, previous, events, final), "Slice review is stale")
        accepted_review = state.load(absolute_file(previous["review_path"]))
        if delivery is not None:
            match_delivery_reference(accepted_review, delivery)
        if not accepting:
            require(not accepted_review["findings"] or previous.get("review_acceptance_path"),
                    "Slice review has open findings")
    if status == "accepted":
        contributions = {c["criterion_id"] for c in item["contributions"]}
        for applied in value.get("_amendments", []):
            if applied["criterion_id"] not in contributions:
                continue
            require(accepted_review.get("amendment_sha256") in requirement_amendment_suffix(value, applied["criterion_id"]),
                    "Slice acceptance requires review of amended requirement")
        data["accepted_files"] = {p: final_files.get(names.key(p), state.MISSING) for p in touched}
        data["dependency_states"] = {}
        require(set(item["depends_on"]) <= accepted_slices(contract_path, repo, set(item["depends_on"])),
                "Slice predecessors need current source and dependency acceptance")
        for dep in item["depends_on"]:
            dep_events = history(Path(contract_path).parent / repo / "slices" / dep)
            require(dep_events and dep_events[-1][2]["status"] == "accepted", f"Predecessor not accepted: {dep}")
            data["dependency_states"][dep] = sha(dep_events[-1][1])
    data["artifact_hashes"] = extend_seals(value, previous, [p for p in sealed if p], delivery)
    state.write_once(out, data)
    return data


def check_accepted_delivery(value, repo, item, event):
    delivery, paths = deliverable_binding(value, repo, event.get("deliverable_manifest_path"), slice_item=item)
    paths += recheck_pre_review(value, repo, event, event_manifest(value, repo, event), delivery)
    seals = event.get("artifact_hashes", {})
    require(all(seals.get(path) == sha(absolute_file(path, value)) for path in paths),
            "Slice acceptance is missing a current artifact seal")
    if delivery is not None:
        match_delivery_reference(state.load(absolute_file(event["review_path"], value)), delivery)
    return paths


def check_slice_delivery(value, repo, gate_path, manifest_path=None):
    declaration = value.get("slice_schedules", {}).get(repo)
    if declaration is None or not has_bound_pre_review(value, repo):
        return []
    schedule, plan = checked_slice_schedule(value, repo, declaration["path"])
    gate_path = run_local_path(value, gate_path, file=True)
    gate = state.load(gate_path)
    bound(value, repo, gate)
    require(gate.get("schedule_sha256") == plan["schedule_sha256"], "Slice gate has the wrong schedule")
    require(set(gate.get("slices", {})) == {item["id"] for item in schedule["slices"]}, "Slice gate omits slices")
    rows = {} if manifest_path is None else {
        row["id"]: row for row in state.load(run_local_path(value, manifest_path, file=True))["deliverables"]}
    sealed = [str(gate_path)]
    for item in schedule["slices"]:
        identifier = item["id"]
        events = history(Path(value["_contract_path"]).parent / repo / "slices" / identifier, value)
        require(events and events[-1][2]["status"] == "accepted", f"Slice not accepted: {identifier}")
        _, path, event = events[-1]
        require(gate["slices"][identifier] == sha(path) == gate["artifact_hashes"].get(str(path)),
                f"Slice {identifier} changed after slice-gate; run slice-gate again")
        require(event.get("schedule_sha256") == plan["schedule_sha256"] and event.get("slice_id") == identifier,
                "Slice state has the wrong schedule or scope")
        sealed += [str(path), *check_accepted_delivery(value, repo, item, event)]
        for dep, expected in event["dependency_states"].items():
            require(gate["slices"].get(dep) == expected, f"Slice {identifier} has a stale predecessor")
        if rows and event.get("deliverable_manifest_path"):
            recorded = state.load(run_local_path(value, event["deliverable_manifest_path"], file=True))
            for row in recorded["deliverables"]:
                require(row["id"] in rows and {k: v for k, v in row.items() if k != "snapshot"}
                        == {k: v for k, v in rows[row["id"]].items() if k != "snapshot"},
                        f"Repo deliverable differs from accepted slice {identifier}: {row['id']}")
    return sealed


def delivery_status(value, repo, event, *, slice_item=None, recheck=True):
    entries = value.get("deliverables", {}).get(repo, [])
    if not entries:
        return []
    current = event or {}
    path = current.get("deliverable_manifest_path")
    selected, _ = deliverable_selection(value, repo, slice_item)
    due = {entry["id"] for entry in selected}
    result = {entry["id"]: {"id": entry["id"], "path": entry["path"],
                            "required_at": delivery_scope(entry), "status": "pending"} for entry in entries}
    if not path:
        return list(result.values())
    try:
        path = str(run_local_path(value, path, file=True))
        reference = artifact_reference(path)
        require(current.get("artifact_hashes", {}).get(path) == reference["sha256"], "Deliverable manifest seal changed")
        _, rows = read_deliverable_manifest(value, repo, path, slice_item=slice_item)
    except (ValueError, OSError, TypeError, KeyError) as error:
        for identifier in due:
            result[identifier].update(status="stale", error=str(error))
        return list(result.values())
    for entry, version in rows:
        row = result[entry["id"]]
        row.update(status="verified", manifest=reference,
                   version={key: val for key, val in version.items() if key != "id"})
        if recheck:
            try:
                validate_deliverable_row(value, entry, version)
            except (ValueError, OSError, TypeError, KeyError) as error:
                row.update(status="stale", error=str(error))
    return list(result.values())


def check_input_status(value, repo, event):
    result = []
    paths = [*event.get("pre_review_evidence_paths", []), *event.get("gate_evidence_paths", [])]
    if event.get("assessment_path"):
        report = state.load(absolute_file(event["assessment_path"], value))
        paths.extend(path for row in report["criteria"] for path in row["evidence_paths"])
    for path in dict.fromkeys(paths):
        record = state.load(absolute_file(path, value))
        binding = record.get("input_bindings")
        if not binding:
            continue
        manifest_path = checked_reference(value, binding["before"])
        observation = state.load(manifest_path)
        item = {"check_id": observation["check_id"], "evidence_path": path, "bindings": binding,
                "inputs": observation["inputs"], "status": "verified"}
        try:
            require(event.get("artifact_hashes", {}).get(path) == sha(path), "Check evidence seal changed")
            validate_input_binding(value, repo, record)
        except (ValueError, OSError) as error:
            item.update(status="stale", error=str(error))
        result.append(item)
    return result


def confirm_completion_versions(value, summaries):
    """Check paths again after building the seal map for every repo."""
    guard = excluded_guard(value)
    for repo in summaries.values():
        deliveries = [item["version"] for item in repo.get("deliverables", []) if "store" in item["version"]]
        checks = [*repo.get("check_inputs", []),
                  *(row for rows in repo.get("slice_check_inputs", {}).values() for row in rows)]
        inputs = [row for check in checks for row in check["inputs"]]
        for row in deliveries + inputs:
            path, name = row["path"], row["store"]
            actual = store.observe(path, store_file(value, path, name), guard, hash_contents=False)
            expected = {key: row[key] for key in actual}
            expected["directories"] = [entry[:2] for entry in expected["directories"]]
            require(actual == expected, f"STORE path changed during completion: {path}")


def accepted_slices(contract_path, repo, ids):
    value = contract(contract_path)
    root = Path(value["repo_paths"][repo]).resolve()
    memo = {}
    items = {}
    if ids and has_bound_pre_review(value, repo) and repo in value.get("slice_schedules", {}):
        schedule, _ = checked_slice_schedule(value, repo, value["slice_schedules"][repo]["path"])
        items = {item["id"]: item for item in schedule["slices"]}

    def current(identifier, visiting):
        if identifier in memo:
            return memo[identifier]
        if identifier in visiting:
            return False
        directory = Path(contract_path).parent / repo / "slices" / identifier
        events = history(directory) if directory.is_dir() else []
        if not events or events[-1][2]["status"] != "accepted":
            return False
        event = events[-1][2]
        valid = all(state.file_state(state.safe_path(root, name)) == recorded
                    for name, recorded in event["accepted_files"].items())
        if valid and identifier in items:
            try:
                check_accepted_delivery(value, repo, items[identifier], event)
            except (ValueError, OSError):
                valid = False
        for dep, expected in event["dependency_states"].items():
            dep_events = history(Path(contract_path).parent / repo / "slices" / dep)
            valid = (valid and bool(dep_events) and sha(dep_events[-1][1]) == expected
                     and current(dep, visiting | {identifier}))
        memo[identifier] = valid
        return valid

    return {identifier for identifier in ids if current(identifier, set())}


def slice_gate(contract_path, repo, schedule_path, initial_path, source_path, out, amendment_path=None):
    record = prepare_slice_gate(contract_path, repo, schedule_path, initial_path, source_path, amendment_path)
    state.write_once(out, record)


def prepare_slice_gate(contract_path, repo, schedule_path, initial_path, source_path, amendment_path=None):
    """Certify that every slice is accepted, current and not stale before the repo-level lifecycle starts."""
    value = contract(contract_path)
    require_unreserved(value, contract_path, repo)
    plan = slice_schedule(contract_path, repo, schedule_path)
    schedule = state.load(absolute_file(schedule_path))
    initial = manifest(value, repo, initial_path)
    root = Path(contract_path).parent / repo / "slices"
    latest, touched, authors, sealed = {}, set(), {}, [contract_path, schedule_path, initial_path, source_path]
    for applied in value.get("_amendments", []):
        sealed.append(applied["path"])
    for item in schedule["slices"]:
        events = history(root / item["id"]) if (root / item["id"]).is_dir() else []
        require(events and events[-1][2]["status"] == "accepted", f"Slice not accepted: {item['id']}")
        inherited_seals = {}
        immutable_paths = []
        for _, _, event in events:
            immutable_paths = check_event_seals(value, repo, event, inherited_seals)
            inherited_seals = event.get("artifact_hashes", {})
        contributions = {c["criterion_id"] for c in item["contributions"]}
        for applied in value.get("_amendments", []):
            if applied["criterion_id"] not in contributions:
                continue
            suffix = requirement_amendment_suffix(value, applied["criterion_id"])
            require(events[-1][2].get("amendment_sha256") in suffix,
                    "Amended slice requires current review and acceptance")
            accepted_review = state.load(absolute_file(events[-1][2].get("review_path")))
            require(accepted_review.get("amendment_sha256") in suffix,
                    "Amended slice requires current review and acceptance")
        latest[item["id"]] = events[-1]
        if has_bound_pre_review(value, repo):
            sealed += check_accepted_delivery(value, repo, item, events[-1][2])
        touched |= set(events[-1][2]["touched"])
        authors[item["id"]] = events[-1][2]["author_agent_id"]
        sealed += [str(events[-1][1]), *immutable_paths]
    # Repo certification binds ignored dependencies as well as writable source. Use
    # the repo lifecycle's already frozen dependency scope and exclusions, otherwise
    # this gate could never certify the same fingerprint as the next transition.
    # Slice-only historical callers without a repo lifecycle retain their old scope.
    repo_events = history(root.parent)
    dependency_scope, exclusions = [], []
    additions = []
    if repo_events:
        inherited_seals = {}
        for _, _, event in repo_events:
            check_event_seals(value, repo, event, inherited_seals)
            inherited_seals = event.get("artifact_hashes", {})
        repo_state = repo_events[-1][2]
        dependency_scope = repo_state.get("source_scope", [])
        exclusions = repo_state.get("excludes", [])
        repo_initial = manifest(value, repo, repo_state["initial_manifest"])
        require(initial["fingerprint"] == repo_initial["fingerprint"],
                "Slice gate initial manifest differs from repo initial source")
        sealed.append(str(repo_events[-1][1]))
        if amendment_path:
            additions, amendment_seals = scope_amendment(contract_path, value, repo, amendment_path,
                                                          repo_events, initial)
            amendment = state.load(amendment_path)
            require(amendment["slice_states"] == {i: sha(p) for i, (_, p, _) in latest.items()},
                    "Scope amendment accepted slices changed")
            sealed += amendment_seals
        else:
            require(not repo_state.get("scope_amendment_path"), "Scope amendment required by repo history")
    else:
        require(not amendment_path, "Scope amendment requires repo history")
    final = fresh(value, repo, source_path, sorted(touched | set(dependency_scope) | set(additions)), exclusions)
    names = source_names(value, repo)
    final_files = names.files(final["files"])
    for identifier, (_, path, event) in latest.items():
        for name, recorded in event["accepted_files"].items():
            require(final_files.get(names.key(name), state.MISSING) == recorded,
                    f"Slice {identifier} is stale: {name} changed after acceptance; re-review it")
        for dep, dep_sha in event["dependency_states"].items():
            require(sha(latest[dep][1]) == dep_sha, f"Slice {identifier} is stale: predecessor {dep} was re-accepted")
    unowned = names.changed(initial, final) - names.keys(touched | set(additions))
    require(not unowned, "Repo changes not owned by any accepted slice: " + ", ".join(sorted(unowned)))
    if amendment_path:
        before = manifest(value, repo, state.load(amendment_path)["before_manifest"])
        require(names.changed(before, final) <= names.keys(additions), "Scope amendment changed unapproved source")
    record = {"schema_version": VERSION, "run_id": value["run_id"], "repo": repo,
                           "source_fingerprint": final["fingerprint"], "slice_authors": authors,
                           "amendment_sha256": value.get("_amendment", {}).get("sha256"),
                           "slices": {i: sha(p) for i, (_, p, _) in latest.items()},
                           "touched_union": sorted(touched), "cross_slice_criteria": plan["cross_slice_criteria"],
                           "schedule_sha256": plan["schedule_sha256"], "artifact_hashes": seal_files(sealed)}
    if amendment_path:
        record["scope_amendment_path"] = str(amendment_path)
        record["amended_paths"] = sorted(additions)
    return record


def recovery_reference(run, reference, label, value):
    require(isinstance(reference, dict) and set(reference) == {"path", "sha256"},
            f"Slice repair recovery needs a path and hash for {label}")
    path = absolute_file(reference["path"], value)
    require(path.resolve().is_relative_to(run), f"Slice repair recovery {label} must be run-local")
    require(sha(path) == reference["sha256"], f"Slice repair recovery {label} hash mismatch")
    return str(path)


def gate_slice_histories(value, repo, gate, histories):
    """Select the accepted history prefixes sealed by a gate, even after later slice operations."""
    require(set(gate.get("slices", {})) == set(histories), "Slice gate does not bind the approved schedule")
    prefixes = {}
    for identifier, events in histories.items():
        expected = gate["slices"][identifier]
        matches = [i for i, path, _ in events if gate["artifact_hashes"].get(str(path)) == expected]
        require(len(matches) == 1, f"Slice gate needs a sealed accepted state for {identifier}")
        prefix = events[:matches[0]]
        _, path, accepted = prefix[-1]
        require(sha(path) == expected and accepted.get("status") == "accepted",
                f"Slice gate does not bind an accepted state for {identifier}")
        inherited = {}
        for _, _, event in prefix:
            bound(value, repo, event)
            require(event.get("slice_id") == identifier and event.get("schedule_sha256") == gate["schedule_sha256"],
                    f"Foreign slice history for {identifier}")
            check_event_seals(value, repo, event, inherited)
            inherited = event["artifact_hashes"]
        prefixes[identifier] = prefix
    return prefixes


def slice_repair_recovery(contract_path, value, repo, events, recovery_path, source_path, final, historical_seals=None):
    """Account for accepted slice repairs without recreating a repo author phase."""
    run_path = Path(contract_path).parent
    run = run_path.resolve()
    live = historical_seals is None
    declaration = value.get("slice_schedules", {}).get(repo)
    require(declaration and events and events[-1][2]["status"] == "needs-fix",
            "Slice repair recovery requires a sliced repo in needs-fix")
    previous_path, previous = events[-1][1:]
    recovery_path = absolute_file(recovery_path, value)
    require(recovery_path.resolve().is_relative_to(run), "Slice repair recovery must be run-local")
    recovery = state.load(recovery_path)
    bound(value, repo, recovery)
    references = {key: recovery_reference(run, recovery.get(key), key, value)
                  for key in ("previous_state", "review", "slice_gate", "applied")}
    require(references["previous_state"] == str(previous_path) and references["review"] == previous["review_path"],
            "Slice repair recovery must bind the current needs-fix state and repo review")
    schedule_path = absolute_file(declaration["path"], value)
    schedule = state.load(schedule_path)
    items = {item["id"]: item for item in schedule["slices"]}
    gate = state.load(references["slice_gate"])
    bound(value, repo, gate)
    original_gate_path = absolute_file(previous.get("slice_gate_path"), value)
    original_gate = state.load(original_gate_path)
    bound(value, repo, original_gate)
    before = event_manifest(value, repo, previous)
    repo_review = state.load(references["review"])
    bound(value, repo, repo_review)
    require(repo_review.get("round") == previous.get("latest_review_round") and
            repo_review.get("source_fingerprint") == before["fingerprint"] and
            original_gate.get("source_fingerprint") == before["fingerprint"],
            "Slice repair recovery needs the original gate and review on the needs-fix source")
    for path in (original_gate_path, references["review"]):
        require(previous["artifact_hashes"].get(str(path)) == sha(path),
                "Original slice gate and repo review must be sealed by needs-fix")
    for proof_gate in (original_gate, gate):
        require(proof_gate.get("schedule_sha256") == declaration["sha256"] == sha(schedule_path),
                "Slice gate schedule changed")
        require(isinstance(proof_gate.get("artifact_hashes"), dict), "Slice gate is missing artifact seals")
        check_seals(proof_gate["artifact_hashes"], value)
    require(gate.get("source_fingerprint") == final["fingerprint"] and
            all(gate["artifact_hashes"].get(str(path)) == sha(path) for path in (source_path, previous_path)),
            "Slice gate is stale for needs-fix or the source capture. Run slice-gate again")
    require(Path(source_path).resolve().is_relative_to(run), "Recovery source manifest must be run-local")
    names = source_names(value, repo)
    require(before["head"] == final["head"], "Slice repair recovery cannot adopt a changed HEAD")
    changed = names.changed(before, final, missing=state.MISSING)
    require(changed, "Slice repair recovery needs a source change since the repo review")
    require(changed <= names.keys(previous["scope"]),
            "Slice repair recovery found changed frozen dependencies or source outside repo scope")
    if live:
        expected_gate = prepare_slice_gate(contract_path, repo, str(schedule_path),
                                           previous["initial_manifest"], source_path,
                                           previous.get("scope_amendment_path"))
        require(gate == expected_gate, "Slice gate differs from current acceptance proof. Run slice-gate again")
    histories = {identifier: history(run_path / repo / "slices" / state.relative_path(identifier), value)
                 for identifier in items}
    originals = gate_slice_histories(value, repo, original_gate, histories)
    current = gate_slice_histories(value, repo, gate, histories)
    if live:
        require(all(len(current[identifier]) == len(events) for identifier, events in histories.items()),
                "Slice gate is stale for current slice states. Run slice-gate again")
    plan = {"schedule_sha256": declaration["sha256"]}
    old_files, final_files = names.files(before["files"]), names.files(final["files"])
    reopened = {identifier for identifier in items if gate["slices"][identifier] != original_gate["slices"][identifier]}
    bindings = recovery.get("slices")
    require(isinstance(bindings, dict) and set(bindings) == reopened,
            "Slice repair recovery must bind every reaccepted slice")
    # A same-state repo grant may postdate the original repo review.
    review_event = next((event for _, _, event in reversed(events)
                         if event["status"] == "needs-fix" and not event.get("budget_grant_applied")), None)
    require(review_event and review_event["review_path"] == references["review"],
            "Slice repair recovery could not find the original repo review event")
    review_at = timestamp(review_event["at"])
    owned, edited_slices = set(), set()
    for identifier, prefix in current.items():
        _, old_path, old = originals[identifier][-1]
        _, accepted_path, accepted = prefix[-1]
        require(set(accepted["dependency_states"]) == set(items[identifier]["depends_on"]) and
                all(gate["slices"].get(dep) == digest for dep, digest in accepted["dependency_states"].items()),
                f"Slice {identifier} has stale dependency acceptance. Recheck and reaccept it")
        for event, files in ((old, old_files), (accepted, final_files)):
            accepted_files = event["accepted_files"]
            require(set(accepted_files) == set(event["touched"]) and
                    all(files.get(names.key(path), state.MISSING) == recorded
                        for path, recorded in accepted_files.items()),
                    f"Slice {identifier} source differs from its acceptance")
        if identifier not in reopened:
            continue
        binding = bindings[identifier]
        require(isinstance(binding, dict), f"Invalid recovery binding for slice {identifier}")
        checkpoint_path = recovery_reference(run, binding.get("checkpoint"), f"{identifier} checkpoint", value)
        require(recovery_reference(run, binding.get("previous_state"), f"{identifier} previous state", value) == str(old_path) and
                recovery_reference(run, binding.get("accepted_state"), f"{identifier} acceptance", value) == str(accepted_path),
                f"Stale slice recovery versions for {identifier}")
        suffix = prefix[len(originals[identifier]):]
        require(suffix, f"Slice {identifier} did not advance from the review gate")
        checkpoint = state.load(checkpoint_path)
        require(checkpoint.get("previous_state_path") == str(old_path),
                f"Slice {identifier} checkpoint is stale for the original acceptance")
        authorization_path = absolute_file(checkpoint.get("authorization_path"), value)
        authorization = state.load(authorization_path)
        require(timestamp(authorization.get("approved_at")) >= review_at,
                f"Slice {identifier} checkpoint authorization predates the repo review")
        target_delta = changed & names.keys(old["touched"])
        if target_delta:
            require(not owned & target_delta, "Slice repair recovery has overlapping changed file ownership")
            owned |= target_delta
            edited_slices.add(identifier)
        for sequence, _, event in suffix:
            prior_path, prior = prefix[sequence - 2][1:]
            require(all(event[field] == old[field] for field in ("touched", "author_agent_id", "base_manifest")),
                    f"Slice {identifier} changed scope, author or base during integration repair")
            require(event.get("integration_path") == checkpoint_path and
                    all(event["artifact_hashes"].get(str(path)) == sha(guard_path(path, value))
                        for path in (prior_path, checkpoint_path, event["source_manifest"])),
                    f"Slice {identifier} repair chain does not seal the supplied checkpoint and sources")
            boundary = timestamp(event["at"])
            require(timestamp(checkpoint.get("at")) <= boundary, "Integration checkpoint postdates its slice state")
            status, prior_status = event["status"], prior["status"]
            require(status in SLICE_TRANSITIONS.get(prior_status, set())
                    or recorded_binding_recheck(value, repo, event, prior, slice=True)
                    or (prior_status == "accepted" and status == "pre-review-passed"
                        and event.get("action") == "dependency-recheck")
                    or (prior_status == "capped" and status == "needs-fix" and event.get("is_continuation"))
                    or (prior_status == "capped" and status == "reviewed-with-findings"
                        and event.get("review_acceptance_path"))
                    or status == "capped" and prior_status == "needs-fix",
                    f"Slice {identifier} has an invalid transition before acceptance")
            siblings = {other: [entry for entry in history if timestamp(entry[2]["at"]) <= boundary]
                        for other, history in current.items() if other != identifier}
            snapshot = event_manifest(value, repo, event)
            integration_checkpoint(value, repo, contract_path, plan, items, identifier, prior,
                                   prefix[:sequence - 1], event, snapshot, sibling_histories=siblings)
        snapshot = event_manifest(value, repo, accepted)
        reviewed = prefix[-2][2]
        accepted_review = absolute_file(accepted.get("review_path"), value)
        require(accepted.get("review_path") == reviewed.get("review_path")
                and accepted["artifact_hashes"].get(str(accepted_review)) == sha(accepted_review)
                and review_source_matches(value, repo, reviewed, prefix[:-1], snapshot),
                f"Slice {identifier} acceptance requires a sealed review of the accepted source")
        snapshot_files = names.files(snapshot["files"])
        require(all(snapshot_files.get(names.key(path), state.MISSING) == recorded
                    for path, recorded in accepted["accepted_files"].items()),
                f"Slice {identifier} acceptance differs from its sealed source")
    require(changed == owned, "Slice repair recovery cannot attribute all source changes to authorized slices: "
            + ", ".join(sorted(changed - owned)))
    result, proofs = applied_findings(value, repo, references["applied"], references["review"], final, historical_seals)
    finding_slices = recovery.get("finding_slices")
    require(isinstance(finding_slices, dict) and set(finding_slices) == {item["id"] for item in result["applied"]},
            "Slice repair recovery must bind each applied finding to its changed slices")
    covered = set()
    for identifiers in finding_slices.values():
        require(isinstance(identifiers, list) and identifiers and all(isinstance(i, str) for i in identifiers)
                and set(identifiers) <= edited_slices
                and len(set(identifiers)) == len(identifiers), "Invalid applied finding slice scope")
        covered.update(identifiers)
    require(covered == edited_slices, "Slice repair recovery has changed slices without applied finding proof")
    return references, [str(recovery_path), *references.values(), *gate["artifact_hashes"], *proofs]


def batches(contract_path, repo):
    directory = Path(contract_path).parent / repo / "batches"
    found = {}
    for path in directory.glob("batch-*-*.json") if directory.is_dir() else []:
        match = re.fullmatch(r"batch-(\d+)-(start|barrier)\.json", path.name)
        require(match, f"Unexpected batch artifact: {path.name}")
        found.setdefault(int(match[1]), {})[match[2]] = path
    require(sorted(found) == list(range(1, len(found) + 1)), "Broken batch sequence")
    return directory, found


def batch_start(contract_path, repo, schedule_path, members, base_path, baseline_dir, scope, out):
    """Open one shared-checkout write window for a computed group; the orchestrator alone calls this."""
    value = contract(contract_path)
    require_unreserved(value, contract_path, repo)
    plan = slice_schedule(contract_path, repo, schedule_path)
    schedule = state.load(absolute_file(schedule_path))
    require(sorted(members) == sorted(next((g for g in plan["groups"] if set(members) <= set(g)), [])),
            "Members must be exactly one computed schedule group")
    by_id = {i["id"]: i for i in schedule["slices"]}
    root = Path(contract_path).parent / repo
    for identifier in members:
        require(not (root / "slices" / identifier).is_dir(), f"Slice already started: {identifier}")
        require(set(by_id[identifier]["depends_on"]) <= accepted_slices(contract_path, repo, set(by_id[identifier]["depends_on"])),
                f"Predecessors of {identifier} are not accepted")
    directory, found = batches(contract_path, repo)
    if found:
        last = found[max(found)]
        require("barrier" in last, "A batch is still open; close it at its barrier first")
        require(state.load(last["barrier"]).get("status") != "violation",
                "The previous batch ended in a violation; a human must resolve it")
    number = len(found) + 1
    require(Path(out).resolve() == (directory / f"batch-{number}-start.json").resolve(), "Invalid batch start path")
    baseline_dir = str(baseline_dir)
    require(Path(baseline_dir).is_absolute() and Path(baseline_dir).is_dir(), "Batch needs an absolute baseline directory")
    base = fresh(value, repo, base_path, scope)
    state.write_once(out, {"schema_version": VERSION, "run_id": value["run_id"], "repo": repo, "batch": number,
                           "members": sorted(members), "write_scopes": {i: by_id[i]["write_scope"] for i in members},
                           "base_manifest": str(base_path), "base_fingerprint": base["fingerprint"],
                           "baseline_dir": baseline_dir, "scope": list(scope), "schedule_sha256": plan["schedule_sha256"],
                           "at": datetime.now(timezone.utc).isoformat(), "status": "open",
                           "artifact_hashes": seal_files([contract_path, schedule_path, base_path])})


def batch_barrier(contract_path, repo, schedule_path, start_path, manifest_path, results_path, out):
    """Close a write window: all writers stopped, every change attributed to exactly one member, nothing else moved."""
    value = contract(contract_path)
    require_unreserved(value, contract_path, repo)
    plan = slice_schedule(contract_path, repo, schedule_path)
    start = state.load(absolute_file(start_path))
    bound(value, repo, start)
    check_seals(start["artifact_hashes"])
    directory, found = batches(contract_path, repo)
    number = start["batch"]
    require(found.get(number, {}).get("start") == Path(start_path) and "barrier" not in found[number]
            and number == max(found), "Barrier must close the latest open batch")
    require(Path(out).resolve() == (directory / f"batch-{number}-barrier.json").resolve(), "Invalid batch barrier path")
    require(start["schedule_sha256"] == plan["schedule_sha256"], "Schedule changed during batch")
    results = state.load(absolute_file(results_path))
    require(isinstance(results, list) and sorted(r.get("slice_id") for r in results) == start["members"],
            "A result is required for exactly every batch member")
    for result in results:
        state.text(result.get("agent_id"), "batch writer agent ID")
        require(result.get("status") in {"passed", "failed", "blocked", "interrupted"}, "Invalid writer status")
    base = manifest(value, repo, start["base_manifest"])
    final = fresh(value, repo, manifest_path, start["scope"])
    names = source_names(value, repo)
    spellings = names.spellings(base["files"].keys() | final["files"].keys())
    reported = {names.key(path): path for path in sorted(base["files"])}
    reported.update({names.key(path): path for path in sorted(final["files"])})
    changed = sorted(names.changed(base, final))
    owned, violations = {i: [] for i in start["members"]}, []
    if base.get("head") != final.get("head"):
        violations.append("git HEAD moved during the batch")
    for path in changed:
        owners = [i for i in start["members"]
                  if any(in_scope(name, start["write_scopes"][i], names) for name in spellings[path])]
        if len(owners) == 1:
            owned[owners[0]].append(reported[path])
        else:
            violations.append(f"{path}: " + ("no member owns it" if not owners else "several members own it"))
    if not violations and changed:
        try:
            delta = state.delta(value["repo_paths"][repo], start["baseline_dir"], [reported[p] for p in changed])
        except ValueError as error:
            violations.append(str(error))
        else:
            base_files = names.files(base["files"])
            for entry in delta["changes"]:
                if entry["before"] != base_files.get(names.key(entry["path"]), state.MISSING):
                    violations.append(f"{entry['path']}: baseline differs from the batch start state")
    record = {"schema_version": VERSION, "run_id": value["run_id"], "repo": repo, "batch": number,
              "members": start["members"], "base_manifest": start["base_manifest"], "final_manifest": str(manifest_path),
              "owned": owned, "results": {r["slice_id"]: r["status"] for r in results},
              "writers": {r["slice_id"]: r["agent_id"] for r in results}, "violations": violations,
              "status": "violation" if violations else ("closed" if all(r["status"] == "passed" for r in results)
                                                          else "closed-incomplete"),
              "at": datetime.now(timezone.utc).isoformat(),
              "artifact_hashes": seal_files([contract_path, start_path, manifest_path, results_path])}
    state.write_once(out, record)
    require(not violations, "Batch violation: " + "; ".join(violations))


SLICE_FIELDS = ("id", "title", "contributions", "write_scope", "depends_on", "interfaces", "targeted_checks",
                "resources", "parallel_allowed")


def scope_prefix(pattern):
    """Longest literal directory prefix of a path or glob; ambiguity resolves toward a conflict."""
    parts = []
    for part in state.relative_path(pattern).split("/"):
        if re.search(r"[*?\[\]{}]", part):
            break
        parts.append(part)
    return parts


def scopes_overlap(left, right):
    a, b = scope_prefix(left), scope_prefix(right)
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def slice_schedule(contract_path, repo, schedule_path):
    """Validate a human-approved slice schedule and return execution groups.

    Groups are computed from depends_on, never trusted from the proposal. A group may hold several slices
    only when every member allows parallelism, scopes and resources are disjoint and the approved
    max_parallel allows it; otherwise members run sequentially in separate groups.
    """
    _, plan = checked_slice_schedule(contract(contract_path), repo, schedule_path)
    return plan


def selected_slice(value, repo, slice_id=None, schedule_path=None):
    if slice_id is None:
        require(schedule_path is None, "--schedule needs --slice")
        return None
    require(schedule_path, "--slice needs --schedule")
    schedule, _ = checked_slice_schedule(value, repo, schedule_path)
    item = next((entry for entry in schedule["slices"] if entry["id"] == slice_id), None)
    require(item is not None, "Unknown slice")
    return item


def checked_slice_schedule(value, repo, schedule_path):
    """Validate the sealed schedule in the caller's contract and read policy."""
    require(repo in value["repos"], "Unknown slice repo")
    path = absolute_file(schedule_path, value)
    schedule = state.load(path)
    require(isinstance(schedule, dict) and schedule.get("schema_version") == VERSION
            and schedule.get("run_id") == value["run_id"] and schedule.get("repo") == repo,
            "Slice schedule is not bound to this run/repo")
    state.text(schedule.get("approved_by"), "slice schedule approver")
    require(schedule.get("approved") is True, "Slice schedule is not explicitly approved")
    timestamp(schedule.get("approved_at"))
    declared = value.get("slice_schedules", {}).get(repo)
    require(isinstance(declared, dict) and declared.get("path") == str(path) and declared.get("sha256") == sha(path),
            "Approved slice schedule must be declared in the contract with its hash")
    concurrency = schedule.get("max_parallel", 1)
    require(type(concurrency) is int and 1 <= concurrency <= 3, "max_parallel must be an integer from 1 to 3")
    items = schedule.get("slices")
    ids = unique_ids(items, "slices")
    require(ids, "Slice schedule cannot be empty")
    criteria = {c["id"] for c in value["criteria"] if repo in c["repos"]}
    mandatory = {c["id"] for c in value["criteria"] if repo in c["repos"] and c["mandatory"]}
    contributors = {}
    root = Path(value["repo_paths"][repo])
    exclusions = repo_read_excludes(value, repo)
    for item in items:
        state.relative_path(item["id"])
        for field in SLICE_FIELDS:
            require(field in item, f"Slice {item['id']} lacks {field}")
        state.text(item["title"], "slice title")
        require(type(item["parallel_allowed"]) is bool, "parallel_allowed must be boolean")
        for field in ("write_scope", "resources"):
            require(isinstance(item[field], list) and all(isinstance(x, str) and x for x in item[field]),
                    f"Slice {item['id']} {field} must be a list of strings")
        require(item["write_scope"], f"Slice {item['id']} needs a write_scope")
        for scope in item["write_scope"]:
            prefix = scope_prefix(scope)
            require(prefix or not exclusions, "Slice write scope intersects read_excludes")
            if prefix:
                guard_path(root.joinpath(*prefix), value, scope=True)
        require(isinstance(item["contributions"], list) and item["contributions"], "Slice needs contributions")
        for contribution in item["contributions"]:
            require(contribution.get("criterion_id") in criteria, "Contribution references unknown criterion")
            state.text(contribution.get("part"), "contribution part")
            contributors.setdefault(contribution["criterion_id"], set()).add(item["id"])
        require(isinstance(item["depends_on"], list) and set(item["depends_on"]) <= ids
                and item["id"] not in item["depends_on"], f"Slice {item['id']} has invalid depends_on")
        for check in item["targeted_checks"]:
            require(isinstance(check, dict), "targeted_checks entries must be objects")
            require(("command" in check) != ("argv" in check), "Slice check needs exactly one of command or argv")
            state.text(check.get("verification"), "slice check verification")
    require(mandatory <= set(contributors), "Mandatory criteria without any slice contribution: "
            + ", ".join(sorted(mandatory - set(contributors))))
    for deliverable in value.get("deliverables", {}).get(repo, []):
        scope = delivery_scope(deliverable)
        if scope and scope["scope"] == "slice":
            identifier = scope["slice_id"]
            require(identifier in ids and any(identifier in contributors.get(criterion, [])
                                               for criterion in deliverable["criterion_ids"]),
                    "Deliverable slice must exist and contribute to a linked criterion")
    for item in items:
        _, pending = deliverable_selection(value, repo, item)
        pending_paths = {entry["path"] for entry in value.get("deliverables", {}).get(repo, [])
                         if entry["id"] in pending and delivery_scope(entry)}
        require(not any(pending_paths.intersection(check.get("input_paths", []))
                        for check in value["pre_review_checks"][repo]),
                "Shared pre-review check uses a pending deliverable; use a required gate")
    cross = {cid for cid, owners in contributors.items() if len(owners) > 1}
    require(set(schedule.get("cross_slice_criteria", [])) == cross,
            "cross_slice_criteria must list exactly the criteria that several slices contribute to: "
            + ", ".join(sorted(cross)))
    # Topological levels (Kahn); a cycle leaves slices unplaced.
    remaining = {item["id"]: set(item["depends_on"]) for item in items}
    by_id = {item["id"]: item for item in items}
    levels, done = [], set()
    while remaining:
        ready = sorted(i for i, deps in remaining.items() if deps <= done)
        require(ready, "Slice dependencies contain a cycle")
        levels.append(ready)
        done |= set(ready)
        for i in ready:
            del remaining[i]
    groups = []
    for level in levels:
        pending = list(level)
        while pending:
            batch = [pending.pop(0)]
            for candidate in list(pending):
                if len(batch) >= concurrency:
                    break
                members = [by_id[i] for i in batch] + [by_id[candidate]]
                if all(m["parallel_allowed"] for m in members) and not any(
                        scopes_overlap(x, y) for a in members for b in members if a is not b
                        for x in a["write_scope"] for y in b["write_scope"]) and not any(
                        set(a["resources"]) & set(b["resources"]) for a in members for b in members if a is not b):
                    batch.append(candidate)
                    pending.remove(candidate)
            groups.append(batch)
    return schedule, {"groups": groups, "cross_slice_criteria": sorted(cross), "schedule_sha256": sha(path)}


def gate_budget_certificate(contract_path, value, repo, events, *, lineage_cache=None):
    certificate_path = guard_path(contract_path.parent / "completion.json", value)
    if not certificate_path.is_file():
        return None
    record = guarded_load(certificate_path, value)
    require(isinstance(record, dict), "Invalid completion certificate")
    require(record.get("schema_version") == VERSION and record.get("run_id") == value["run_id"]
            and record.get("workflow") == value["workflow"] and record.get("status") == "completed",
            "Invalid completion certificate identity")
    replacements = all_setup_replacements(value, contract_path, lineage_cache=lineage_cache)
    if replacements or "setup_replacements" in record:
        require(record.get("setup_replacements") == replacements, "Completion replacement chain mismatch")
        for replacement in replacements:
            require(record.get("artifact_hashes", {}).get(replacement["closure_path"]) == replacement["closure_sha256"]
                    and record.get("artifact_hashes", {}).get(replacement["contract_path"]) == replacement["contract_sha256"],
                    "Completion replacement seal mismatch")
            require(all(record.get("artifact_hashes", {}).get(path) == digest
                        for path, digest in replacement["files"].items()), "Completion setup artifact seal mismatch")
    repos = record.get("repos")
    require(isinstance(repos, dict) and set(value["repos"]) <= repos.keys(),
            "Completion certificate is missing a contract repository")
    candidate = repos[repo]
    require(isinstance(candidate, dict), "Invalid completion certificate repository")
    certified_state = candidate.get("state_path")
    require(isinstance(certified_state, str), "Completion certificate is missing its verified state")
    anchor = next(((path, event) for _, path, event in events if str(path) == certified_state), None)
    seals = record.get("artifact_hashes")
    require(isinstance(seals, dict) and anchor is not None and anchor[1]["status"] == "verified"
            and seals.get(certified_state) == guarded_sha(anchor[0], value)
            and seals.get(str(contract_path)) == guarded_sha(contract_path, value),
            "Completion certificate state/contract seal mismatch")
    state_dir = anchor[0].parent
    sealed_states = []
    for path in map(Path, seals):
        match = re.fullmatch(r"state-(\d+)\.json", path.name)
        if match and path.parent == state_dir:
            sealed_states.append((int(match[1]), str(path)))
    require(max(sealed_states)[1] == certified_state,
            "Completion certificate does not name its latest sealed repository state")
    has_budget = ("gate_fix_budget" in candidate or "gate_fix_history_sha256" in candidate
                  or "gate_fix_budget" in anchor[1])
    return candidate if has_budget else None


def validate_history(contract_path, repo, visited_contracts=None, read_excludes=(), *,
                     inherited_policy=(), lineage_cache=None, lineage_out=None):
    """Audit immutable lifecycle state and grant chains without recapturing today's source."""
    if lineage_cache is None:
        lineage_cache = {}
    contract_file = absolute_file(contract_path, {"_read_policy": inherited_policy})
    visited = set(visited_contracts or ())
    identity = guarded_resolve(contract_file, {"_read_policy": inherited_policy})
    require(identity not in visited,
            "Historical budget reference cycle/current-run ancestry detected")
    visited.add(identity)
    value = guarded_load(contract_file, {"_read_policy": inherited_policy})
    require(isinstance(value, dict) and value.get("schema_version") == VERSION
            and value.get("workflow") in {"ship", "fix"}, "Invalid historical workflow contract")
    require(contract_file.parent.name == value.get("run_id"), "Contract/run directory mismatch")
    require(repo in value.get("repos", []), "Unknown historical repo")
    if read_excludes:
        inherited_policy = (*inherited_policy, (value["repo_paths"][repo], read_excludes))
    value = with_read_policy(value, inherited_policy)
    guard_path(contract_file, value)
    if value.get("setup_replacement") is not None:
        replacement_chain(value, contract_file)
        pending = reserve_predecessors(value, contract_file, guarded_sha(contract_file, value), value,
                                       lineage_cache=lineage_cache)
        require(not pending, "Predecessor reservation is missing")
    continuity, _ = continuity_declarations(value, historical=True)
    events = history(contract_file.parent / state.relative_path(repo), value)
    require(events, "No lifecycle history")
    require(events[0][2].get("status") == "initialized", "Historical lifecycle must start initialized")
    expected_review_fixes, expected_gate_fixes, expected_round = 0, 0, 0
    prior_status = None
    for index, (_, _, event) in enumerate(events):
        status = event.get("status")
        require(status in set(TRANSITIONS) | TERMINAL, "Invalid historical lifecycle status")
        grant_same_state = (event.get("budget_grant_applied") is True and status == prior_status)
        grant_resumes_capped = (event.get("budget_grant_applied") is True and prior_status == "capped"
                                and index >= 2 and status == events[index - 2][2].get("status"))
        gate_correction = (status == prior_status == "gates-passed"
                           and bool(event.get("gate_correction_reason")))
        binding_recheck = index and recorded_binding_recheck(value, repo, event, events[index - 1][2])
        requirement_registration = (status == prior_status == "ready" and event.get("amendment_applied") is True
                                    and event.get("amendment_path") and event.get("source_fingerprint") ==
                                    events[index - 1][2].get("source_fingerprint"))
        if requirement_registration:
            amendment_path = absolute_file(event["amendment_path"], value)
            amendment = state.load(amendment_path)
            previous_path = events[index - 1][1]
            require(value["workflow"] == "ship" and amendment.get("approved_by") == "user"
                    and amendment.get("previous_state_path") == str(previous_path)
                    and amendment.get("previous_state_sha256") == sha(previous_path)
                    and event.get("artifact_hashes", {}).get(str(amendment_path)) == sha(amendment_path),
                    "Invalid historical requirement amendment registration")
        if prior_status is not None:
            require(status in TRANSITIONS.get(prior_status, set()) or status in TERMINAL
                    or (prior_status == "capped" and status == "reviewed-with-findings"
                        and event.get("review_acceptance_path"))
                    or grant_same_state or grant_resumes_capped or gate_correction or requirement_registration or binding_recheck,
                    "Invalid historical lifecycle transition")
        if status == "reviewed-with-findings" and not event.get("budget_grant_applied"):
            path = accept_review_findings(contract_file, value, repo, events[:index], event,
                                          manifest(value, repo, event["source_manifest"]))
            require(event.get("artifact_hashes", {}).get(path) == sha(path),
                    "Historical review acceptance is not sealed")
        if status == "edited-unverified" and not binding_recheck:
            action = event.get("action")
            require(action in {"review-fix", "plan-gap", "gate-fix"},
                    "Invalid historical repair action")
            if action in {"review-fix", "plan-gap"}:
                expected_review_fixes += 1
            else:
                expected_gate_fixes += 1
        if status in {"needs-fix", "reviewed"} and not event.get("budget_grant_applied"):
            expected_round += 1
        require(event.get("review_fix_count", 0) == expected_review_fixes,
                "Historical review-fix counter is not derived from repair events")
        require(event.get("gate_fix_count", 0) == expected_gate_fixes,
                "Historical gate-fix counter is not derived from repair events")
        require(event.get("latest_review_round", 0) == expected_round,
                "Historical review round is not derived from review events")
        require(event.get("review_fix_count", 0) <= event.get("review_fix_limit", REVIEW_FIX_BUDGET),
                "Historical review-fix counter exceeds its recorded limit")
        prior_status = status
    inherited = {}
    for index, (_, _, event) in enumerate(events):
        bound(value, repo, event)
        check_event_seals(value, repo, event, inherited)
        inherited = event.get("artifact_hashes", {})
        recovery_path = event.get("slice_repair_recovery_path")
        if recovery_path:
            require(index and event["status"] == "edited-unverified" and event["action"] == "review-fix",
                    "Invalid historical slice repair recovery transition")
            previous = events[index - 1][2]
            for key in ("initial_manifest", "baseline_dir", "scope_path", "scope", "source_scope_path",
                        "source_scope", "excludes", "original_agent_id", "current_author_agent_id",
                        "slice_authors", "review_path", "review_fingerprint", "scope_amendment_path"):
                require(event.get(key) == previous.get(key), f"Historical recovery changed {key}")
            references, seals = slice_repair_recovery(
                contract_file, value, repo, events[:index], recovery_path, event["source_manifest"],
                event_manifest(value, repo, event), historical_seals=inherited)
            require(event.get("slice_gate_path") == references["slice_gate"]
                    and event.get("applied_path") == references["applied"]
                    and set(seals) <= inherited.keys(),
                    "Historical slice repair recovery proof is not sealed")
    certificate = gate_budget_certificate(contract_file, value, repo, events, lineage_cache=lineage_cache)
    lineage = GateFixLineage(value, repo, lineage_cache)
    limit, grants = review_budget(events, contract_file, value, repo, visited,
                                  gate_lineage=lineage, certificate=certificate)
    latest = events[-1][2]
    for item in continuity:
        if item.get("repo") == repo and item.get("slice_id") is not None:
            budget_continuity(value, repo, contract_file, item["slice_id"], visited_contracts=visited)
    if latest.get("scope_amendment_path"):
        scope_amendment(contract_path, value, repo, latest["scope_amendment_path"], events,
                        manifest(value, repo, latest["initial_manifest"]))
    result = {"run_id": value["run_id"], "repo": repo, "latest_status": latest["status"],
            "review_fix_count": latest.get("review_fix_count", 0), "review_fix_limit": limit,
            "state_path": str(events[-1][1]), "state_sha256": sha(events[-1][1]),
            "granted_additional_passes": sum(item["additional_passes"] for item in grants),
            "grants": grants,
            "gate_fix_budget": lineage.summary(expected_gate_fixes),
            "history_sha256": lineage.history_hash(sha(contract_file),
                                                   [(path.name, sha(path)) for _, path, _ in events]),
            "states": len(events), "status": "valid"}
    if historical_budget_used(grants):
        result["historical_used_passes"] = historical_budget_used(grants)
    if lineage_out is not None:
        lineage_out.update(lineage.runs)
    return result


def pending_slice_recovery(events):
    if events and events[-1][2]["status"] in {"edited-unverified", "pre-review-passed"}:
        for _, _, event in reversed(events):
            if event["status"] in {"implemented", "edited-unverified"}:
                return event if event.get("slice_repair_recovery_path") else None
    return None


def review_budget_status(contract_path, repo, slice_id=None):
    """Read-only pre-dispatch budget check bound to the repo's latest immutable state."""
    contract_file = absolute_file(contract_path)
    value = contract(contract_file)
    require(repo in value["repos"], "Unknown review-budget repo")
    check_related_budgets(value, repo, contract_file)
    directory = contract_file.parent / repo
    if slice_id is not None:
        schedules = value.get("slice_schedules", {})
        require(repo in schedules, "Repository has no slice schedule")
        plan = slice_schedule(contract_file, repo, schedules[repo]["path"])
        require(any(slice_id in group for group in plan["groups"]), "Unknown slice")
        directory = directory / "slices" / slice_id
    events = history(directory)
    require(events, "No lifecycle history")
    inherited = {}
    for _, path, event in events:
        bound(value, repo, event)
        if slice_id is not None:
            require(event.get("slice_id") == slice_id and event.get("schedule_sha256") == plan["schedule_sha256"],
                    "Slice history belongs to a different slice or schedule")
        check_event_seals(value, repo, event, inherited)
        inherited = event.get("artifact_hashes", {})
    lineage = GateFixLineage(value, repo) if slice_id is None else None
    limit, grants = (review_budget(events, contract_file, value, repo, gate_lineage=lineage) if slice_id is None
                     else (events[-1][2].get("review_fix_limit", SLICE_REVIEW_FIX_BUDGET), []))
    latest_path, latest = events[-1][1:]
    scope = (sorted(set(latest.get("scope", [])) | set(latest.get("source_scope", [])))
             if slice_id is None else latest.get("touched", []))
    current = state.capture(value["repo_paths"][repo], scope, latest.get("excludes", []),
                            repo_read_excludes(value, repo), policies=read_policy(value))
    recorded = manifest(value, repo, latest["source_manifest"])
    changed = sorted(source_names(value, repo).changed(recorded, current, missing=state.MISSING))
    source_matches = not changed and recorded["head"] == current["head"]
    context = latest
    if latest.get("status") == "capped" and len(events) >= 2:
        context = events[-2][2]
    findings = []
    review_path = context.get("review_path")
    recovery = pending_slice_recovery(events) if slice_id is None else None
    if review_path and (context.get("status") == "needs-fix" or recovery):
        findings = state.load(absolute_file(review_path))["findings"]
    used = latest.get("review_fix_count", 0)
    historical_used = historical_budget_used(grants)
    result = {"run_id": value["run_id"], "repo": repo, "state_path": str(latest_path),
            "state_sha256": sha(latest_path), "latest_status": latest["status"],
            "limit": limit, "used_passes": used, "remaining_passes": max(0, limit - used),
            "exhausted": used >= limit, "open_finding_ids": sorted(item["id"] for item in findings),
            "open_findings": findings,
            "findings_by_severity": {severity: sum(item.get("severity") == severity for item in findings)
                                     for severity in ("Critical", "Warning", "Suggestion")},
            "source_matches_state": source_matches, "changed_source_paths": changed,
            "grants": grants, "status": "valid"}
    if slice_id is not None:
        result["slice_id"] = slice_id
    elif latest["status"] == "needs-fix" and not source_matches and repo in value.get("slice_schedules", {}):
        result["next_step"] = (
            "HEAD changed since the repo review. Reconcile HEAD before recovery."
            if recorded["head"] != current["head"] else
            "For authorized and reaccepted slice repairs, run slice-gate and submit slice_repair_recovery_path "
            "via transition. Follow the repo review fix recovery procedure in PROTOCOL.md."
            + (" A user-approved budget grant bound to the recovery is required." if used >= limit else ""))
    if lineage is not None:
        result["gate_fix_budget"] = lineage.summary(latest.get("gate_fix_count", 0))
    continuity = budget_continuity(value, repo, contract_file, slice_id)
    if continuity:
        result.update({"historical_used_passes": continuity["used_passes"],
                       "historical_effective_limit": continuity["effective_limit"],
                       "cumulative_used_passes": continuity["used_passes"] + used,
                       "continuity": continuity})
    if historical_used:
        result.update({"historical_used_passes": historical_used,
                       "cumulative_used_passes": historical_used + used})
    return result


def complete(contract_path, out):
    value = contract(contract_path)
    require_unreserved(value, contract_path)
    root = Path(contract_path).parent
    require(Path(out).resolve() == (root / "completion.json").resolve(), "Invalid completion path")
    repos = {}
    lineages = {}
    certified_state_hashes = {}
    paths = [contract_path]
    for applied in value.get("_amendments", []):
        paths.append(applied["path"])
    for repo in value["repos"]:
        check_related_budgets(value, repo, contract_path)
        events = history(root / repo)
        require(events and events[-1][2]["status"] == "verified", f"Repo not verified: {repo}")
        latest = events[-1][2]
        audit = validate_history(contract_path, repo, lineage_cache=lineages)
        require(audit["state_path"] == str(events[-1][1]) and audit["state_sha256"] == sha(events[-1][1]),
                "Completion state changed during history audit")
        certified_state_hashes[repo] = audit["state_sha256"]
        effective_limit, grants = audit["review_fix_limit"], audit["grants"]
        inherited_seals = {}
        for _, _, event in events:
            check_event_seals(value, repo, event, inherited_seals)
            inherited_seals = event.get("artifact_hashes", {})
        if latest.get("scope_amendment_path"):
            scope_amendment(contract_path, value, repo, latest["scope_amendment_path"], events,
                            manifest(value, repo, latest["initial_manifest"]))
        final = fresh(value, repo, latest["source_manifest"],
                      sorted(set(latest["scope"]) | set(latest["source_scope"])), latest["excludes"])
        # Revalidate prior repos after all consumers and checker activity.
        delivery, delivery_paths = deliverable_binding(value, repo, latest.get("deliverable_manifest_path"))
        paths += delivery_paths + recheck_pre_review(value, repo, latest, final, delivery)
        paths += check_slice_delivery(value, repo, latest.get("slice_gate_path"), latest.get("deliverable_manifest_path"))
        if delivery is not None:
            match_delivery_reference(state.load(absolute_file(latest["review_path"], value)), delivery)
        paths += check_set(value, repo, "required_gates", latest["gate_evidence_paths"], final, delivery=delivery)
        paths += assessment(value, repo, latest["assessment_path"], final,
                            manifest(value, repo, latest["initial_manifest"]), delivery=delivery)
        used = latest.get("review_fix_count", 0)
        grants_total = sum(item["additional_passes"] for item in grants)
        continuity = budget_continuity(value, repo, contract_path)
        carried_over = continuity["used_passes"] if continuity else historical_budget_used(grants)
        budget_summary = {"standard_limit": REVIEW_FIX_BUDGET,
                          "granted_additional_passes": grants_total,
                          "effective_limit": effective_limit, "used_passes": used,
                          "extra_passes_used": max(0, carried_over + used - REVIEW_FIX_BUDGET),
                          "grants": grants}
        if carried_over:
            budget_summary.update({"historical_used_passes": carried_over,
                                   "cumulative_used_passes": carried_over + used})
        if continuity:
            budget_summary.update({"historical_effective_limit": continuity["effective_limit"],
                                   "continuity": continuity})
        open_findings = []
        if latest.get("review_path"):
            for finding in state.load(absolute_file(latest["review_path"]))["findings"]:
                open_findings.append({**finding, "scope": "repo",
                                      "review_acceptance_path": latest.get("review_acceptance_path")})
        slice_inputs = {}
        slices_root = root / repo / "slices"
        if slices_root.is_dir():
            for directory in sorted(p for p in slices_root.iterdir() if p.is_dir()):
                slice_events = history(directory)
                if not slice_events:
                    continue
                slice_latest = slice_events[-1][2]
                if has_bound_pre_review(value, repo) and slice_latest.get("status") == "accepted":
                    inputs = check_input_status(value, repo, slice_latest)
                    require(all(item["status"] == "verified" for item in inputs), "Completion slice check input is stale")
                    if inputs:
                        slice_inputs[directory.name] = inputs
                inherited = {}
                for _, _, event in slice_events:
                    check_event_seals(value, repo, event, inherited)
                    inherited = event.get("artifact_hashes", {})
                if slice_latest.get("review_path"):
                    paths.extend([str(slice_events[-1][1]), *slice_latest.get("artifact_hashes", {})])
                    for finding in state.load(absolute_file(slice_latest["review_path"]))["findings"]:
                        open_findings.append({**finding, "scope": "slice/" + directory.name,
                                              "review_acceptance_path": slice_latest.get("review_acceptance_path")})
        repos[repo] = {"source_fingerprint": final["fingerprint"], "state_path": str(events[-1][1]),
                       "review_budget": budget_summary, "open_findings": open_findings,
                       "gate_fix_budget": audit["gate_fix_budget"], "gate_fix_history_sha256": audit["history_sha256"]}
        if value.get("deliverables", {}).get(repo):
            deliveries = delivery_status(value, repo, latest, recheck=False)
            require(all(item["status"] == "verified" for item in deliveries), "Completion deliverable is stale")
            repos[repo]["deliverables"] = deliveries
        if value.get("external_stores"):
            inputs = check_input_status(value, repo, latest)
            require(all(item["status"] == "verified" for item in inputs), "Completion check input is stale")
            repos[repo]["check_inputs"] = inputs
            if slice_inputs:
                repos[repo]["slice_check_inputs"] = slice_inputs
        paths += [str(events[-1][1]), *latest["artifact_hashes"]]
    if value.get("telemetry_policy", {}).get("run_local_seals"):
        require_run_local(value, root, paths)
    replacements = all_setup_replacements(value, contract_path, lineage_cache=lineages)
    for replacement in replacements:
        paths.extend([replacement["closure_path"], *replacement["files"]])
    for repo, entry in repos.items():
        current = history(root / repo)
        require(current and str(current[-1][1]) == entry["state_path"]
                and sha(current[-1][1]) == certified_state_hashes[repo], "Completion state changed during checks")
    check_gate_sources_current(lineages)
    require(all_setup_replacements(value, contract_path, lineage_cache=lineages) == replacements,
            "Setup replacement changed during completion")
    seals = seal_files(paths, value)
    if value.get("external_stores"):
        confirm_completion_versions(value, repos)
    state.write_once(out, {"schema_version": VERSION, "run_id": value["run_id"],
                          "workflow": value["workflow"], "status": "completed",
                          "amendment": value.get("_amendment"),
                          "amendments": [dict(applied) for applied in value.get("_amendments", [])],
                          "at": datetime.now(timezone.utc).isoformat(), "repos": repos,
                          **({"setup_replacements": replacements} if replacements else {}),
                          "artifact_hashes": seals})


def packet(contract_path, repo, scope_path, baseline_dir, out):
    value = contract(contract_path)
    require_unreserved(value, contract_path, repo)
    require(repo in value["repos"], "Unknown packet repo")
    scope = scope_input(Path(scope_path).absolute(), value)
    directory = Path(out)
    require(directory.is_absolute() and guard_path(directory, value).resolve().is_relative_to(Path(contract_path).parent.resolve()),
            "Review packet must remain in run directory")
    require(not directory.exists(), "Review packet directory already exists")
    events = history(Path(contract_path).parent / repo)
    previous_state = events[-1][2] if events else {}
    source_scope = sorted(set(scope) | set(previous_state.get("source_scope", [])))
    excludes = previous_state.get("excludes", [])
    read_excludes = repo_read_excludes(value, repo)
    current = state.capture(value["repo_paths"][repo], source_scope, excludes, read_excludes,
                            policies=read_policy(value))
    changes = state.delta(value["repo_paths"][repo], baseline_dir, scope)
    state.match(current, state.capture(value["repo_paths"][repo], source_scope, excludes, read_excludes,
                                       policies=read_policy(value)))
    reviews = [e[2] for e in events if e[2]["status"] in {"needs-fix", "reviewed"}
               and not e[2].get("budget_grant_applied")]
    previous = manifest(value, repo, reviews[-1]["source_manifest"]) if reviews else None
    changed = sorted(source_names(value, repo).changed(previous, current)
                     if previous else current["files"])
    state.write_once(directory / "source.json", current)
    state.write_once(directory / "delta.json", changes)
    index = {
        "schema_version": VERSION, "run_id": value["run_id"], "repo": repo,
        "source_manifest": str(directory / "source.json"), "delta_path": str(directory / "delta.json"),
        "scope": scope, "changed_since_review": changed, "criteria": criteria_for(value, repo),
        "required_passes": value["required_passes"][repo],
        "prior_reviews": [r["review_path"] for r in reviews],
        "open_findings": state.load(reviews[-1]["review_path"])["findings"] if reviews else [],
    }
    recovery = pending_slice_recovery(events)
    if recovery:
        index["slice_repair_recovery_path"] = recovery["slice_repair_recovery_path"]
        index["applied_path"] = recovery["applied_path"]
    state.write_once(directory / "index.json", index)


def record_evidence(contract_path, repo, request_path, out):
    value = contract(contract_path)
    record = guarded_load(absolute_file(request_path, value), value)
    return record_evidence_record(contract_path, repo, record, out)


def prepare_evidence_record(contract_path, repo, record):
    """Validate one evidence record and seal its artifacts without writing it yet."""
    value = contract(contract_path)
    require_unreserved(value, contract_path, repo)
    require(repo in value["repos"], "Unknown evidence repo")
    require(isinstance(record, dict), "Evidence input must be an object")
    prepared = dict(record)
    if any(key in prepared for key in ("schema_version", "run_id", "repo")):
        bound(value, repo, prepared)
    prepared.update(schema_version=VERSION, run_id=value["run_id"], repo=repo)
    claims = prepared.get("claims", [])
    require(claims, "Evidence needs claims")
    unique_ids(claims, "evidence claims")
    claimed = {c["id"] for c in claims}
    suffixes = [requirement_amendment_suffix(value, identifier) for identifier in claimed
                if requirement_amendment_suffix(value, identifier)]
    if suffixes:
        suffix = set.intersection(*suffixes)
        require(prepared.get("amendment_sha256") in suffix, "Evidence lacks amendment binding")
    prepared["criterion_id"] = claims[0]["id"]
    final = manifest(value, repo, prepared["after_manifest"])
    state.evidence(prepared, final, repo_read_excludes(value, repo), guard=lambda p: guard_path(p, value),
                   loader=lambda p: guarded_load(p, value), file_check=lambda p: bool(absolute_file(p, value)))
    for path in prepared["artifact_paths"]:
        absolute_file(path, value)
    execution_binding(prepared, value)
    validate_input_binding(value, repo, prepared)
    validate_delivery_reference(value, repo, prepared)
    prepared["artifact_hashes"] = seal_files([
        prepared["before_manifest"], prepared["after_manifest"], *prepared["artifact_paths"]], value)
    return prepared


def record_evidence_record(contract_path, repo, record, out):
    prepared = prepare_evidence_record(contract_path, repo, record)
    value = contract(contract_path)
    output = guarded_resolve(out, value)
    require(output.is_relative_to(guarded_resolve(contract_path, value).parent),
            "Evidence must remain in run directory")
    state.write_once(output, prepared, pinned=True)
    return prepared


def resolve_batch_record(entry):
    """Turn one batch entry into an evidence record dict (from-file or inline fields)."""
    require(isinstance(entry, dict), "Batch evidence record must be an object")
    if "file" in entry or "from_file" in entry:
        source = entry.get("file", entry.get("from_file"))
        return state.load(absolute_file(source))
    if "record" in entry:
        require(isinstance(entry["record"], dict), "Batch evidence record.record must be an object")
        return entry["record"]
    claim_id = entry.get("claim_id")
    claim_assertion = entry.get("claim_assertion")
    require(claim_id and claim_assertion,
            "Batch evidence needs file/record or claim_id/claim_assertion")
    record = {"claims": [{"id": claim_id, "assertion": claim_assertion}]}
    for key in ("kind", "producer", "status", "command", "exit_code", "assertion",
                "environment", "data_scope", "before_manifest", "after_manifest", "reason"):
        if entry.get(key) is not None:
            record[key] = entry[key]
    artifacts = entry.get("artifact_paths") or entry.get("artifact") or entry.get("artifacts")
    if artifacts:
        require(isinstance(artifacts, list), "Batch evidence artifacts must be a list")
        record["artifact_paths"] = artifacts
    return record


def record_evidence_batch(contract_path, records, stop_on_fail=False):
    """Validate every batch record before writing any of them; no silent partial success."""
    require(isinstance(records, list) and records, "Batch evidence needs a non-empty records array")
    results, prepared, failed = [], [], False
    for index, entry in enumerate(records):
        identifier = entry.get("id") if isinstance(entry, dict) else None
        try:
            require(isinstance(entry, dict), "Batch evidence record must be an object")
            repo = entry.get("repo")
            require(isinstance(repo, str) and repo, "Batch evidence record needs repo")
            out = entry.get("out")
            require(isinstance(out, str) and Path(out).is_absolute(),
                    "Batch evidence record needs an absolute out path")
            value = contract(contract_path)
            output = guarded_resolve(out, value)
            require(output.is_relative_to(guarded_resolve(contract_path, value).parent),
                    "Evidence must remain in run directory")
            require(not guarded_exists(output, value), f"Evidence output already exists: {out}")
            record = resolve_batch_record(entry)
            identifier = record.get("criterion_id") or \
                (record["claims"][0]["id"] if isinstance(record.get("claims"), list) and record["claims"] else None)
            prepared_record = prepare_evidence_record(contract_path, repo, record)
            identifier = prepared_record["criterion_id"]
            prepared.append((output, prepared_record))
            results.append({"id": identifier, "status": "ok", "path": out, "error": None})
        except (ValueError, OSError, KeyError, TypeError) as error:
            failed = True
            results.append({"id": identifier or (entry.get("repo") if isinstance(entry, dict) else None)
                            or f"record-{index}",
                            "status": "failed",
                            "path": entry.get("out") if isinstance(entry, dict) else None,
                            "error": str(error)})
            if stop_on_fail:
                break
    for out, record in prepared:
        state.write_once(out, record, pinned=True)
    return results, not failed


def phase(contract_path, request_path, out):
    return phase_from_record(contract_path, state.load(absolute_file(request_path)), out)


def phase_from_flags(contract_path, out, *, phase_id, phase, event, repo=None, status=None,
                     end_reason=None, agent_id=None, round=None, attempt=None, session_id=None, at=None,
                     action=None, previous_state_path=None, previous_state_sha256=None,
                     checkpoint_path=None, capsule_path=None, slice_id=None, gate_results=None):
    record = {"phase_id": phase_id, "phase": phase, "event": event, "repo": repo,
              "at": at or datetime.now(timezone.utc).isoformat()}
    for key, value in (("status", status), ("end_reason", end_reason), ("agent_id", agent_id),
                       ("round", round), ("attempt", attempt), ("session_id", session_id), ("action", action),
                       ("previous_state_path", previous_state_path),
                       ("previous_state_sha256", previous_state_sha256),
                       ("checkpoint_path", checkpoint_path), ("capsule_path", capsule_path), ("slice_id", slice_id)):
        if value is not None:
            record[key] = value
    if gate_results is not None:
        record["gate_results"] = gate_results
    return phase_from_record(contract_path, record, out)


def phase_from_record(contract_path, record, out):
    value = contract(contract_path)
    require(isinstance(record, dict), "Telemetry request must be an object")
    require("checkpoint_sha256" not in record, "Checkpoint hash is generated by phase")
    require_unreserved(value, contract_path, record.get("repo"))
    record.setdefault("run_id", value["run_id"])
    record.setdefault("workflow", value["workflow"])
    require(record.get("run_id") == value["run_id"] and record.get("workflow") == value["workflow"],
            "Wrong telemetry run/workflow")
    require(record.get("event") in {"phase-start", "phase-end"}, "Invalid phase event")
    require(record.get("phase") in {"implement", "review-fix", "gate-fix", "review", "checker",
                                     "gates", "preflight", "pre-review", "checkpoint-wait"}, "Invalid phase")
    identifier = record.get("phase_id")
    require(isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", identifier)
            and identifier not in {".", ".."}, "Invalid phase ID")
    require(record.get("repo") in value["repos"] or record.get("repo") is None, "Unknown phase repo")
    timestamp(record.get("at"))
    explicit_session = record.get("session_id")
    if isinstance(explicit_session, str) and explicit_session:
        require(re.fullmatch(r"[A-Za-z0-9_-]+", explicit_session), "Invalid phase session_id")
        record["session_id"] = explicit_session
    else:
        env_session = os.environ.get("CODEX_SESSION_ID") or os.environ.get("CODEX_THREAD_ID")
        record["session_id"] = (env_session if isinstance(env_session, str)
                                and re.fullmatch(r"[A-Za-z0-9_-]+", env_session) else "")
    directory = Path(contract_path).parent / "telemetry"
    name = record["phase_id"] + ("-start.json" if record["event"] == "phase-start" else "-end.json")
    require(Path(out).resolve() == (directory / name).resolve(), "Invalid telemetry output path")
    slice_id = record.get("slice_id")
    if slice_id is not None:
        state.relative_path(slice_id)
        require(record.get("repo") in value.get("slice_schedules", {}), "Slice telemetry requires a scheduled repo")
        schedule = value["slice_schedules"][record["repo"]]
        slice_schedule(contract_path, record["repo"], schedule.get("path"))
        require(any(item.get("id") == slice_id for item in state.load(schedule["path"])["slices"]),
                "Unknown telemetry slice")
    repair = (value.get("telemetry_policy", {}).get("repair_phase_required", False)
              and record["phase"] in {"review-fix", "gate-fix"} and not value.get("from_review")
              and slice_id is None)
    if record["phase"] == "gate-fix" and slice_id is None and record["event"] == "phase-start":
        repo = record.get("repo")
        require(repo in value["repos"], "Gate-fix phase requires a repo")
        check_related_budgets(value, repo, contract_path)
        audited = validate_history(contract_path, repo)
        require(audited["gate_fix_budget"]["remaining_attempts"] > 0, "Gate-fix budget exhausted")
    if repair and record["event"] == "phase-start":
        repo = record.get("repo")
        require(repo in value["repos"], "Repair phase requires a repo")
        events = history(Path(contract_path).parent / repo)
        require(events, "Repair phase requires lifecycle history")
        previous_path, previous = events[-1][1:]
        action = record.get("action")
        require(action in ({"gate-fix"} if record["phase"] == "gate-fix" else {"review-fix", "plan-gap"}),
                "Repair phase action mismatch")
        require(record.get("previous_state_path") == str(previous_path)
                and record.get("previous_state_sha256") == sha(previous_path), "Repair phase previous state mismatch")
        require(timestamp(previous["at"]) <= timestamp(record["at"]) <= datetime.now(timezone.utc),
                "Repair phase start time is stale or future")
        counter = "attempt" if action == "gate-fix" else "round"
        expected_counter = previous.get("gate_fix_count", 0) + 1 if action == "gate-fix" else previous.get("latest_review_round", 0)
        require(record.get(counter) == expected_counter, "Repair phase counter mismatch")
        author = previous.get("current_author_agent_id") or previous.get("original_agent_id")
        state.text(author, "current author")
        require(record.get("agent_id") == author, "Repair phase author mismatch")
        if value.get("author_rotation", "resume") == "capsule":
            capsule_path = run_local_evidence_path(record.get("capsule_path"), Path(contract_path).parent, value,
                                                   "Repair capsule")
            capsule = guarded_load(capsule_path, value)
            bound(value, repo, capsule)
            require(capsule.get("written_by") == author
                    and capsule.get("source_fingerprint") == previous.get("source_fingerprint"),
                    "Repair phase capsule author/source mismatch")
            capsule_manifest_path = run_local_evidence_path(capsule.get("manifest_path"), Path(contract_path).parent,
                                                           value, "Repair capsule manifest")
            capsule_manifest = state.checked_manifest(capsule_manifest_path, loader=lambda path: guarded_load(path, value))
            require(capsule_manifest["fingerprint"] == previous.get("source_fingerprint"),
                    "Repair phase capsule manifest mismatch")
    if record["event"] == "phase-end":
        start_path = directory / (record["phase_id"] + "-start.json")
        start = state.load(absolute_file(start_path))
        require(all(record.get(k) == start.get(k) for k in ("run_id", "workflow", "repo", "phase", "session_id", "slice_id")),
                "Phase end/start mismatch")
        if repair:
            require(all(record.get(k) == start.get(k) for k in
                        ("action", "previous_state_path", "previous_state_sha256", "round", "attempt", "capsule_path")),
                    "Repair phase end/start binding mismatch")
            if record.get("status") == "passed":
                state.text(record.get("agent_id"), "repair author")
                if start.get("capsule_path"):
                    require(record["agent_id"] != start.get("agent_id"), "Fresh repair must use a new author")
                else:
                    require(record["agent_id"] == start.get("agent_id"), "Repair author changed without a capsule")
        require(timestamp(record["at"]) >= timestamp(start["at"]), "Negative phase duration")
        require(record.get("status") in {"passed", "failed", "blocked", "interrupted"}, "Invalid phase status")
        reason = record.get("end_reason")
        require(reason is None or reason in END_REASONS, "Invalid phase end_reason")
        if value.get("telemetry_policy", {}).get("end_reason_required") and record["status"] != "passed":
            require(reason is not None, "Non-passed phase needs end_reason (use unknown when not established)")
        checkpoint = record.get("checkpoint_path")
        require(checkpoint is None or (isinstance(checkpoint, str) and Path(checkpoint).is_absolute()),
                "checkpoint_path must be absolute")
        if checkpoint is not None and record["phase"] in {"review-fix", "gate-fix"}:
            checkpoint_path = guarded_resolve(checkpoint, value)
            run_root = guarded_resolve(Path(contract_path).parent, value)
            if checkpoint_path.is_relative_to(run_root) and guarded_exists(checkpoint, value):
                record["checkpoint_sha256"] = guarded_sha(absolute_file(checkpoint, value), value)
        record["start_sha256"] = sha(start_path)
    if (record["phase"] in {"review-fix", "gate-fix"} and record.get("repo") in value["repos"]
            and record.get("previous_state_path") and record.get("previous_state_sha256")):
        previous_path = run_local_evidence_path(record["previous_state_path"], Path(contract_path).parent, value,
                                                "Repair predecessor state")
        require(guarded_sha(previous_path, value) == record["previous_state_sha256"],
                "Repair phase source capture predecessor mismatch")
        previous = guarded_load(previous_path, value)
        bound(value, record["repo"], previous)
        require(previous.get("slice_id") == slice_id, "Repair phase source capture slice mismatch")
        scope = sorted(set(previous.get("scope", previous.get("touched", []))) |
                       set(previous.get("source_scope", [])))
        coverage = None
        enumerated = None
        if slice_id is not None:
            declaration = value["slice_schedules"][record["repo"]]
            schedule, _ = checked_slice_schedule(value, record["repo"], declaration["path"])
            item = next(entry for entry in schedule["slices"] if entry["id"] == slice_id)
            coverage = item["write_scope"]
            if not guarded_exists(Path(value["repo_paths"][record["repo"]]) / ".git", value):
                enumerated = slice_capture_paths(value, record["repo"], coverage)
                scope = sorted(set(scope) | set(enumerated))
            if record["event"] == "phase-end":
                scope = sorted(set(scope) | set(start.get("repair_source", {}).get("manifest", {}).get("files", {})))
        capture = state.capture(value["repo_paths"][record["repo"]], scope, previous.get("excludes", []),
                                repo_read_excludes(value, record["repo"]), policies=read_policy(value))
        if enumerated is not None:
            require(slice_capture_paths(value, record["repo"], coverage) == enumerated,
                    "Slice source scope changed during capture; stabilize and retry")
        record["repair_source"] = {"observed_at": datetime.now(timezone.utc).isoformat(),
                                   "contract_sha256": guarded_sha(contract_path, value), "manifest": capture}
        if record.get("capsule_path"):
            capsule_path = run_local_evidence_path(record["capsule_path"], Path(contract_path).parent, value,
                                                   "Repair capsule")
            record["capsule_sha256"] = guarded_sha(capsule_path, value)
            capsule = guarded_load(capsule_path, value)
            capsule_manifest_path = run_local_evidence_path(capsule.get("manifest_path"), Path(contract_path).parent,
                                                           value, "Repair capsule manifest")
            record["capsule_manifest_sha256"] = guarded_sha(capsule_manifest_path, value)
        if coverage is not None:
            record["repair_source"]["scope_coverage"] = coverage
        if record["event"] == "phase-start":
            if record.get("repair_recovery_path"):
                recovery_path = run_local_evidence_path(record["repair_recovery_path"], Path(contract_path).parent,
                                                        value, "Partial repair recovery")
                record["repair_recovery_sha256"] = guarded_sha(recovery_path, value)
            prior = {}
            for name in guarded_names(directory, value):
                if not name.endswith(("-start.json", "-end.json")):
                    continue
                path = directory / name
                boundary = guarded_load(path, value)
                if (boundary.get("phase") in {"review-fix", "gate-fix"}
                        and boundary.get("repo") == record["repo"] and boundary.get("slice_id") == slice_id
                        and boundary.get("previous_state_path") == str(previous_path)
                        and boundary.get("previous_state_sha256") == record["previous_state_sha256"]):
                    prior[str(path)] = guarded_sha(path, value)
            record["prior_repair_boundaries"] = prior
    record["schema_version"] = VERSION
    state.write_once(out, record)
    return record


def digest(contract_path, repo, verbose=False):
    """Read-only status and proof digest for one repo; never writes run artifacts."""
    summary = {"repo": repo, "run_id": None, "status": "ok", "current_status": None,
               "next_transitions": [], "remaining_findings": [],
               "artifacts": {"contract": str(Path(contract_path).resolve()),
                             "state": None, "review": None, "assessment": None,
                             "telemetry_count": 0, "sealed_count": 0},
               "checks": {}, "review_budget": None, "gate_fix_budget": None}
    errors = {}
    try:
        raw = state.load(absolute_file(contract_path))
        closure_path = guard_path(Path(contract_path).absolute().parent / "setup-replacement.json", raw)
        if closure_path.is_file():
            closure = state.load(closure_path)
            successor_path = absolute_file(closure.get("successor_contract_path"), raw)
            successor = with_read_policy(state.load(successor_path), read_policy(raw))
            replacements = replacement_chain(successor, successor_path)
            require(replacements and Path(replacements[0]["contract_path"]).resolve() == Path(contract_path).resolve(),
                    "Invalid replacement successor")
            require(repo in raw.get("repos", []), "Unknown digest repo")
            summary.update(run_id=raw["run_id"], current_status="replaced",
                           setup_replacements=replacements[:1])
            summary["checks"]["replacement"] = "passed"
            return summary
        value = contract(contract_path)
        summary["run_id"] = value["run_id"]
        if value.get("_setup_replacements"):
            summary["setup_replacements"] = value["_setup_replacements"]
        require(repo in value["repos"], "Unknown digest repo")
        summary["checks"]["contract"] = "passed"
    except (ValueError, OSError, KeyError, TypeError) as error:
        summary["checks"]["contract"] = "failed"
        summary["status"] = "failed"
        summary["errors"] = {"contract": str(error)}
        return summary
    directory = Path(contract_path).parent / repo
    try:
        events = history(directory)
        inherited = {}
        for _, _, event in events:
            bound(value, repo, event)
            check_event_seals(value, repo, event, inherited)
            inherited = event.get("artifact_hashes", {})
        summary["checks"]["history"] = "passed"
    except (ValueError, OSError, KeyError, TypeError) as error:
        summary["checks"]["history"] = "failed"
        summary["status"] = "failed"
        summary["errors"] = {"history": str(error)}
        return summary
    telemetry = Path(contract_path).parent / "telemetry"
    telemetry_paths = (sorted(str(path.resolve()) for path in telemetry.glob("*.json"))
                       if telemetry.is_dir() else [])
    sealed = set()
    for _, _, event in events:
        sealed.update(event.get("artifact_hashes", {}))
    latest = events[-1][2] if events else None
    latest_path = events[-1][1] if events else None
    summary["artifacts"].update({
        "state": str(latest_path.resolve()) if latest_path else None,
        "review": (latest or {}).get("review_path"),
        "assessment": (latest or {}).get("assessment_path"),
        "telemetry_count": len(telemetry_paths),
        "sealed_count": len(sealed),
    })
    status = latest.get("status") if latest else "uninitialized"
    summary["current_status"] = status
    if value.get("deliverables", {}).get(repo):
        summary["deliverables"] = delivery_status(value, repo, latest)
        if any(row["status"] == "stale" for row in summary["deliverables"]):
            summary["status"] = "failed"
            errors["deliverables"] = "A published deliverable differs from its bound version"
    try:
        if latest and value.get("external_stores"):
            summary["check_inputs"] = check_input_status(value, repo, latest)
        declaration = value.get("slice_schedules", {}).get(repo)
        if declaration and has_bound_pre_review(value, repo):
            schedule, _ = checked_slice_schedule(value, repo, declaration["path"])
            summary["slices"] = {}
            for item in schedule["slices"]:
                slice_events = history(directory / "slices" / item["id"], value)
                current = slice_events[-1][2] if slice_events else {}
                summary["slices"][item["id"]] = {"status": current.get("status", "pending"),
                                                 "deliverables": delivery_status(value, repo, current, slice_item=item),
                                                 "check_inputs": check_input_status(value, repo, current)}
            if any(row["status"] == "stale" for section in summary["slices"].values()
                   for row in section["deliverables"] + section["check_inputs"]):
                summary["status"] = "failed"
                errors["slices"] = "A slice has stale published deliverables or check inputs"
    except (ValueError, OSError, KeyError, TypeError) as error:
        summary["status"] = "failed"
        errors["bindings"] = str(error)
    try:
        lineage = GateFixLineage(value, repo)
        limit, grants = review_budget(events, contract_path, value, repo, gate_lineage=lineage)
        replacements = collect_setup_replacements(value, contract_path, repo, lineage=lineage)
        if replacements:
            summary["setup_replacements"] = replacements
        summary["gate_fix_budget"] = lineage.summary((latest or {}).get("gate_fix_count", 0))
        used = latest.get("review_fix_count", 0) if latest else 0
        continuity = budget_continuity(value, repo, contract_path)
        carried_over = continuity["used_passes"] if continuity else historical_budget_used(grants)
        budget_summary = {"limit": limit, "used": used,
                          "remaining": max(0, limit - used), "grants": len(grants)}
        if carried_over:
            budget_summary.update({"historical_used": carried_over,
                                   "cumulative_used": carried_over + used})
        if continuity:
            budget_summary["continuity"] = continuity
        summary["review_budget"] = budget_summary
        if verbose:
            summary["review_budget"]["grant_details"] = grants
        summary["checks"]["budget"] = "passed"
    except (ValueError, OSError, KeyError, TypeError) as error:
        summary["checks"]["budget"] = "failed"
        summary["status"] = "failed"
        errors["budget"] = str(error)
    if status == "uninitialized":
        summary["next_transitions"] = ["initialized"]
    elif status == "capped" and len(events) >= 2 and events[-2][2].get("status"):
        summary["next_transitions"] = sorted({events[-2][2]["status"]} | TERMINAL)
        if (events[-2][2].get("status") == "needs-fix" and latest.get("review_fix_count", 0)
                >= latest.get("review_fix_limit", REVIEW_FIX_BUDGET)):
            summary["next_transitions"].append("reviewed-with-findings")
    else:
        summary["next_transitions"] = sorted(TRANSITIONS.get(status, set()) | TERMINAL)
    context = latest
    if latest and latest.get("status") == "capped" and len(events) >= 2:
        context = events[-2][2]
    review_path = context.get("review_path") if context else None
    if review_path:
        try:
            review_record = state.load(absolute_file(review_path))
            review_manifest = manifest(value, repo, context["source_manifest"])
            prior = [state.load(absolute_file(e[2]["review_path"])) for e in events
                     if e[2].get("status") in {"needs-fix", "reviewed"}
                     and not e[2].get("budget_grant_applied")
                     and e[2].get("review_path") and e[2]["review_path"] != review_path]
            state.findings(review_record, repo, review_record.get("round"), review_manifest, prior)
            summary["remaining_findings"] = sorted(item["id"] for item in review_record.get("findings", []))
            summary["checks"]["findings"] = "passed" if summary["remaining_findings"] else "empty"
            if verbose:
                summary["findings"] = review_record.get("findings", [])
        except (ValueError, OSError, KeyError, TypeError) as error:
            summary["checks"]["findings"] = "failed"
            summary["status"] = "failed"
            errors["findings"] = str(error)
    else:
        summary["checks"]["findings"] = "empty"
    evidence = {}
    if latest:
        try:
            final = manifest(value, repo, latest["source_manifest"])
            initial = manifest(value, repo, latest["initial_manifest"])
            if latest.get("matrix_path"):
                matrix(value, repo, latest["matrix_path"], final)
                evidence["matrix"] = "passed"
            delivery = None
            if latest.get("deliverable_manifest_path") and staged_deliverables(value, repo):
                delivery = artifact_reference(latest["deliverable_manifest_path"])
            if latest.get("pre_review_evidence_paths"):
                check_set(value, repo, "pre_review_checks", latest["pre_review_evidence_paths"], final, delivery=delivery)
                evidence["pre_review_checks"] = "passed"
            if latest.get("gate_evidence_paths"):
                check_set(value, repo, "required_gates", latest["gate_evidence_paths"], final, delivery=delivery)
                evidence["required_gates"] = "passed"
            if latest.get("assessment_path"):
                assessment(value, repo, latest["assessment_path"], final, initial, delivery=delivery)
                evidence["assessment"] = "passed"
            if latest.get("scope_amendment_path"):
                scope_amendment(contract_path, value, repo, latest["scope_amendment_path"], events, initial)
                evidence["scope_amendment"] = "passed"
            summary["checks"]["evidence"] = "passed" if evidence else "empty"
        except (ValueError, OSError, KeyError, TypeError) as error:
            summary["checks"]["evidence"] = "failed"
            summary["status"] = "failed"
            errors["evidence"] = str(error)
    else:
        summary["checks"]["evidence"] = "empty"
    if verbose:
        summary["evidence"] = evidence
        summary["artifacts"]["states"] = [str(path.resolve()) for _, path, _ in events]
        summary["artifacts"]["sealed"] = sorted(sealed)
        summary["artifacts"]["telemetry"] = telemetry_paths
        summary["events"] = [{"path": str(path.resolve()), "status": event.get("status"),
                              "artifacts": len(event.get("artifact_hashes", {}))}
                             for _, path, event in events]
    if errors:
        summary["errors"] = errors
    return summary


def digest_all_repos(contract_path, verbose=False):
    """Read-only digest for every repo in the contract in one process; never writes run artifacts."""
    result = {"run_id": None, "status": "failed", "repos": {}, "checks": {}, "summary": {}}
    try:
        raw = state.load(absolute_file(contract_path))
        closed = guard_path(Path(contract_path).absolute().parent / "setup-replacement.json", raw).is_file()
        value = raw if closed else contract(contract_path)
    except (ValueError, OSError, KeyError, TypeError) as error:
        result["checks"]["contract"] = "failed"
        result["errors"] = {"contract": str(error)}
        return result
    result["run_id"] = value["run_id"]
    result["checks"]["contract"] = "passed"
    repos = value["repos"]
    failed = []
    for repo in repos:
        summary = digest(contract_path, repo, verbose)
        if verbose:
            result["repos"][repo] = summary
        else:
            result["repos"][repo] = {key: summary[key] for key in
                                     ("status", "current_status", "next_transitions",
                                      "remaining_findings", "artifacts", "checks", "review_budget", "gate_fix_budget",
                                      "setup_replacements")
                                     if key in summary}
            if "errors" in summary:
                result["repos"][repo]["errors"] = summary["errors"]
        if summary.get("status") != "ok":
            failed.append(repo)
    result["status"] = "ok" if not failed else "failed"
    result["checks"]["repos_ok"] = len(repos) - len(failed)
    result["checks"]["repos_failed"] = len(failed)
    result["checks"]["failed_repos"] = failed
    result["summary"] = {"total_repos": len(repos), "passed_repos": len(repos) - len(failed),
                         "failed_repos": len(failed)}
    return result
