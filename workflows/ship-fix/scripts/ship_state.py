#!/usr/bin/env python3
"""Capture source state and validate immutable workflow artifacts without running gates."""

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys


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


def capture_once(root, scope, excludes):
    root = Path(root).resolve()
    require(root.is_dir(), f"Missing repo directory: {root}")
    # Do not mistake a directory inside a different checkout for an independent repo.
    is_git = (root / ".git").exists()
    tracked, others, head = set(), set(), None
    if is_git:
        tracked = {os.fsdecode(p) for p in git(root, "ls-files", "-z", "--cached").split(b"\0") if p}
        others = {os.fsdecode(p) for p in git(root, "ls-files", "-z", "--others",
                                             "--exclude-standard").split(b"\0") if p}
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        head = result.stdout.decode().strip() if result.returncode == 0 else None
    else:
        require(bool(scope), "Non-git capture requires explicit source/dependency scope")
    explicit = {relative_path(p) for p in scope}
    exclusions = [relative_path(p) for p in (*DEFAULT_EXCLUDES, *excludes)]

    def excluded(path):
        return any(path == item or path.startswith(item + "/") for item in exclusions)

    paths = tracked | explicit | {p for p in others if not excluded(p)}
    files = {p: file_state(safe_path(root, p)) for p in sorted(paths)}
    payload = {"repo_path": str(root), "head": head, "files": files}
    return {**payload, "fingerprint": digest(payload)}


def capture(root, scope, excludes=()):
    first = capture_once(root, scope, excludes)
    second = capture_once(root, scope, excludes)
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
    for name in ("capture", "delta"):
        sub = commands.add_parser(name)
        sub.add_argument("--repo", required=True)
        sub.add_argument("--scope", required=True)
        sub.add_argument("--out", required=True)
        if name == "capture":
            sub.add_argument("--exclude", action="append", default=[])
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
    sub.add_argument("--session-id")
    sub.add_argument("--at")
    sub.add_argument("--action")
    sub.add_argument("--previous-state-path")
    sub.add_argument("--previous-state-sha256")
    sub.add_argument("--checkpoint-path")
    sub.add_argument("--capsule-path")
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
    elif args.command in {"validate-contract", "validate-history", "review-budget", "transition",
                          "complete", "packet", "phase", "record-evidence", "digest"}:
        import importlib.util
        module_spec = importlib.util.spec_from_file_location(
            "ship_workflow", Path(__file__).with_name("ship_workflow.py"))
        workflow = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(workflow)
        if args.command == "validate-contract":
            workflow.contract(args.contract)
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
                    end_reason=args.end_reason, agent_id=args.agent_id, round=args.round,
                    session_id=args.session_id, at=args.at, action=args.action,
                    previous_state_path=args.previous_state_path,
                    previous_state_sha256=args.previous_state_sha256,
                    checkpoint_path=args.checkpoint_path, capsule_path=args.capsule_path)
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
        write_once(args.out, capture(args.repo, scope_paths(args.scope), args.exclude))
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
