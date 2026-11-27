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
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import stat
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
    contract = state.load(value["contract"])
    for field in ("baseline_dir", "touched", "evidence_dir"):
        value["_pinned_" + field] = workflow.guarded_resolve(Path(value[field]).absolute(), contract)
        workflow.require_writable_run(value["_pinned_" + field], value["contract"], contract)
    if value.get("round_dir"):
        value["_pinned_round_dir"] = workflow.guarded_resolve(Path(value["round_dir"]).absolute(), contract)
        workflow.require_writable_run(value["_pinned_round_dir"], value["contract"], contract)
    value["touched_list"] = workflow.scope_input(value["touched"], contract, optional=True)
    value["source_scope_list"] = workflow.scope_input(value["source_scope"], contract) if value.get("source_scope") else []
    value["read_excludes"] = workflow.repo_read_excludes(contract, value["repo"])
    value["read_policy"] = workflow.read_policy(contract)
    return value


def require_readable(task, path):
    state.relative_path(path)
    root = Path(task["repo_path"]).resolve()
    exclusion = task.get("read_excludes", [])
    require(not state.PathNames(root).intersects(path, exclusion, include_ancestors=True),
            "Path intersects read_excludes")
    state.guard_excluded_path(root / path, root, exclusion)


def pinned_evidence(task, path):
    return task["_pinned_evidence_dir"] / Path(path).relative_to(Path(task["evidence_dir"]))


def task_load(task, path):
    return workflow.guarded_load(path, {"_read_policy": task["read_policy"]})


def write_text_once(path, text):
    with state.open_directory(path.parent, create=True) as directory:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666, dir_fd=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)


def next_free(base, suffix):
    index = 1
    while (base / f"{index}{suffix}").exists():
        index += 1
    return base / f"{index}{suffix}"


def backup_for(task, path):
    folder = Path(task["evidence_dir"]) / "backups" / Path(path).parent
    suffix = f".{Path(path).name}.bak"
    physical = task["_pinned_evidence_dir"] / "backups" / Path(path).parent
    matches = [folder / name for name, mode in workflow.guarded_entries(physical, {"_read_policy": task["read_policy"]})
               if name.endswith(suffix) and stat.S_ISREG(mode)]
    numbered = [(int(match.name.split(".", 1)[0]), match) for match in matches if match.name.split(".", 1)[0].isdigit()]
    return max(numbered)[1] if numbered else None


def begin_edit(task, paths):
    root = Path(task["repo_path"]).resolve()
    names = state.PathNames(root)
    touched = set(task["touched_list"])
    for path in paths:
        require_readable(task, path)
        require(workflow.in_scope(path, task["write_scope"], names), f"Outside write_scope: {path}")
    workflow.mark_work_started(task["contract"], task["repo"])
    for path in paths:
        state.baseline(root, task["baseline_dir"], path, pinned_directory=task["_pinned_baseline_dir"],
                       policies=task["read_policy"])
        source = state.safe_path(root.resolve(), path)
        if source.is_file():
            folder = task["_pinned_evidence_dir"] / "backups" / Path(path).parent
            with state.open_directory(folder, create=True) as directory:
                index = 1
                while True:
                    name = f"{index}.{Path(path).name}.bak"
                    try:
                        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o666, dir_fd=directory)
                        break
                    except FileExistsError:
                        index += 1
                with os.fdopen(fd, "wb") as handle:
                    handle.write(workflow.guarded_bytes(source, {"_read_policy": task["read_policy"]}))
            target = Path(task["evidence_dir"]) / "backups" / Path(path).parent / name
            print(f"backup {path} -> {target}")
        touched.add(path)
    touched_path = task["_pinned_touched"]
    with state.open_directory(touched_path.parent, create=True) as directory:
        fd = os.open(touched_path.name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                     0o666, dir_fd=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(sorted(touched), indent=2) + "\n")
    print(f"begin-edit ok: {len(paths)} path(s), touched={len(touched)}")


def check_file(task, paths):
    failed = []
    for path in paths:
        require_readable(task, path)
        source = state.safe_path(Path(task["repo_path"]).resolve(), path)
        data = workflow.guarded_bytes(source, {"_read_policy": task["read_policy"]})
        size = len(data)
        lines = data.count(b"\n")
        backup = backup_for(task, path)
        if backup:
            previous = workflow.guarded_bytes(backup, {"_read_policy": task["read_policy"]})
            old_size, old_lines = len(previous), previous.count(b"\n")
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


def run(task, name, argv, check, deliverable_manifest=None):
    run_name(name)
    evidence = Path(task["evidence_dir"])
    record = evidence / "runs" / f"{name}.json"
    log = evidence / "logs" / f"{name}.log"
    require(not record.exists() and not log.exists(), f"Run {name} already exists; choose a new name")
    contract = task_load(task, task["contract"])
    repo = task["repo"]
    if contract.get("external_stores") or workflow.staged_deliverables(contract, repo) or deliverable_manifest:
        contract = workflow.contract(task["contract"])
    if check:
        argv = run_check(contract, repo, check)
    require(argv, "Give --check ID or argv after --")
    workflow.mark_work_started(task["contract"], task["repo"])
    dependencies, excludes = workflow.capture_policy(task["contract"], task["repo"])
    scope = sorted(set(task["touched_list"]) | set(task["source_scope_list"]) | set(dependencies))
    root = task["repo_path"]
    before = state.capture(root, scope, excludes, task.get("read_excludes", []), policies=task.get("read_policy", ()))
    before_path = evidence / "runs" / f"{name}-before.json"
    state.write_once(pinned_evidence(task, before_path), before, pinned=True)
    check_spec = spec_for(contract, repo, check) if check else {}
    item, delivery = None, None
    if deliverable_manifest:
        slice_id = task.get("slice_id")
        schedule = contract["slice_schedules"][repo]["path"] if slice_id else None
        item = workflow.selected_slice(contract, repo, slice_id, schedule)
        delivery, _ = workflow.deliverable_binding(contract, repo, deliverable_manifest, slice_item=item)
    inputs, observed = workflow.before_check_inputs(contract, repo, check_spec, evidence / "inputs" / f"{name}-files",
                                                    pinned_out_dir=task["_pinned_evidence_dir"] / "inputs" / f"{name}-files")
    returncode, raw, timed_out = bounded_run(argv, root)
    text = checks.sanitize(raw, truncated=timed_out or len(raw) >= checks.LIMIT)
    suffix = " (timeout)" if timed_out else ""
    write_text_once(pinned_evidence(task, log), text + f"\nEXIT={returncode}{suffix}\n")
    inputs, input_bindings = workflow.after_check_inputs(contract, check_spec, inputs, observed)
    if deliverable_manifest:
        require(delivery == workflow.deliverable_binding(contract, repo, deliverable_manifest, slice_item=item)[0],
                "Deliverable version changed during execution")
    references = workflow.execution_references(inputs, input_bindings, delivery)
    after = state.capture(root, scope, excludes, task.get("read_excludes", []), policies=task.get("read_policy", ()))
    after_path = evidence / "runs" / f"{name}-after.json"
    state.write_once(pinned_evidence(task, after_path), after, pinned=True)
    state.write_once(pinned_evidence(task, record), {"name": name, "producer": "implementer", "command": shlex.join(argv), "exit_code": returncode,
                              "before_manifest": str(before_path), "after_manifest": str(after_path),
                              "log": str(log), "check": check, "timed_out": timed_out, "input_artifacts": inputs,
                              **({"check_id": check, **references} if references else {})}, pinned=True)
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
            "artifact_paths": [run_record["log"], *run_record.get("input_artifacts", [])],
            **{field: run_record[field] for field in ("input_bindings", "deliverable_binding") if field in run_record}}


def record(task, name, claims, environment, data_scope):
    evidence = Path(task["evidence_dir"])
    execution_path = evidence / "runs" / f"{run_name(name)}.json"
    run_record = task_load(task, execution_path)
    parsed = []
    for item in claims:
        require("=" in item, "Claim must be ID=assertion")
        identifier, assertion = item.split("=", 1)
        parsed.append((identifier, assertion))
    body = evidence_input(task_load(task, task["contract"]), task["repo"], run_record, parsed, environment, data_scope)
    body["execution_record"] = str(execution_path)
    body["artifact_paths"].append(str(execution_path))
    request = evidence / "inputs" / f"{name}-record.json"
    state.write_once(pinned_evidence(task, request), body, pinned=True)
    target = evidence / "records" / f"{name}.json"
    completed = subprocess.run([sys.executable, "-I", "-B", str(HELPER), "record-evidence",
                                "--contract", task["contract"], "--repo", task["repo"],
                                "--file", str(request), "--out", str(target)], capture_output=True, text=True)
    require(completed.returncode == 0, "record-evidence refused: " + (completed.stdout + completed.stderr).strip())
    print(f"record {name}: {target}")


def preflight(task, plan_file):
    """Inspect planned operations without executing commands or creating artifacts."""
    operations = task_load(task, plan_file)
    require(isinstance(operations, list) and operations, "Preflight input must be a non-empty array")
    contract = task_load(task, task["contract"])
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
                    evidence_input(contract, task["repo"], task_load(task, run_path), parsed,
                                   operation.get("environment"), operation.get("data_scope"))
                else:
                    require(name in planned_runs, f"Missing run: {name}")
                    deferred.append("Run result, command binding and evidence validation are not checked")
                deferred.append("Claim truth and final evidence validity are not checked")
                planned_records.add(name)
            elif kind == "matrix":
                rows = task_load(task, operation.get("rows"))
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
                capsule_draft(operation.get("draft"), loader=lambda path: task_load(task, path))
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
    rows = task_load(task, rows_file)
    require(isinstance(rows, list) and rows, "Matrix input is a non-empty array of rows")
    key = "rows" if task.get("slice_id") else "criteria"
    out = []
    if base_file:
        base = task_load(task, workflow.absolute_file(base_file, {"_read_policy": task["read_policy"]}))
        workflow.bound(task_load(task, task["contract"]), task["repo"], base)
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
    body = {"schema_version": workflow.VERSION, "run_id": task_load(task, task["contract"])["run_id"],
            "repo": task["repo"], key: out}
    if task.get("slice_id"):
        body["slice_id"] = task["slice_id"]
    target = next_free(Path(task["evidence_dir"]) / "matrix", ".json")
    state.write_once(pinned_evidence(task, target), body, pinned=True)
    print(f"matrix: {target}")


def applied(task, result_file):
    result = task_load(task, result_file)
    findings = task_load(task, task["findings"])
    kinds = {f["id"]: f["verification_kind"] for f in findings["findings"]}
    contract = task_load(task, task["contract"])
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
    state.write_once(task["_pinned_round_dir"] / target.name, body, pinned=True)
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
    root = Path(task["repo_path"]).absolute()
    read_excludes = task.get("read_excludes", [])
    policies = task.get("read_policy", ())
    pinned = bool(read_excludes or policies)
    marker = (state.guard_excluded_paths(root / ".git", policies, pinned=True) if policies else
              state.guard_excluded_path(root / ".git", root, read_excludes, pinned=pinned))
    try:
        state.capture_lstat(marker) if pinned else marker.stat()
    except FileNotFoundError:
        print("scope-check skipped: non-git source root")
        return
    events = workflow.history(Path(task["contract"]).resolve().parent / task["repo"])
    names = state.PathNames(root.resolve(), read_excludes)
    changed = None
    if events:
        initial_path = events[0][2].get("initial_manifest")
        require(initial_path, "Initialized state lacks initial manifest")
        initial = state.checked_manifest(initial_path)
        dependencies, excludes = workflow.capture_policy(task["contract"], task["repo"])
        scope = sorted(set(initial["files"]) | set(task["touched_list"]) | set(dependencies))
        current = state.capture(root, scope, excludes, read_excludes, policies=policies)
        require(initial["repo_path"] == current["repo_path"], "Initial manifest belongs to another repo")
        path_keys = names.changed(initial, current, missing=state.MISSING)
        spellings = names.spellings(initial["files"].keys() | current["files"].keys() | set(task["touched_list"]))
        outside = sorted(p for p in path_keys
                         if not any(workflow.in_scope(path, task["write_scope"], names) for path in spellings[p]))
        unowned = sorted(path_keys - names.keys(task["touched_list"]))
        changed = path_keys
    else:
        require(not pinned, "scope-check requires initialized history with read_excludes")
        changed = git_changes(task["repo_path"])
        outside = sorted(p for p in changed if not workflow.in_scope(p, task["write_scope"], names))
        unowned = sorted(names.keys(changed) - names.keys(task["touched_list"]))
    touched = set(task["touched_list"])
    print(f"changed={len(changed)} touched={len(touched)}")
    for path in outside:
        print(f"OUTSIDE write_scope: {path}")
    for path in unowned:
        print(f"NOT in touched: {path}")
    require(not outside and not unowned, "Scope violations; resolve ownership before continuing")
    print("scope-check ok")


def capsule_draft(draft_file, *, loader=state.load):
    """Shape check only: the author's text, never the binding fields the orchestrator writes."""
    draft = loader(draft_file)
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
    draft = capsule_draft(draft_file, loader=lambda path: task_load(task, path))
    body = json_text(draft)
    contract = task_load(task, task["contract"])
    # The helper binds a capsule to its own fingerprint scope; a repo task must name the scope file it uses.
    require(task.get("slice_id") or task.get("scope_file"), "Repo capsule needs scope_file in the task")
    # Same scope the helper fingerprints: a slice uses its touched list, a repo its scope file, plus dependencies.
    base = (workflow.scope_input(task["scope_file"], contract)
            if task.get("scope_file") and not task.get("slice_id") else task["touched_list"])
    scope = sorted(set(base) | set(task["source_scope_list"]))
    _, excludes = workflow.capture_policy(task["contract"], task["repo"])
    manifest = state.capture(task["repo_path"], scope, excludes, task.get("read_excludes", []),
                             policies=task.get("read_policy", ()))
    directory = Path(task["evidence_dir"]) / "capsules"
    target = next_free(directory, ".json")
    sidecar = directory / (target.stem + ".manifest.json")
    state.write_once(pinned_evidence(task, sidecar), manifest, pinned=True)
    value = {**draft, "schema_version": workflow.VERSION, "run_id": contract["run_id"], "repo": task["repo"],
             "written_by": agent_id, "source_fingerprint": manifest["fingerprint"],
             "manifest_path": str(sidecar)}
    if task.get("slice_id"):
        value["slice_id"] = task["slice_id"]
    state.write_once(pinned_evidence(task, target), value, pinned=True)
    print(f"capsule: {target}")


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def since_capsule(task, capsule_file):
    value = task_load(task, capsule_file)
    manifest = state.checked_manifest(value["manifest_path"], loader=lambda path: task_load(task, path))
    require(manifest["fingerprint"] == value["source_fingerprint"], "Capsule fingerprint mismatch")
    contract = task_load(task, task["contract"])
    base = (workflow.scope_input(task["scope_file"], contract)
            if task.get("scope_file") and not task.get("slice_id") else task["touched_list"])
    _, excludes = workflow.capture_policy(task["contract"], task["repo"])
    current = state.capture(task["repo_path"], sorted(set(base) | set(task["source_scope_list"])), excludes,
                            task.get("read_excludes", []), policies=task.get("read_policy", ()))
    read_excludes = task.get("read_excludes", [])
    if state.same_source(manifest, current, read_excludes):
        print("no change since capsule")
        return
    changed = sorted(state.PathNames(current["repo_path"], read_excludes).changed(manifest, current))
    for path in changed:
        print(f"changed since capsule: {path}")


def init_task(args):
    """Orchestrator step before each spawn or resume: writes the task file the implementer reads."""
    policies = state.contract_read_policy(args.contract)
    guarded = state.guard_excluded_paths(Path(args.contract).absolute(), policies, pinned=bool(policies))
    contract_path = str(guarded if policies else guarded.resolve())
    contract = workflow.contract(contract_path)
    workflow.slicer.require_setup(state, Path(contract_path), contract, workflow.read_policy(contract))
    require(args.repo in contract["repos"], f"Unknown repo: {args.repo}")
    workflow.require_unreserved(contract, contract_path, args.repo)
    for path in (args.touched, args.baseline, args.evidence_dir, args.out):
        workflow.require_writable_run(path, contract_path, contract)
    if args.round_dir:
        workflow.require_writable_run(args.round_dir, contract_path, contract)
    out = workflow.guarded_resolve(Path(args.out).absolute(), contract)
    workflow.require_writable_run(out, contract_path, contract)
    require(out.is_relative_to(Path(contract_path).parent.resolve()), "--out must lie inside the run directory")
    require(bool(args.slice) != bool(args.scope_file), "Give exactly one of --slice or --scope-file")
    require(bool(args.findings) == bool(args.round_dir), "--findings and --round-dir go together")
    if args.slice:
        require(args.schedule, "--slice needs --schedule")
        if "slicer_proposals" in contract:
            declared = contract["slice_schedules"].get(args.repo)
            require(declared and declared["path"] == args.schedule, "--schedule needs the exact approved schedule")
            item = workflow.selected_slice(contract, args.repo, args.slice, args.schedule)
        else:
            plan = state.load(Path(args.schedule).resolve())
            item = next((entry for entry in plan["slices"] if entry["id"] == args.slice), None)
            require(item, f"Unknown slice: {args.slice}")
        write_scope = item["write_scope"]
    else:
        # Repo-level writes are bounded by the scope file and touched list, not by a pattern here.
        write_scope = ["**"]
    touched = workflow.guarded_resolve(Path(args.touched).absolute(), contract)
    workflow.require_writable_run(touched, contract_path, contract)
    if not touched.exists():
        state.write_once(touched, [], pinned=True)
    task = {"contract": contract_path, "repo": args.repo, "repo_path": contract["repo_paths"][args.repo],
            "baseline_dir": str(workflow.guarded_resolve(Path(args.baseline).absolute(), contract)), "touched": str(touched),
            "write_scope": write_scope, "evidence_dir": str(workflow.guarded_resolve(Path(args.evidence_dir).absolute(), contract))}
    for field in ("baseline_dir", "evidence_dir"):
        workflow.require_writable_run(task[field], contract_path, contract)
    if args.scope_file:
        task["scope_file"] = str(Path(args.scope_file).resolve())
    if args.source_scope:
        task["source_scope"] = str(Path(args.source_scope).resolve())
    if args.slice:
        task["slice_id"] = args.slice
    if args.findings:
        task["findings"] = str(Path(args.findings).resolve())
        task["round_dir"] = str(workflow.guarded_resolve(Path(args.round_dir).absolute(), contract))
        workflow.require_writable_run(task["round_dir"], contract_path, contract)
    state.write_once(out, task, pinned=True)
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
    sub.add_argument("--deliverable-manifest")
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
        policies = state.contract_read_policy(args.contract)
        source = state.guard_excluded_paths(Path(args.contract).absolute(), policies, pinned=bool(policies))
        if not policies:
            source = source.resolve()
        root = source.parent
        state.guard_excluded_paths(root, policies, pinned=bool(policies))
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            init_task(args)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return
    if args.command == "capsule-check":
        capsule_check(args.draft)
        return
    require(args.task, "--task is required for this command")
    if args.command in {"record", "matrix", "applied", "capsule"}:
        task = state.load(args.task)
        workflow.mark_work_started(task["contract"], task["repo"])
    task = task_file(args.task)
    if args.command == "begin-edit":
        begin_edit(task, args.paths)
    elif args.command == "check-file":
        check_file(task, args.paths)
    elif args.command == "run":
        command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        run(task, args.name, command, args.check, args.deliverable_manifest)
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
