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
import stat
import subprocess
import sys
import tempfile


DEFAULT_EXCLUDES = (".claude/review-findings.json", ".claude/review-applied.json")
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
            workflow.slice_schedule(contract_path, repo, declaration.get("path"))
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


def safe_path(root, value):
    path = Path(root) / relative_path(value)
    # A final symlink is source data; a symlinked parent would escape the scoped tree.
    for parent in path.parents:
        if parent == Path(root):
            break
        require(not parent.is_symlink(), f"Symlinked parent: {parent}")
    return path


def guard_excluded_path(path, root, read_excludes):
    """Resolve symlinks one component at a time without probing an excluded path."""
    if not read_excludes:
        return Path(path)
    root = Path(root).resolve()
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
        try:
            relative = current.relative_to(root).as_posix()
        except ValueError:
            relative = None
        require(relative is None or not any(relative == p or relative.startswith(p + "/")
                                           for p in read_excludes), "Path intersects read_excludes")
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
    def forbidden_path(path):
        return any(path == item or path.startswith(item + "/") or item.startswith(path + "/") for item in forbidden)
    require(not any(forbidden_path(p) for p in explicit), "Source scope intersects read_excludes")
    tracked, others, head = set(), set(), None
    if is_git:
        pathspec = [":(top)"] + [":(top,exclude,literal)" + p for p in forbidden]
        tracked = {os.fsdecode(p) for p in git(root, "ls-files", "-z", "--cached", "--", *pathspec).split(b"\0") if p}
        if forbidden:
            # Avoid asking Git to traverse excluded directories during untracked discovery.
            def walk(directory, prefix=""):
                with os.scandir(directory) as entries:
                    for entry in entries:
                        name = prefix + entry.name
                        if entry.name == ".git" or any(name == p or name.startswith(p + "/") for p in forbidden):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            walk(entry.path, name + "/")
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

    def excluded(path):
        return any(path == item or path.startswith(item + "/") for item in exclusions)

    paths = {p for p in tracked | explicit | {p for p in others if not excluded(p)} if not forbidden_path(p)}
    files = {p: file_state(safe_path(root, p)) for p in sorted(paths)}
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
    paths = set(scope_paths(dependencies))
    require(paths and paths <= set(original["files"]), "Dependencies must exist in the original manifest")
    def scan():
        tracked = {os.fsdecode(p) for p in git(root, "ls-files", "-z", "--cached").split(b"\0") if p}
        require(not paths & tracked, "Cannot project tracked source files")
        result = {}
        for name in sorted(paths):
            ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "--", name])
            require(ignored.returncode == 0, f"Dependency is not ignored: {name}")
            result[name] = file_state(safe_path(root, name))
        return result

    first = scan()
    require(first == scan(), "Dependencies changed during projection")
    require(all(first[name] == original["files"][name] for name in paths),
            "Dependency changed since initial manifest")
    payload = {"repo_path": original["repo_path"], "head": original["head"],
               "files": {name: value for name, value in original["files"].items() if name not in paths}}
    return {**payload, "fingerprint": digest(payload)}


def match(left, right):
    require(left["repo_path"] == right["repo_path"], "Manifests refer to different repositories")
    require(left["fingerprint"] == right["fingerprint"], "Stale certification: source states differ")


def baseline(root, directory, path):
    source = safe_path(Path(root).resolve(), path)
    directory = Path(directory).resolve()
    marker = directory / ".ship-state" / (digest(path) + ".json")
    if marker.exists():
        record = load(marker)
        require(record.get("path") == path, "Baseline marker does not match path")
        require(file_state(safe_path(directory, path)) == record["state"], "Baseline was modified")
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
    for name in scope:
        marker = directory / ".ship-state" / (digest(name) + ".json")
        require(marker.is_file(), f"Missing first-touch record: {name}")
        record = load(marker)
        require(record.get("path") == name, "Baseline marker does not match path")
        old_path, new_path = safe_path(directory, name), safe_path(root, name)
        before, after = file_state(old_path), file_state(new_path)
        require(before == record["state"], f"Baseline modified: {name}")
        if before == after:
            continue
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


def evidence(value, final):
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
        require(Path(path).is_absolute() and Path(path).is_file(), f"Missing absolute artifact: {path}")
    match(checked_manifest(value["before_manifest"]), checked_manifest(value["after_manifest"]))
    match(checked_manifest(value["after_manifest"]), final)


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
            print(json.dumps(workflow.review_budget_status(args.contract, args.repo), indent=2))
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
