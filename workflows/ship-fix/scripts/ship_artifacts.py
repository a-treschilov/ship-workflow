#!/usr/bin/env python3
"""Mechanical /ship artifact steps for implementer: backups, logs, evidence inputs, applied files, capsules.

Binds, copies and validates; never decides semantics. Claims, matrix rows and review dispositions come from
the implementer's JSON input. Every output is write-once. Check commands come from the contract (`run --check`),
so a run cannot quietly substitute a weaker command for a declared one.

Task file (written by the orchestrator, read-only here), absolute paths:
  contract, repo, repo_path, baseline_dir, touched (JSON array file, updated by begin-edit), write_scope
  (list of patterns), evidence_dir; optional: scope_file (repo) or slice_id, source_scope, findings + round_dir.
Created by `init-task` (orchestrator, before each spawn); `capsule-check` needs no task file.
  source_scope (JSON array file of dependency paths included in run manifests).
"""

import argparse
import difflib
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
HELPER = HERE / "ship_state.py"
CAPSULE_LIMIT = 16384
FILE_LIMIT = 1_000_000
GROWTH_LIMIT = 3
RUN_TIMEOUT_SECONDS = 1800


def _module(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


state = _module("artifact_state", "ship_state.py")
workflow = _module("artifact_workflow", "ship_workflow.py")
checks = _module("artifact_checks", "ship_checks.py")
require = state.require
NAME = re.compile(r"[A-Za-z0-9_.-]+")
CAPSULE_FIELDS = ("task", "decisions", "invariants", "files", "pitfalls", "verify", "do_not_touch", "open_items")


def task_file(path):
    value = state.load(path)
    require(isinstance(value, dict), "Task must be an object")
    for key in ("contract", "repo", "repo_path", "baseline_dir", "touched", "write_scope", "evidence_dir"):
        require(value.get(key), f"Task lacks {key}")
    require(Path(value["evidence_dir"]).is_absolute(), "evidence_dir must be absolute")
    value["touched_list"] = state.load(value["touched"]) if Path(value["touched"]).is_file() else []
    value["source_scope_list"] = state.load(value["source_scope"]) if value.get("source_scope") else []
    value["read_excludes"] = state.load(value["contract"]).get("read_excludes", {}).get(value["repo"], [])
    return value


def require_readable(task, path):
    state.relative_path(path)
    require(not any(path == p or path.startswith(p + "/") or p.startswith(path + "/")
                    for p in task.get("read_excludes", [])), "Path intersects read_excludes")


def next_free(base, suffix):
    index = 1
    while (base / f"{index}{suffix}").exists():
        index += 1
    return base / f"{index}{suffix}"


def backup_for(task, path):
    folder = Path(task["evidence_dir"]) / "backups" / Path(path).parent
    matches = list(folder.glob(f"*.{Path(path).name}.bak")) if folder.is_dir() else []
    numbered = [(int(match.name.split(".", 1)[0]), match) for match in matches if match.name.split(".", 1)[0].isdigit()]
    return max(numbered)[1] if numbered else None


def begin_edit(task, paths):
    root = Path(task["repo_path"])
    touched = set(task["touched_list"])
    for path in paths:
        require_readable(task, path)
        require(workflow.in_scope(path, task["write_scope"]), f"Outside write_scope: {path}")
        state.baseline(root, task["baseline_dir"], path)
        source = state.safe_path(root.resolve(), path)
        if source.is_file():
            target = next_free(Path(task["evidence_dir"]) / "backups" / Path(path).parent, f".{Path(path).name}.bak")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            print(f"backup {path} -> {target}")
        touched.add(path)
    Path(task["touched"]).write_text(json.dumps(sorted(touched), indent=2) + "\n")
    print(f"begin-edit ok: {len(paths)} path(s), touched={len(touched)}")


def check_file(task, paths):
    failed = []
    for path in paths:
        require_readable(task, path)
        source = state.safe_path(Path(task["repo_path"]).resolve(), path)
        require(source.is_file(), f"Not a regular file: {path}")
        size = source.stat().st_size
        lines = source.read_bytes().count(b"\n")
        backup = backup_for(task, path)
        if backup:
            old_size, old_lines = backup.stat().st_size, backup.read_bytes().count(b"\n")
            grown = size > FILE_LIMIT and size > GROWTH_LIMIT * max(old_size, 1)
            grown = grown or (lines > 1000 and lines > GROWTH_LIMIT * max(old_lines, 1))
        else:
            grown = size > 5 * FILE_LIMIT
        status = "FAIL" if grown else "ok"
        print(f"{status} {path} bytes={size} lines={lines} backup={'yes' if backup else 'no'}")
        if grown:
            failed.append(path)
    require(not failed, "Suspicious growth (restore from backup): " + ", ".join(failed))


def run_name(name):
    require(NAME.fullmatch(name or ""), "Run name must be [A-Za-z0-9_.-]+")
    return name


def run_check(contract, repo, identifier):
    for entry in contract.get("pre_review_checks", {}).get(repo, []) + contract.get("preflight_checks", {}).get(repo, []) + contract.get("required_gates", {}).get(repo, []):
        if entry.get("id") == identifier:
            require(isinstance(entry.get("argv"), list), f"Check {identifier} has no argv declaration")
            return entry["argv"]
    raise ValueError(f"Unknown check: {identifier}")


def run(task, name, argv, check):
    run_name(name)
    evidence = Path(task["evidence_dir"])
    record = evidence / "runs" / f"{name}.json"
    log = evidence / "logs" / f"{name}.log"
    require(not record.exists() and not log.exists(), f"Run {name} already exists; choose a new name")
    if check:
        argv = run_check(state.load(task["contract"]), task["repo"], check)
    require(argv, "Give --check ID or argv after --")
    dependencies, excludes = workflow.capture_policy(task["contract"], task["repo"])
    scope = sorted(set(task["touched_list"]) | set(task["source_scope_list"]) | set(dependencies))
    root = task["repo_path"]
    before = state.capture(root, scope, excludes, task.get("read_excludes", []))
    before_path = evidence / "runs" / f"{name}-before.json"
    state.write_once(before_path, before)
    check_spec = spec_for(state.load(task["contract"]), task["repo"], check) if check else {}
    inputs = workflow.snapshot_inputs(check_spec.get("input_paths", []), evidence / "inputs" / f"{name}-files",
                                      root, task.get("read_excludes", []))
    returncode, raw, timed_out = bounded_run(argv, root)
    workflow.check_inputs_unchanged(inputs)
    log.parent.mkdir(parents=True, exist_ok=True)
    text = checks.sanitize(raw, truncated=timed_out or len(raw) >= checks.LIMIT)
    suffix = " (timeout)" if timed_out else ""
    log.write_text(text + f"\nEXIT={returncode}{suffix}\n", encoding="utf-8")
    after = state.capture(root, scope, excludes, task.get("read_excludes", []))
    after_path = evidence / "runs" / f"{name}-after.json"
    state.write_once(after_path, after)
    state.write_once(record, {"name": name, "producer": "implementer", "command": shlex.join(argv), "exit_code": returncode,
                              "before_manifest": str(before_path), "after_manifest": str(after_path),
                              "log": str(log), "check": check, "timed_out": timed_out, "input_artifacts": inputs})
    print(f"run {name}: exit={returncode} log={log}")
    for line in text.splitlines()[-20:]:
        print("  " + line)


def bounded_run(argv, cwd, timeout=RUN_TIMEOUT_SECONDS):
    """Run argv with a timeout, retaining at most checks.LIMIT bytes of combined output."""
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    selector = selectors.DefaultSelector()
    os.set_blocking(proc.stdout.fileno(), False)
    selector.register(proc.stdout, selectors.EVENT_READ)
    buf, deadline, timed_out = bytearray(), time.monotonic() + timeout, False
    try:
        while True:
            if proc.poll() is not None and not selector.get_map():
                break
            if time.monotonic() >= deadline:
                timed_out = True
                proc.kill()
                break
            for key, _ in selector.select(0.2):
                chunk = os.read(key.fileobj.fileno(), 16384)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                if len(buf) < checks.LIMIT:
                    buf.extend(chunk[:checks.LIMIT - len(buf)])
        proc.wait()
    finally:
        selector.close()
    return proc.returncode, bytes(buf), timed_out


def spec_for(contract, repo, identifier):
    for entry in contract.get("criteria", []):
        if entry.get("id") == identifier:
            return entry
    for key in ("pre_review_checks", "preflight_checks", "required_gates"):
        for entry in contract.get(key, {}).get(repo, []):
            if entry.get("id") == identifier:
                return entry
    raise ValueError(f"Unknown criterion or check: {identifier}")


def evidence_input(contract, repo, run_record, claims, environment, data_scope):
    """Evidence fields derive from the contract, never from the implementer's free text."""
    kinds, specs = set(), []
    for identifier, assertion in claims:
        spec = spec_for(contract, repo, identifier)
        kinds.add(spec["kind"])
        specs.append(spec)
        state.text(assertion, "claim assertion")
    require(len(kinds) == 1, "All claims of one run must share one verification kind")
    first = specs[0]
    if first.get("argv"):
        require(run_record["command"] == shlex.join(first["argv"]), "Run command differs from declared check")
    env = first.get("environment") or environment
    scope = first.get("data_scope") or data_scope
    require(env and scope, "Environment and data scope are required (contract or --environment/--data-scope)")
    return {"kind": kinds.pop(), "status": "passed" if run_record["exit_code"] == 0 else "failed",
            "producer": "implementer", "command": run_record["command"], "exit_code": run_record["exit_code"],
            "environment": env, "data_scope": scope,
            "claims": [{"id": i, "assertion": a} for i, a in claims],
            "before_manifest": run_record["before_manifest"], "after_manifest": run_record["after_manifest"],
            "artifact_paths": [run_record["log"], *run_record.get("input_artifacts", [])]}


def record(task, name, claims, environment, data_scope):
    evidence = Path(task["evidence_dir"])
    execution_path = evidence / "runs" / f"{run_name(name)}.json"
    run_record = state.load(execution_path)
    parsed = []
    for item in claims:
        require("=" in item, "Claim must be ID=assertion")
        identifier, assertion = item.split("=", 1)
        parsed.append((identifier, assertion))
    body = evidence_input(state.load(task["contract"]), task["repo"], run_record, parsed, environment, data_scope)
    body["execution_record"] = str(execution_path)
    body["artifact_paths"].append(str(execution_path))
    request = evidence / "inputs" / f"{name}-record.json"
    state.write_once(request, body)
    target = evidence / "records" / f"{name}.json"
    completed = subprocess.run([sys.executable, "-I", "-B", str(HELPER), "record-evidence",
                                "--contract", task["contract"], "--repo", task["repo"],
                                "--file", str(request), "--out", str(target)], capture_output=True, text=True)
    require(completed.returncode == 0, "record-evidence refused: " + (completed.stdout + completed.stderr).strip())
    print(f"record {name}: {target}")


def preflight(task, plan_file):
    """Inspect planned operations without executing commands or creating artifacts."""
    operations = state.load(plan_file)
    require(isinstance(operations, list) and operations, "Preflight input must be a non-empty array")
    contract = state.load(task["contract"])
    evidence = Path(task["evidence_dir"])
    planned_runs, planned_records = set(), set()
    results = []
    for index, operation in enumerate(operations):
        deferred = []
        try:
            require(isinstance(operation, dict), "Operation must be an object")
            kind = operation.get("op")
            if kind == "run":
                name = run_name(operation.get("name"))
                require(name not in planned_runs, f"Duplicate run: {name}")
                for path in (evidence / "runs" / f"{name}.json", evidence / "logs" / f"{name}.log",
                             evidence / "runs" / f"{name}-before.json", evidence / "runs" / f"{name}-after.json"):
                    require(not path.exists(), f"Run output already exists: {path}")
                if operation.get("check"):
                    run_check(contract, task["repo"], operation["check"])
                else:
                    argv = operation.get("argv")
                    require(isinstance(argv, list) and argv and all(isinstance(x, str) and x for x in argv),
                            "Exploratory run needs a non-empty argv array")
                planned_runs.add(name)
                deferred.append("Command execution and outcome are not checked")
            elif kind == "record":
                name = run_name(operation.get("name"))
                require(name not in planned_records, f"Duplicate record: {name}")
                require(not (evidence / "records" / f"{name}.json").exists(), f"Evidence record already exists: {name}")
                claims = operation.get("claims")
                require(isinstance(claims, list) and claims, "Record needs claims")
                parsed = []
                for item in claims:
                    require(isinstance(item, str) and "=" in item, "Claim must be ID=assertion")
                    identifier, assertion = item.split("=", 1)
                    state.text(assertion, "claim assertion")
                    spec_for(contract, task["repo"], identifier)
                    parsed.append((identifier, assertion))
                require(len({x[0] for x in parsed}) == len(parsed), "Duplicate claim ID")
                require(len({spec_for(contract, task["repo"], x[0])["kind"] for x in parsed}) == 1,
                        "All claims of one run must share one verification kind")
                run_path = evidence / "runs" / f"{name}.json"
                if run_path.is_file():
                    evidence_input(contract, task["repo"], state.load(run_path), parsed,
                                   operation.get("environment"), operation.get("data_scope"))
                else:
                    require(name in planned_runs, f"Missing run: {name}")
                    deferred.append("Run result, command binding and evidence validation are not checked")
                deferred.append("Claim truth and final evidence validity are not checked")
                planned_records.add(name)
            elif kind == "matrix":
                rows = state.load(operation.get("rows"))
                require(isinstance(rows, list) and rows, "Matrix input is a non-empty array of rows")
                for row in rows:
                    require(isinstance(row, dict) and isinstance(row.get("runs", []), list), "Invalid matrix row")
                    for name in row.get("runs", []):
                        run_name(name)
                        if not (evidence / "records" / f"{name}.json").is_file():
                            require(name in planned_records, f"No evidence record for run {name}")
                            deferred.append(f"Evidence record pending: {name}")
                deferred.append("Matrix semantics and source freshness are not checked")
            elif kind == "capsule-check":
                capsule_draft(operation.get("draft"))
                deferred.append("Capsule claims and source binding are not checked")
            else:
                raise ValueError(f"Unknown preflight operation: {kind}")
            results.append({"index": index, "op": kind, "status": "deferred" if deferred else "ready",
                            "limitations": deferred})
        except (ValueError, OSError, KeyError, TypeError) as error:
            results.append({"index": index, "op": operation.get("op") if isinstance(operation, dict) else None,
                            "status": "refused", "reason": str(error)})
    print(json.dumps(results, indent=2))
    require(all(result["status"] != "refused" for result in results), "Preflight refused one or more operations")


def evidence_paths(task, runs):
    paths = []
    for name in runs:
        path = Path(task["evidence_dir"]) / "records" / f"{run_name(name)}.json"
        require(path.is_file(), f"No evidence record for run {name}; run record first")
        paths.append(str(path))
    return paths


def matrix(task, rows_file, base_file=None):
    rows = state.load(rows_file)
    require(isinstance(rows, list) and rows, "Matrix input is a non-empty array of rows")
    key = "rows" if task.get("slice_id") else "criteria"
    out = []
    if base_file:
        base = state.load(workflow.absolute_file(base_file))
        workflow.bound(state.load(task["contract"]), task["repo"], base)
        require(base.get("slice_id") == task.get("slice_id"), "Matrix base belongs to another slice")
        require(isinstance(base.get(key), list), "Matrix base lacks rows")
        out = list(base[key])
    identity = lambda row: (row.get("criterion_id"), row.get("part")) if task.get("slice_id") else row.get("criterion_id")
    replacements = set()
    for row in rows:
        row_id = identity(row)
        require(row_id not in replacements, "Duplicate matrix replacement")
        replacements.add(row_id)
        entry = {key: value for key, value in row.items() if key != "runs"}
        entry["evidence_paths"] = evidence_paths(task, row.get("runs", []))
        if base_file:
            matches = [i for i, old in enumerate(out) if identity(old) == row_id]
            require(len(matches) == 1, "Matrix replacement must name one existing row")
            require(row.get("replaces_evidence_paths") == out[matches[0]].get("evidence_paths"),
                    "Matrix replacement must name previous evidence")
            entry.pop("replaces_evidence_paths", None)
            out[matches[0]] = entry
            continue
        out.append(entry)
    body = {"schema_version": workflow.VERSION, "run_id": state.load(task["contract"])["run_id"],
            "repo": task["repo"], key: out}
    if task.get("slice_id"):
        body["slice_id"] = task["slice_id"]
    target = next_free(Path(task["evidence_dir"]) / "matrix", ".json")
    state.write_once(target, body)
    print(f"matrix: {target}")


def applied(task, result_file):
    result = state.load(result_file)
    findings = state.load(task["findings"])
    kinds = {f["id"]: f["verification_kind"] for f in findings["findings"]}
    contract = state.load(task["contract"])
    body = {"schema_version": workflow.VERSION, "run_id": contract["run_id"], "repo": task["repo"],
            "round": findings["round"], "applied": [], "failed": [], "skipped": []}
    for entry in result.get("applied", []):
        require(entry.get("id") in kinds, f"Applied id not in findings: {entry.get('id')}")
        body["applied"].append({"id": entry["id"], "verified": entry.get("verified"),
                                "verification_kind": kinds[entry["id"]],
                                "evidence_paths": evidence_paths(task, entry.get("runs", []))})
    for group in ("failed", "skipped"):
        for entry in result.get(group, []):
            body[group].append({"id": entry.get("id"), "reason": entry.get("reason")})
    state.applied(body, findings)
    target = Path(task["round_dir"]) / f"round-{findings['round']}-applied.json"
    state.write_once(target, body)
    print(f"applied: {target}")


def git_changes(root):
    if not (Path(root) / ".git").exists():
        return None
    completed = subprocess.run(["git", "-C", root, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                               capture_output=True)
    require(completed.returncode == 0, "git status failed for scope check")
    entries = completed.stdout.decode().split("\0")
    paths = set()
    for entry in entries:
        if len(entry) > 3:
            paths.add(entry[3:])
    return paths


def scope_check(task):
    changed = git_changes(task["repo_path"])
    if changed is None:
        print("scope-check skipped: non-git source root")
        return
    events = workflow.history(Path(task["contract"]).resolve().parent / task["repo"])
    if events:
        initial_path = events[0][2].get("initial_manifest")
        require(initial_path, "Initialized state lacks initial manifest")
        initial = state.checked_manifest(initial_path)
        dependencies, excludes = workflow.capture_policy(task["contract"], task["repo"])
        scope = sorted(set(initial["files"]) | set(task["touched_list"]) | set(dependencies))
        current = state.capture(task["repo_path"], scope, excludes, task.get("read_excludes", []))
        require(initial["repo_path"] == current["repo_path"], "Initial manifest belongs to another repo")
        changed = {p for p in set(initial["files"]) | set(current["files"])
                   if initial["files"].get(p, {"kind": "missing"}) !=
                   current["files"].get(p, {"kind": "missing"})}
    touched = set(task["touched_list"])
    outside = sorted(p for p in changed if not workflow.in_scope(p, task["write_scope"]))
    unowned = sorted(changed - touched)
    print(f"changed={len(changed)} touched={len(touched)}")
    for path in outside:
        print(f"OUTSIDE write_scope: {path}")
    for path in unowned:
        print(f"NOT in touched: {path}")
    require(not outside and not unowned, "Scope violations; resolve ownership before continuing")
    print("scope-check ok")


def capsule_draft(draft_file):
    """Shape check only: the author's text, never the binding fields the orchestrator writes."""
    draft = state.load(draft_file)
    require(isinstance(draft, dict), "Capsule must be an object")
    for field in CAPSULE_FIELDS:
        require(field in draft, f"Capsule lacks {field}")
    reserved = set(draft) - set(CAPSULE_FIELDS)
    require(not reserved, "Capsule draft has binding fields: " + ", ".join(sorted(reserved)))
    require(len(json_text(draft).encode()) <= CAPSULE_LIMIT, f"Capsule exceeds {CAPSULE_LIMIT} bytes")
    return draft


def capsule_check(draft_file):
    capsule_draft(draft_file)
    print(f"capsule-check ok: {draft_file}")


def capsule(task, draft_file, agent_id):
    """Sealed by the orchestrator with the agent identity it observed; the agent cannot name its own author."""
    require(isinstance(agent_id, str) and agent_id.strip(), "capsule needs --agent-id of the author that wrote it")
    draft = capsule_draft(draft_file)
    body = json_text(draft)
    contract = state.load(task["contract"])
    # The helper binds a capsule to its own fingerprint scope; a repo task must name the scope file it uses.
    require(task.get("slice_id") or task.get("scope_file"), "Repo capsule needs scope_file in the task")
    # Same scope the helper fingerprints: a slice uses its touched list, a repo its scope file, plus dependencies.
    base = state.load(task["scope_file"]) if task.get("scope_file") and not task.get("slice_id") else task["touched_list"]
    scope = sorted(set(base) | set(task["source_scope_list"]))
    _, excludes = workflow.capture_policy(task["contract"], task["repo"])
    manifest = state.capture(task["repo_path"], scope, excludes, task.get("read_excludes", []))
    directory = Path(task["evidence_dir"]) / "capsules"
    target = next_free(directory, ".json")
    sidecar = directory / (target.stem + ".manifest.json")
    state.write_once(sidecar, manifest)
    value = {**draft, "schema_version": workflow.VERSION, "run_id": contract["run_id"], "repo": task["repo"],
             "written_by": agent_id, "source_fingerprint": manifest["fingerprint"],
             "manifest_path": str(sidecar)}
    if task.get("slice_id"):
        value["slice_id"] = task["slice_id"]
    state.write_once(target, value)
    print(f"capsule: {target}")


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def since_capsule(task, capsule_file):
    value = state.load(capsule_file)
    manifest = state.load(value["manifest_path"])
    base = state.load(task["scope_file"]) if task.get("scope_file") and not task.get("slice_id") else task["touched_list"]
    _, excludes = workflow.capture_policy(task["contract"], task["repo"])
    current = state.capture(task["repo_path"], sorted(set(base) | set(task["source_scope_list"])), excludes,
                            task.get("read_excludes", []))
    if current["fingerprint"] == value["source_fingerprint"]:
        print("no change since capsule")
        return
    before, after = manifest["files"], current["files"]
    changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    for path in changed:
        print(f"changed since capsule: {path}")


def init_task(args):
    """Orchestrator step before each spawn or resume: writes the task file the implementer reads."""
    contract_path = str(Path(args.contract).resolve())
    contract = workflow.contract(contract_path)
    require(args.repo in contract["repos"], f"Unknown repo: {args.repo}")
    out = Path(args.out).resolve()
    require(out.is_relative_to(Path(contract_path).parent.resolve()), "--out must lie inside the run directory")
    require(bool(args.slice) != bool(args.scope_file), "Give exactly one of --slice or --scope-file")
    require(bool(args.findings) == bool(args.round_dir), "--findings and --round-dir go together")
    if args.slice:
        require(args.schedule, "--slice needs --schedule")
        plan = state.load(Path(args.schedule).resolve())
        item = next((entry for entry in plan["slices"] if entry["id"] == args.slice), None)
        require(item, f"Unknown slice: {args.slice}")
        write_scope = item["write_scope"]
    else:
        # Repo-level writes are bounded by the scope file and touched list, not by a pattern here.
        write_scope = ["**"]
    touched = Path(args.touched).resolve()
    if not touched.exists():
        touched.parent.mkdir(parents=True, exist_ok=True)
        touched.write_text("[]\n")
    task = {"contract": contract_path, "repo": args.repo, "repo_path": contract["repo_paths"][args.repo],
            "baseline_dir": str(Path(args.baseline).resolve()), "touched": str(touched),
            "write_scope": write_scope, "evidence_dir": str(Path(args.evidence_dir).resolve())}
    if args.scope_file:
        task["scope_file"] = str(Path(args.scope_file).resolve())
    if args.source_scope:
        task["source_scope"] = str(Path(args.source_scope).resolve())
    if args.slice:
        task["slice_id"] = args.slice
    if args.findings:
        task["findings"] = str(Path(args.findings).resolve())
        task["round_dir"] = str(Path(args.round_dir).resolve())
    state.write_once(out, task)
    print(f"init-task: {out}")


def parser():
    main = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    main.add_argument("--task")
    commands = main.add_subparsers(dest="command", required=True)
    sub = commands.add_parser("init-task")
    sub.add_argument("--contract", required=True)
    sub.add_argument("--repo", required=True)
    sub.add_argument("--touched", required=True)
    sub.add_argument("--baseline", required=True)
    sub.add_argument("--evidence-dir", required=True)
    sub.add_argument("--out", required=True)
    sub.add_argument("--scope-file")
    sub.add_argument("--source-scope")
    sub.add_argument("--slice")
    sub.add_argument("--schedule")
    sub.add_argument("--findings")
    sub.add_argument("--round-dir")
    sub = commands.add_parser("capsule-check")
    sub.add_argument("draft")
    sub = commands.add_parser("begin-edit")
    sub.add_argument("paths", nargs="+")
    sub = commands.add_parser("check-file")
    sub.add_argument("paths", nargs="+")
    sub = commands.add_parser("run")
    sub.add_argument("name")
    sub.add_argument("--check")
    sub.add_argument("argv", nargs="*")
    sub = commands.add_parser("record")
    sub.add_argument("name")
    sub.add_argument("--claim", action="append", required=True)
    sub.add_argument("--environment")
    sub.add_argument("--data-scope")
    sub = commands.add_parser("matrix")
    sub.add_argument("rows")
    sub.add_argument("--base")
    sub = commands.add_parser("applied")
    sub.add_argument("result")
    sub = commands.add_parser("preflight")
    sub.add_argument("plan")
    commands.add_parser("scope-check")
    sub = commands.add_parser("capsule")
    sub.add_argument("draft")
    sub.add_argument("--agent-id", required=True)
    sub = commands.add_parser("since-capsule")
    sub.add_argument("capsule")
    return main


def main(argv):
    args = parser().parse_args(argv)
    if args.command == "init-task":
        init_task(args)
        return
    if args.command == "capsule-check":
        capsule_check(args.draft)
        return
    require(args.task, "--task is required for this command")
    task = task_file(args.task)
    if args.command == "begin-edit":
        begin_edit(task, args.paths)
    elif args.command == "check-file":
        check_file(task, args.paths)
    elif args.command == "run":
        command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        run(task, args.name, command, args.check)
    elif args.command == "record":
        record(task, args.name, args.claim, args.environment, args.data_scope)
    elif args.command == "matrix":
        matrix(task, args.rows, args.base)
    elif args.command == "applied":
        applied(task, args.result)
    elif args.command == "preflight":
        preflight(task, args.plan)
    elif args.command == "scope-check":
        scope_check(task)
    elif args.command == "capsule":
        capsule(task, args.draft, args.agent_id)
    elif args.command == "since-capsule":
        since_capsule(task, args.capsule)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ValueError, OSError, KeyError) as error:
        print(f"refused: {error}", file=sys.stderr)
        sys.exit(2)
