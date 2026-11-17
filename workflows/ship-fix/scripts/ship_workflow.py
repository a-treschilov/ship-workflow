"""Versioned workflow validation; never executes checks or launches agents."""

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import shlex

import importlib.util

_spec = importlib.util.spec_from_file_location("workflow_state_helpers", Path(__file__).with_name("ship_state.py"))
state = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(state)

VERSION = 2
# Why a phase ended; unknown is a legitimate answer and must never be inferred from duration.
END_REASONS = {"done", "context", "api-error", "user-stop", "timeout", "unknown"}
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
    "reviewed": {"gates-passed", "edited-unverified"},
    "gates-passed": {"verified", "edited-unverified"},
    "verified": {"edited-unverified", "verified"},
    "blocked": {"ready", "edited-unverified", "pre-review-passed"},
    "handoff": {"pre-review-passed", "needs-fix", "reviewed"},
    "capped": set(),
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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


def absolute_file(path):
    require(isinstance(path, (str, Path)) and Path(path).is_absolute() and Path(path).is_file(),
            f"Missing absolute file: {path}")
    return Path(path)


def scope_amendment(contract_path, value, repo, path, events, initial):
    """Validate a bounded, run-local addition without replacing the sealed schedule."""
    path = absolute_file(path)
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
    schedule_path = absolute_file(declaration.get("path"))
    slice_schedule(contract_path, repo, schedule_path)
    slices = state.load(schedule_path)["slices"]
    anchor_path = absolute_file(amendment.get("previous_state_path"))
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
    before_path = absolute_file(amendment.get("before_manifest"))
    require(str(before_path) == anchor.get("source_manifest") and
            amendment.get("before_manifest_sha256") == sha(before_path), "Scope amendment source changed")
    before = manifest(value, repo, before_path)
    require(before["fingerprint"] == anchor["source_fingerprint"] and before["head"] == initial["head"],
            "Scope amendment source is not the reviewed source")
    original_gate = state.load(absolute_file(anchor.get("slice_gate_path")))
    bound(value, repo, original_gate)
    check_seals(original_gate["artifact_hashes"])
    require(amendment.get("slice_states") == original_gate.get("slices"),
            "Scope amendment must bind all accepted slices")
    for identifier, expected_hash in amendment["slice_states"].items():
        state.relative_path(identifier)
        slice_events = history(run / repo / "slices" / identifier)
        require(slice_events and slice_events[-1][2].get("status") == "accepted" and
                sha(slice_events[-1][1]) == expected_hash, "Scope amendment accepted slices changed")
    additions = amendment.get("paths")
    require(isinstance(additions, list) and additions and len(additions) == len(set(additions)),
            "Scope amendment needs unique paths")
    root = Path(value["repo_paths"][repo])
    require((root / ".git").exists(), "Scope amendment requires a git repository")
    tracked = {p.decode() for p in state.git(root, "ls-files", "-z", "--cached").split(b"\0") if p}
    baseline_dir = Path(anchor["baseline_dir"])
    for name in additions:
        state.relative_path(name)
        require(not any(char in name for char in "*?[]"), "Scope amendment paths cannot be patterns")
        require(name in tracked, "Scope amendment path must be tracked")
        require(not any(in_scope(name, item["write_scope"]) for item in slices),
                "Scope amendment path belongs to an approved slice")
        require(name not in anchor.get("scope", []) and name not in anchor.get("source_scope", []),
                "Scope amendment path is already scoped")
        require(before["files"].get(name, {"kind": "missing"}) == initial["files"].get(name, {"kind": "missing"}),
                "Scope amendment cannot adopt pre-existing source drift")
        marker = baseline_dir / ".ship-state" / (state.digest(name) + ".json")
        record = state.load(absolute_file(marker))
        require(record.get("path") == name and record.get("state") == initial["files"].get(name) and
                state.file_state(state.safe_path(baseline_dir, name)) == record["state"],
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
    path = absolute_file(path)
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


def contract(path):
    path = absolute_file(path)
    value = state.load(path)
    require(isinstance(value, dict), "Contract must be an object")
    require("_amendment" not in value and "_contract_path" not in value, "Contract cannot define internal state")
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
        require(isinstance(root, str) and Path(root).is_absolute() and Path(root).is_dir(),
                f"Missing absolute repo path: {repo}")
    read_excludes = value.get("read_excludes", {})
    require(isinstance(read_excludes, dict) and set(read_excludes) <= set(repos), "Invalid read_excludes")
    for paths in read_excludes.values():
        require(isinstance(paths, list) and len(paths) == len(set(paths)), "Invalid read_excludes paths")
        for path in paths:
            state.relative_path(path)
    continuity = value.get("budget_continuity", [])
    require(isinstance(continuity, list), "Invalid budget continuity")
    continuity_keys = []
    continuity_sources = []
    for item in continuity:
        require(isinstance(item, dict) and item.get("repo") in repos, "Invalid continuity repo")
        if item.get("slice_id") is not None:
            state.relative_path(item["slice_id"])
        continuity_keys.append((item["repo"], item.get("slice_id")))
        continuity_sources.append((item.get("source_contract_path"), item["repo"], item.get("source_slice_id")))
    require(len(continuity_keys) == len(set(continuity_keys)), "Duplicate budget continuity scope")
    require(len(continuity_sources) == len(set(continuity_sources)), "Duplicate budget continuity source")
    successors = value.get("successor_scopes", [])
    require(isinstance(successors, list) and all(isinstance(item, dict) for item in successors),
            "Invalid successor scopes")
    successor_keys = [(item.get("repo"), item.get("slice_id"), item.get("work_item")) for item in successors]
    require(len(successor_keys) == len(set(successor_keys)), "Duplicate successor scope")
    continuity_work = {(item["repo"], item.get("slice_id"), item.get("work_item")) for item in continuity}
    require(set(successor_keys) == continuity_work, "Successor scope requires matching budget continuity")
    deliverables = value.get("deliverables", {})
    require(isinstance(deliverables, dict) and set(deliverables) <= set(repos), "Invalid deliverables")
    for repo, entries in deliverables.items():
        unique_ids(entries, "deliverables")
        paths = []
        for entry in entries:
            path = entry.get("path")
            require(isinstance(path, str) and Path(path).is_absolute(), "Deliverable needs an absolute path")
            paths.append(path)
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
                require(isinstance(inputs, list) and len(inputs) == len(set(inputs)), "Invalid check input_paths")
                for input_path in inputs:
                    require(isinstance(input_path, str) and Path(input_path).is_absolute(),
                            "Check input path must be absolute")
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
        snapshot = absolute_file(value.get("approved_snapshot"))
        require(snapshot.resolve().is_relative_to(path.parent.resolve()), "Approved snapshot must be frozen inside run directory")
        require(sha(snapshot) == value.get("plan_sha256"),
                "Approved snapshot hash mismatch")
        require(sha(absolute_file(value.get("plan_path"))) == value["plan_sha256"],
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
        events = history(path.parent / repos[0])
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
            require(sha(absolute_file(a_path)) == registration.get("amendment_sha256")
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
    for item in continuity:
        budget_continuity(value, item["repo"], path, item.get("slice_id"))
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
    result = state.checked_manifest(absolute_file(path))
    require(result["repo_path"] == str(Path(value["repo_paths"][repo]).resolve()), "Wrong manifest repo")
    return result


def fresh(value, repo, path, scope, excludes=()):
    recorded = manifest(value, repo, path)
    state.match(recorded, state.capture(value["repo_paths"][repo], scope, excludes,
                                        value.get("read_excludes", {}).get(repo, [])))
    return recorded


def snapshot_deliverables(contract_path, repo, out_dir, criterion_ids=None):
    value = contract(contract_path)
    entries = value.get("deliverables", {}).get(repo, [])
    if criterion_ids is not None:
        entries = [e for e in entries if set(e["criterion_ids"]) & set(criterion_ids)]
    require(entries, "No deliverables declared")
    root = Path(contract_path).resolve().parent
    output = Path(out_dir)
    require(output.is_absolute() and output.resolve().is_relative_to(root), "Snapshots must be run-local")
    require(not output.exists(), "Snapshot directory already exists")
    payload = []
    for entry in entries:
        state.guard_excluded_path(entry["path"], value["repo_paths"][repo],
                                  value.get("read_excludes", {}).get(repo, []))
        source = absolute_file(entry["path"])
        require(not source.is_symlink(), "Deliverable cannot be a symlink")
        try:
            relative = source.resolve().relative_to(Path(value["repo_paths"][repo]).resolve()).as_posix()
        except ValueError:
            relative = None
        if relative is not None:
            require(not any(relative == p or relative.startswith(p + "/")
                            for p in value.get("read_excludes", {}).get(repo, [])),
                    "Deliverable intersects read_excludes")
        data = source.read_bytes()
        require(data == source.read_bytes(), "Deliverable changed while snapshotting")
        payload.append((entry, data))
    output.mkdir(parents=True)
    rows = []
    for index, (entry, data) in enumerate(payload):
        snapshot = output / f"{index}.snapshot"
        with snapshot.open("xb") as handle:
            handle.write(data)
        rows.append({"id": entry["id"], "path": entry["path"], "snapshot": str(snapshot),
                     "sha256": hashlib.sha256(data).hexdigest()})
    result = {"schema_version": VERSION, "run_id": value["run_id"], "repo": repo, "deliverables": rows}
    state.write_once(output / "manifest.json", result)
    return str(output / "manifest.json")


def snapshot_inputs(paths, out_dir, repo_root, read_excludes):
    if not paths:
        return []
    output = Path(out_dir)
    require(not output.exists(), "Input snapshot directory already exists")
    payload = []
    for item in paths:
        state.guard_excluded_path(item, repo_root, read_excludes)
        source = absolute_file(item)
        require(not source.is_symlink(), "Input cannot be a symlink")
        try:
            relative = source.resolve().relative_to(Path(repo_root).resolve()).as_posix()
        except ValueError:
            relative = None
        if relative is not None:
            require(not any(relative == p or relative.startswith(p + "/") for p in read_excludes),
                    "Input intersects read_excludes")
        data = source.read_bytes()
        require(data == source.read_bytes(), "Check input changed while snapshotting")
        payload.append((str(source), data))
    output.mkdir(parents=True)
    rows = []
    for index, (source, data) in enumerate(payload):
        target = output / f"{index}.input"
        with target.open("xb") as handle:
            handle.write(data)
        rows.append({"path": source, "snapshot": str(target), "sha256": hashlib.sha256(data).hexdigest()})
    registry = output / "manifest.json"
    state.write_once(registry, rows)
    return [str(registry), *[row["snapshot"] for row in rows]]


def check_inputs_unchanged(paths):
    if not paths:
        return
    for row in state.load(paths[0]):
        require(sha(absolute_file(row["path"])) == row["sha256"], "Check input changed during execution")


def validate_deliverables(value, repo, path, criterion_ids=None):
    entries = value.get("deliverables", {}).get(repo, [])
    if criterion_ids is not None:
        entries = [e for e in entries if set(e["criterion_ids"]) & set(criterion_ids)]
    if not entries:
        return []
    manifest_path = absolute_file(path)
    root = Path(value["_contract_path"]).parent.resolve()
    require(manifest_path.resolve().is_relative_to(root), "Deliverable manifest must be run-local")
    record = state.load(manifest_path)
    bound(value, repo, record)
    rows = record.get("deliverables")
    require(isinstance(rows, list) and len(rows) == len(entries), "Incomplete deliverable manifest")
    indexed = {row.get("id"): row for row in rows}
    require(len(indexed) == len(rows) and set(indexed) == {e["id"] for e in entries}, "Wrong deliverable IDs")
    sealed = [str(manifest_path)]
    for entry in entries:
        row = indexed[entry["id"]]
        require(row.get("path") == entry["path"], "Wrong deliverable path")
        snapshot = absolute_file(row.get("snapshot"))
        require(snapshot.resolve().is_relative_to(root), "Deliverable snapshot must be run-local")
        require(sha(snapshot) == row.get("sha256"), "Deliverable snapshot changed")
        state.guard_excluded_path(entry["path"], value["repo_paths"][repo],
                                  value.get("read_excludes", {}).get(repo, []))
        actual = absolute_file(entry["path"])
        try:
            relative = actual.resolve().relative_to(Path(value["repo_paths"][repo]).resolve()).as_posix()
        except ValueError:
            relative = None
        if relative is not None:
            require(not any(relative == p or relative.startswith(p + "/")
                            for p in value.get("read_excludes", {}).get(repo, [])),
                    "Deliverable intersects read_excludes")
        require(not actual.is_symlink() and sha(actual) == row["sha256"], "Published deliverable differs from snapshot")
        sealed.append(str(snapshot))
    return sealed


def bound(value, repo, record):
    require(isinstance(record, dict), "Bound artifact must be an object")
    require(record.get("schema_version") == VERSION and record.get("run_id") == value["run_id"]
            and record.get("repo") == repo, "Wrong artifact version/run/repo")


def seal_files(paths):
    return {str(absolute_file(p)): sha(p) for p in paths}


def check_seals(seals):
    for path, expected in seals.items():
        require(sha(absolute_file(path)) == expected, f"Immutable artifact changed: {path}")


def check_event_seals(value, repo, event, inherited_seals=None):
    """Keep proof files immutable while source snapshots advance through authorized edits."""
    seals = event.get("artifact_hashes", {})
    source_manifest_path = event.get("source_manifest")
    for path in seals:
        state.guard_excluded_path(path, value["repo_paths"][repo], value.get("read_excludes", {}).get(repo, []))
    if not source_manifest_path:
        check_seals(seals)
        return list(seals)

    state.guard_excluded_path(source_manifest_path, value["repo_paths"][repo],
                              value.get("read_excludes", {}).get(repo, []))
    historical = manifest(value, repo, source_manifest_path)
    repo_root = Path(value["repo_paths"][repo]).resolve()
    inherited_seals = inherited_seals or {}
    immutable = {}
    for path, expected in seals.items():
        candidate = Path(path)
        try:
            relative = candidate.resolve(strict=False).relative_to(repo_root).as_posix()
        except (OSError, ValueError):
            immutable[path] = expected
            continue

        source_entry = historical["files"].get(relative)
        if source_entry and source_entry.get("kind") == "file" and source_entry.get("sha256"):
            historically_bound = source_entry["sha256"] == expected
            inherited_bound = inherited_seals.get(path) == expected
            require(historically_bound or inherited_bound,
                    f"Source artifact seal does not match its historical manifest or prior state: {path}")
            continue
        immutable[path] = expected

    check_seals(immutable)
    return list(immutable)


def execution_binding(record):
    if not record.get("execution_record"):
        return
    execution_path = str(absolute_file(record["execution_record"]))
    require(execution_path in record.get("artifact_paths", []), "Execution record must be sealed")
    execution = state.load(execution_path)
    for field in ("producer", "command", "exit_code"):
        require(execution.get(field) == record.get(field), "Execution record differs: " + field)
    require(execution.get("log") in record.get("artifact_paths", []), "Execution log must be sealed")


def proof(value, repo, path, final, identifier, producer=None, expected_status="passed"):
    record = state.load(absolute_file(path))
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
    execution_binding(record)
    for field in ("environment", "data_scope"):
        if expected.get(field):
            require(record.get(field) == expected[field], f"Wrong evidence {field}")
    state.evidence(record, final)
    paths = [path, record["before_manifest"], record["after_manifest"], *record["artifact_paths"]]
    hashes = record.get("artifact_hashes")
    require(isinstance(hashes, dict) and set(hashes) == set(paths[1:]), "Missing evidence artifact hashes")
    check_seals(hashes)
    for dependency, dependency_path in record.get("dependency_manifests", {}).items():
        require(dependency in value["repos"], "Unknown evidence dependency")
        dep = manifest(value, dependency, dependency_path)
        _, excludes = (capture_policy(value["_contract_path"], dependency)
                       if value.get("_contract_path") else ([], []))
        state.match(dep, state.capture(value["repo_paths"][dependency], list(dep["files"]), excludes,
                                       value.get("read_excludes", {}).get(dependency, [])))
        paths.append(dependency_path)
    # A criterion spanning repositories must name the source versions it depends on.
    dependencies = set(expected.get("repos", [])) - {repo}
    require(dependencies <= set(record.get("dependency_manifests", {})), "Missing cross-repo source binding")
    return paths


def finding_proof(value, repo, path, final, identifier, kind):
    record = state.load(absolute_file(path))
    bound(value, repo, record)
    require(record.get("kind") == kind and record.get("status") == "passed", "Finding proof unverified")
    claims = record.get("claims", [])
    unique_ids(claims, "finding claims")
    require(identifier in {c["id"] for c in claims}, "Wrong finding proof")
    for claim in claims:
        state.text(claim.get("assertion"), "finding assertion")
    state.evidence(record, final)
    paths = [record["before_manifest"], record["after_manifest"], *record["artifact_paths"]]
    hashes = record.get("artifact_hashes")
    require(isinstance(hashes, dict) and set(hashes) == set(paths), "Missing finding artifact hashes")
    check_seals(hashes)
    return [path, *paths]


def check_set(value, repo, group, paths, final):
    require(isinstance(paths, list), f"Missing {group} evidence")
    sealed = []
    for item in value[group][repo]:
        matches = [p for p in paths if item["id"] in
                   {x.get("id") for x in state.load(absolute_file(p)).get("claims", [])}]
        require(matches, f"Missing check: {item['id']}")
        sealed += proof(value, repo, matches[-1], final, item["id"], "orchestrator")
    return sealed


def preflight(value, repo, path, initial):
    result = state.load(absolute_file(path))
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
    record = state.load(absolute_file(path))
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
            evidence_status = state.load(absolute_file(evidence_path)).get("status")
            if entry["status"] == "verified":
                evidence_status = "passed"
            require(evidence_status in {"passed", "failed", "unverified"}, "Invalid evidence status")
            sealed += proof(value, repo, evidence_path, final, entry["criterion_id"], expected_status=evidence_status)
    return sealed


def submission_evidence_errors(value, repo, path, final, key):
    record = state.load(absolute_file(path))
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
                status = state.load(absolute_file(path)).get("status")
                if entry.get("status") == "verified":
                    status = "passed"
                require(status in {"passed", "failed", "unverified"}, "Invalid evidence status")
                proof(value, repo, path, final, identifier, expected_status=status)
            except (ValueError, TypeError, KeyError, OSError) as error:
                errors.append(f"{identifier} / {path}: {error}")
    return errors


def review(value, repo, path, final, scope, prior_paths, run_root=None, evidence_seals=None):
    result = state.load(absolute_file(path))
    bound(value, repo, result)
    prior = [state.load(absolute_file(p)) for p in prior_paths]
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
    require(set(scope) <= set(covered), "Review omits scoped paths")
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
        if item["file"] not in scope:
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


def assessment(value, repo, path, final, initial=None):
    result = state.load(absolute_file(path))
    bound(value, repo, result)
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
            sealed += proof(value, repo, p, final, row["criterion_id"])
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


def history(directory):
    events = []
    for path in directory.glob("state-*.json"):
        match = re.fullmatch(r"state-(\d+)\.json", path.name)
        if match:
            record = state.load(path)
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


def historical_budget_sources(sources, repo, workflow, current_contract=None, visited_contracts=None):
    """Revalidate prior-run counters cited to carry budget usage into this run."""
    require(isinstance(sources, list) and sources, "Historical budget needs source runs")
    seen, validated, total = set(), [], 0
    for source in sources:
        require(isinstance(source, dict), "Invalid historical budget source")
        contract_path = absolute_file(source.get("contract_path"))
        require(current_contract is None or contract_path.resolve() != Path(current_contract).resolve(),
                "Historical budget cannot reference its current run")
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
        state_path = absolute_file(source.get("state_path"))
        require(state_path.parent.resolve() == (contract_path.parent / repo).resolve(),
                "Historical budget state is outside its repo history")
        prior_events = history(contract_path.parent / repo)
        require(prior_events and state_path.resolve() == prior_events[-1][1].resolve()
                and sha(state_path) == source.get("state_sha256"),
                "Historical budget state hash/path mismatch")
        audited = validate_history(contract_path, repo, visited_contracts)
        require(state_path.resolve() == Path(audited["state_path"]).resolve()
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


def budget_continuity(value, repo, contract_path, slice_id=None):
    """Validate an explicitly approved carryover without changing historical counters."""
    entries = value.get("budget_continuity", [])
    require(isinstance(entries, list), "Invalid budget continuity")
    keys = [(item.get("repo"), item.get("slice_id")) for item in entries if isinstance(item, dict)]
    require(len(keys) == len(entries) and len(set(keys)) == len(keys), "Duplicate budget continuity scope")
    matches = [item for item in entries if item.get("repo") == repo and item.get("slice_id") == slice_id]
    if not matches:
        return None
    item = matches[0]
    require(item.get("approved_by") == "user", "Budget continuity requires user approval")
    for key in ("work_item", "user_authorization", "reason"):
        state.text(item.get(key), key)
    timestamp(item.get("approved_at"))
    used, limit = item.get("used_passes"), item.get("effective_limit")
    require(type(used) is int and type(limit) is int and 0 <= used <= limit,
            "Invalid continuity usage or limit")
    root = value["repo_paths"][repo]
    exclusions = value.get("read_excludes", {}).get(repo, [])
    state.guard_excluded_path(item.get("source_contract_path"), root, exclusions)
    source = absolute_file(item.get("source_contract_path"))
    require(source.resolve() != Path(contract_path).resolve(), "Continuity cannot reference current run")
    require(sha(source) == item.get("source_contract_sha256"), "Continuity contract seal mismatch")
    old = state.load(source)
    require(old.get("workflow") == value["workflow"] and repo in old.get("repos", [])
            and source.parent.name == old.get("run_id"), "Continuity source run mismatch")
    old = dict(old)
    old_excludes = dict(old.get("read_excludes", {}))
    old_excludes[repo] = sorted(set(old_excludes.get(repo, [])) |
                                set(value.get("read_excludes", {}).get(repo, [])))
    old["read_excludes"] = old_excludes
    source_slice = item.get("source_slice_id")
    require((slice_id is None) == (source_slice is None), "Continuity scope kind mismatch")
    if source_slice is not None:
        state.relative_path(source_slice)
    directory = source.parent / repo
    if source_slice is not None:
        directory = directory / "slices" / source_slice
    events = history(directory)
    require(events, "Continuity source history missing")
    state.guard_excluded_path(item.get("source_state_path"), root, exclusions)
    source_state = absolute_file(item.get("source_state_path"))
    require(source_state.resolve() == events[-1][1].resolve()
            and sha(source_state) == item.get("source_state_sha256"), "Continuity state seal mismatch")
    inherited = {}
    prior_count = 0
    prior_status = None
    for _, _, event in events:
        bound(old, repo, event)
        require(event.get("artifact_hashes", {}).get(str(source)) == item["source_contract_sha256"],
                "Continuity history does not seal its contract")
        if source_slice is not None:
            require(event.get("slice_id") == source_slice, "Continuity slice mismatch")
            status = event.get("status")
            require((prior_status is None and status == "implemented") or
                    (prior_status is not None and (status in SLICE_TRANSITIONS.get(prior_status, set())
                                                   or status in {"blocked", "capped"}
                                                   or (prior_status == "capped" and status == "needs-fix"
                                                       and event.get("is_continuation") is True))),
                    "Invalid continuity slice transition")
            prior_status = status
            if event.get("status") == "edited-unverified" and event.get("action") == "review-fix":
                prior_count += 1
            require(event.get("review_fix_count", 0) == prior_count,
                    "Continuity slice counter is inconsistent with events")
        check_event_seals(old, repo, event, inherited)
        inherited = event.get("artifact_hashes", {})
    state.guard_excluded_path(item.get("decision_path"), root, exclusions)
    decision = absolute_file(item.get("decision_path"))
    require(sha(decision) == item.get("decision_sha256"), "Continuity decision seal mismatch")
    require(used >= events[-1][2].get("review_fix_count", 0), "Continuity cannot reduce recorded usage")
    # Reject cycles and repeated sources, including through earlier continuity declarations.
    seen = {Path(contract_path).resolve()}
    current = source
    while True:
        require(current.resolve() not in seen, "Budget continuity cycle")
        seen.add(current.resolve())
        prior = state.load(current).get("budget_continuity", [])
        require(isinstance(prior, list), "Invalid prior continuity")
        scoped = [entry for entry in prior if entry.get("repo") == repo
                  and entry.get("slice_id") == source_slice]
        require(len(scoped) <= 1, "Duplicate prior continuity")
        if not scoped:
            break
        previous = scoped[0]
        require(previous.get("work_item") == item["work_item"], "Continuity work item mismatch")
        require(used >= previous.get("used_passes", 0) + events[-1][2].get("review_fix_count", 0),
                "Continuity cannot reduce cumulative usage")
        state.guard_excluded_path(previous.get("source_contract_path"), root, exclusions)
        current = absolute_file(previous.get("source_contract_path"))
        require(sha(current) == previous.get("source_contract_sha256"), "Continuity ancestor seal mismatch")
        source_slice = previous.get("source_slice_id")
        ancestor_directory = current.parent / repo
        if source_slice is not None:
            state.relative_path(source_slice)
            ancestor_directory = ancestor_directory / "slices" / source_slice
        events = history(ancestor_directory)
        require(events and str(events[-1][1]) == previous.get("source_state_path")
                and sha(events[-1][1]) == previous.get("source_state_sha256"),
                "Continuity ancestor state seal mismatch")
    if item.get("source_slice_id") is None:
        validate_history(source, repo, read_excludes=old_excludes[repo])
    elif old.get("budget_continuity"):
        budget_continuity(old, repo, source, item.get("source_slice_id"))
    return item


def review_budget(events, contract_path=None, value=None, repo=None, visited_contracts=None):
    """Derive repo-level review budget only from grants applied in immutable states."""
    grants, seen_paths, predecessors = [], set(), set()
    continuity = budget_continuity(value, repo, contract_path) if value is not None and contract_path else None
    base_limit = (continuity["effective_limit"] - continuity["used_passes"]) if continuity else REVIEW_FIX_BUDGET
    carried_over, carried_sources = 0, []
    active_contracts = set(visited_contracts or ())
    if contract_path:
        active_contracts.add(Path(contract_path).resolve())
    run_root = Path(events[0][1]).parent.parent.resolve() if events else None
    for index, (_, event_path, event) in enumerate(events):
        grant = event.get("budget_grant")
        require(bool(event.get("budget_grant_applied")) == (grant is not None),
                "Review-budget grant application marker mismatch")
        if grant is not None:
            require(isinstance(grant, dict), "Malformed applied review-budget grant")
            path = absolute_file(grant.get("path"))
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
            require(event.get("status") == context_event.get("status")
                    and event.get("source_fingerprint") == predecessor_event.get("source_fingerprint"),
                    "Grant event changed its review context or source")
            require(data.get("review_path") == context_event.get("review_path")
                    and data.get("review_round") == context_event.get("latest_review_round"),
                    "Grant review context binding mismatch")
            review_path = absolute_file(data.get("review_path"))
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
                    assessment_file = absolute_file(assessment_path)
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
                    active_contracts)
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
    return max(0, base_limit + sum(item["additional_passes"] for item in grants)
               - carried_over), grants


def apply_budget_grant(contract_path, value, repo, request, events, previous, data):
    """Validate a user's one-time run-local grant and return its sealed summary."""
    require(events and request.get("status") != "edited-unverified", "Grant application needs an existing state")
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
    requested_historical, validated_sources = (historical_budget_sources(
        historical_sources, repo, value["workflow"], contract_path,
        {Path(contract_path).resolve()}) if historical_sources else (0, []))
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
    require(data["source_fingerprint"] == previous["source_fingerprint"], "Grant cannot accompany source edits")
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
    elif before in {"reviewed", "gates-passed", "verified"}:
        target = before
        require(grant.get("purpose") == "plan-gap", "Plan-gap continuation needs a plan-gap grant")
        context = previous
    elif before == "capped":
        require(len(events) >= 2, "Capped state has no resumable context")
        context = events[-2][2]
        target = context.get("status")
        require(target in {"needs-fix", "reviewed", "gates-passed", "verified"},
                "Capped state has no resumable review or plan-gap context")
        require(grant.get("purpose") == ("review-fix" if target == "needs-fix" else "plan-gap"),
                "Grant purpose does not match capped state")
    else:
        raise ValueError("Review-budget grant can only continue needs-fix or capped state")
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
    return [str(p) for p in paths] + [str(previous_path), str(original_path)]


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


def transition(contract_path, repo, request_path, out):
    value = contract(contract_path)
    require(repo in value["repos"], "Unknown transition repo")
    directory = Path(contract_path).parent / repo
    events = history(directory)
    previous = events[-1][2] if events else {}
    inherited_seals = {}
    for _, path, event in events:
        bound(value, repo, event)
        check_event_seals(value, repo, event, inherited_seals)
        inherited_seals = event.get("artifact_hashes", {})
    budget_limit, applied_grants = review_budget(events, contract_path, value, repo)
    request = state.load(absolute_file(request_path))
    require(isinstance(request, dict), "Transition request must be an object")
    protected = {"review_fingerprint", "artifact_hashes", "initial_manifest", "code_only", "schema_version",
                 "run_id", "repo", "sequence", "at", "current_author_agent_id", "review_fix_limit",
                 "review_fix_grants", "budget_grant", "amendment_sha256", "amendment_applied"}
    require(not protected & set(request), "Request overrides certified lifecycle fields")
    status = request.get("status")
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
        (before in {"needs-fix", "reviewed", "gates-passed", "verified"} and status == before) or
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
    require(gate_correction or valid_grant_transition or is_requirement_amendment or identity_correction
            or status in TERMINAL
            or status in TRANSITIONS.get(before, set()),
            f"Illegal transition: {before} -> {status}")
    require(not identity_correction or before == "needs-fix" and not is_grant_transition
            and request.get("action") == "review-fix"
            and not {"review_path", "capsule_handoff", "gate_evidence_paths", "assessment_path"} & set(request),
            "Author-identity recovery cannot modify review, source, or grant context")
    require(before != "capped" or valid_grant_transition,
            "Exhausted run needs an explicit user-approved budget grant")
    require(not is_grant_transition or status not in TERMINAL,
            "Grant application must resume a concrete review/plan-gap state")
    require(before is not None or status == "initialized", "Run must be initialized first")
    if before == "ready" and status == "pre-review-passed":
        require(value.get("from_review") is True, "Implementation must precede pre-review")
    expected_out = directory / f"state-{len(events) + 1}.json"
    require(Path(out).resolve() == expected_out.resolve(), "Transition output must be next immutable state")
    scope_path = request.get("scope_path", previous.get("scope_path"))
    scope = state.scope_paths(absolute_file(scope_path))
    source_path = request.get("source_manifest")
    exclusions = previous.get("excludes", request.get("excludes", []))
    dependency_scope_path = request.get("source_scope_path", previous.get("source_scope_path", scope_path))
    dependency_scope = state.scope_paths(absolute_file(dependency_scope_path))
    require(set(previous.get("scope", [])) <= set(scope), "Touched scope must not shrink")
    final = fresh(value, repo, source_path, sorted(set(scope) | set(dependency_scope)), exclusions)
    initial_path = previous.get("initial_manifest", source_path)
    initial = manifest(value, repo, initial_path)
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
            delta_paths = {name for name in set(before_amendment["files"]) | set(final["files"])
                           if before_amendment["files"].get(name) != final["files"].get(name)}
            require(delta_paths <= set(additions), "Scope amendment changed unapproved source")
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
    changed = {p for p in set(initial["files"]) | set(final["files"])
               if initial["files"].get(p) != final["files"].get(p)}
    require(changed <= set(scope), "Changes outside touched scope; resolve ownership first")
    if policy:
        accepted = [event for _, _, event in events
                    if event["status"] in {"implemented", "edited-unverified"}]
        certification = {"pre-review-passed", "needs-fix", "reviewed", "gates-passed", "verified"}
        if status == "implemented":
            require(not accepted and not any(event["status"] in certification for _, _, event in events),
                    "Source repairs cannot restart implementation; record edited-unverified")
        if status in certification:
            # A blocker/handoff may truthfully capture partially edited source, but must not
            # silently make that source the accepted implementation on unblock. Only initial
            # implementation or an explicitly recorded repair can advance this version anchor.
            anchor = accepted[-1]["source_fingerprint"] if accepted else initial["fingerprint"]
            require(final["fingerprint"] == anchor,
                    "Source changed without recorded repair; record edited-unverified with repair_phase_id "
                    "(or explicit from-review author_handoff)")
    sealed = [contract_path, source_path, request_path, *amendment_seals]
    for applied in value.get("_amendments", []):
        sealed.append(applied["path"])
    if policy and events:
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
    if is_grant_transition:
        target, grant_summary = apply_budget_grant(contract_path, value, repo, request, events, previous, data)
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
        for entry in delta["changes"]:
            require(entry["before"] == initial["files"].get(entry["path"], {"kind": "missing"}),
                    "First-touch baseline differs from run start; resolve concurrent edits")
    if status == "initialized":
        require(not events and not changed, "Initialization must precede implementation")
    if status in {"ready", "preflight-blocked"}:
        require(final["fingerprint"] == initial["fingerprint"], "Preflight must precede source edits")
        pre, paths = preflight(value, repo, request.get("preflight_path"), initial)
        require((pre["status"] == "passed") == (status == "ready"), "Wrong preflight transition")
        sealed += paths
    sliced = repo in value.get("slice_schedules", {})
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
    if status == "edited-unverified":
        action = request.get("action")
        require(action in EDITS - {"implement"}, "Missing edit cause")
        if value.get("from_review"):
            require(action == "plan-gap" and request.get("author_handoff"), "From-review fixes need author handoff")
        elif policy:
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
            require(data["review_fix_count"] < budget_limit, "Review-fix budget exhausted")
            data["review_fix_count"] += 1
            applied_path = request.get("applied_path")
            result = state.load(absolute_file(applied_path))
            bound(value, repo, result)
            findings = state.load(previous["review_path"])
            state.applied(result, findings)
            for item in result["applied"]:
                original = next(f for f in findings["findings"] if f["id"] == item["id"])
                require(item.get("verification_kind") == original["verification_kind"], "Applied kind mismatch")
                require(item.get("evidence_paths"), "Applied finding needs evidence")
                for p in item["evidence_paths"]:
                    sealed += finding_proof(value, repo, p, final, item["id"], original["verification_kind"])
            sealed.append(applied_path)
        if action == "plan-gap":
            require(data["review_fix_count"] < budget_limit, "Plan-gap repair budget exhausted")
            data["review_fix_count"] += 1
        if action == "gate-fix":
            require(data["gate_fix_count"] < 2, "Gate-fix budget exhausted")
            failure_path = request.get("gate_failure_path")
            failure = state.load(absolute_file(failure_path))
            claims = failure.get("claims", [])
            require(claims, "Gate fix needs recorded failed check")
            identifier = claims[0]["id"]
            allowed = {x["id"] for group in ("required_gates", "pre_review_checks") for x in value[group][repo]}
            require(identifier in allowed, "Gate failure does not name a declared gate")
            sealed += proof(value, repo, failure_path, manifest(value, repo, previous["source_manifest"]),
                            identifier, "orchestrator", expected_status="failed")
            data["gate_fix_count"] += 1
        for key in ("gate_evidence_paths", "assessment_path"):
            data.pop(key, None)
    if status == "pre-review-passed":
        sealed += matrix(value, repo, request.get("matrix_path", previous.get("matrix_path")), final)
        sealed += validate_deliverables(value, repo, request.get("deliverable_manifest_path", previous.get("deliverable_manifest_path")))
        sealed += check_set(value, repo, "pre_review_checks", request.get("pre_review_evidence_paths"), final)
    if status in {"needs-fix", "reviewed"} and not is_grant_transition and not identity_correction:
        sealed += validate_deliverables(value, repo, previous.get("deliverable_manifest_path"))
        prior_paths = [e[2]["review_path"] for e in events
                       if e[2]["status"] in {"needs-fix", "reviewed"} and not e[2].get("budget_grant_applied")]
        result, paths = review(value, repo, request.get("review_path"), final, scope, prior_paths,
                               Path(contract_path).parent.resolve(), previous.get("artifact_hashes", {}))
        require(result["round"] == data["latest_review_round"] + 1, "Review rounds must increase by one")
        require(bool(result["findings"]) == (status == "needs-fix"), "Findings/status mismatch")
        data["latest_review_round"] = result["round"]
        data["review_fingerprint"] = final["fingerprint"]
        sealed += paths
    if status in {"gates-passed", "verified"}:
        sealed += validate_deliverables(value, repo, previous.get("deliverable_manifest_path"))
        require(previous.get("review_fingerprint") == final["fingerprint"], "Review is stale")
        require(not state.load(absolute_file(data["review_path"]))["findings"], "Review has open findings")
        sealed += check_set(value, repo, "required_gates", data.get("gate_evidence_paths"), final)
    if status == "verified":
        require(not data.get("code_only"), "Code-only authorization cannot certify completion")
        sealed += assessment(value, repo, request.get("assessment_path", data.get("assessment_path")), final, initial)
    if status in TERMINAL:
        state.text(request.get("reason"), "blocker/handoff reason")
    data["artifact_hashes"] = {**previous.get("artifact_hashes", {}), **seal_files(sealed)}
    # Scope is intentionally mutable; the event captures its values, not a mutable-file seal.
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


def in_scope(path, patterns):
    for pattern in patterns:
        if not re.search(r"[*?]", pattern):
            if path == pattern.rstrip("/") or path.startswith(pattern.rstrip("/") + "/"):
                return True
            continue
        if re.fullmatch(glob_regex(pattern), path):
            return True
    return False


def slice_matrix(value, repo, item, path, final):
    record = state.load(absolute_file(path))
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
    path = absolute_file(path)
    require(path.resolve().is_relative_to(run), "Integration resume authorization must be run-local")
    require(authorization.get("resume_authorization_sha256") == sha(path), "Integration resume authorization changed")
    resume = state.load(path)
    bound(value, repo, resume)
    require(resume.get("slice_id") == slice_id, "Wrong integration resume slice")
    state.text(resume.get("user_authorization"), "human integration resume authorization")
    paused_path = absolute_file(resume.get("paused_state_path"))
    require(resume.get("paused_state_sha256") == sha(paused_path), "Wrong paused integration state")
    found = [event for _, p, event in events if str(p) == str(paused_path)]
    require(len(found) == 1 and found[0]["status"] == "edited-unverified"
            and found[0].get("action") == "integration-fix", "Resume needs a paused integration repair")
    paused = found[0]
    require(resume.get("author_agent_id") == paused["author_agent_id"], "Wrong integration resume author")
    prior_path = absolute_file(resume.get("superseded_checkpoint_path"))
    require(paused.get("integration_path") == str(prior_path)
            and resume.get("superseded_checkpoint_sha256") == sha(prior_path), "Wrong superseded checkpoint")
    prior = state.load(prior_path)
    prior_auth_path = absolute_file(prior.get("authorization_path"))
    require(prior.get("authorization_sha256") == sha(prior_auth_path), "Superseded authorization changed")
    prior_auth = state.load(prior_auth_path)
    require(all(authorization.get(key) == prior_auth.get(key) for key in
                ("starting_slices", "edit_paths", "recertify_slices", "partial_slices",
                 "contract_sha256", "schedule_sha256")), "Resume cannot change alignment permissions")
    require(resume.get("before_manifest") == paused["source_manifest"]
            and resume.get("before_manifest_sha256") == sha(paused["source_manifest"]),
            "Resume must bind the paused source manifest")
    before = manifest(value, repo, paused["source_manifest"])
    permitted = resume.get("partial_repair_paths")
    require(isinstance(permitted, dict) and permitted, "Resume needs explicit partial repair paths")
    paths = set()
    for owner, names in permitted.items():
        require(owner != slice_id and not history(run / repo / "slices" / owner),
                "Resume repair must belong to an unsubmitted partial slice")
        require(isinstance(names, list) and names and set(names) <= set(prior_auth.get("partial_slices", {}).get(owner, [])),
                "Resume repair exceeds the checkpointed partial paths")
        paths.update(names)
    changed = {name for name in set(before["files"]) | set(start["files"])
               if before["files"].get(name, {"kind": "missing"}) != start["files"].get(name, {"kind": "missing"})}
    require(changed <= paths, "Resume changed source outside the authorized partial repair")
    require(before["head"] == start["head"], "Integration resume HEAD changed")
    require(timestamp(paused["at"]) <= timestamp(resume.get("approved_at")) <=
            timestamp(authorization.get("approved_at")), "Invalid integration resume approval time")
    require(manifest(value, repo, authorization["start_manifest_path"])["fingerprint"] == start["fingerprint"],
            "Resume checkpoint must match the authorized repaired snapshot")
    return (str(paused_path), paused), [str(path), str(paused_path), str(prior_path),
                                      str(prior_auth_path), paused["source_manifest"]]


def integration_checkpoint(value, repo, contract_path, plan, items, slice_id, previous, events, request, final):
    """Bind a sequential reopen to certified sibling versions, never a replacement base."""
    path = request.get("integration_path") or previous.get("integration_path")
    if not path:
        return set(), []
    path = absolute_file(path)
    run = Path(contract_path).parent.resolve()
    require(path.resolve().is_relative_to(run), "Integration checkpoint must be run-local")
    checkpoint = state.load(path)
    bound(value, repo, checkpoint)
    require(checkpoint.get("slice_id") == slice_id, "Wrong integration slice")
    require(checkpoint.get("schedule_sha256") == plan["schedule_sha256"]
            and checkpoint.get("contract_sha256") == sha(contract_path), "Wrong integration contract/schedule")
    state.text(checkpoint.get("user_authorization"), "integration authorization")
    authorization_path = absolute_file(checkpoint.get("authorization_path"))
    require(authorization_path.resolve().is_relative_to(run), "Integration authorization must be run-local")
    authorization = state.load(authorization_path)
    bound(value, repo, authorization)
    state.text(authorization.get("user_authorization"), "human integration authorization")
    require(checkpoint.get("authorization_sha256") == sha(authorization_path), "Integration authorization changed")
    require(authorization.get("contract_sha256") == sha(contract_path)
            and authorization.get("schedule_sha256") == plan["schedule_sha256"], "Foreign integration authorization")
    anchor = absolute_file(checkpoint.get("previous_state_path"))
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
    require(isinstance(allowed, list) and all(in_scope(name, items[slice_id]["write_scope"]) for name in allowed),
            "Invalid authorized integration paths")
    if mode == "repair":
        require(allowed, "Slice has no authorized integration edits")
    else:
        require(slice_id in authorization.get("recertify_slices", []), "Dependency recheck is not authorized")
    require(checkpoint.get("author_agent_id") == anchored.get("author_agent_id")
            and request.get("author_agent_id", anchored["author_agent_id"]) == anchored["author_agent_id"],
            "Wrong integration author")
    start_path = absolute_file(checkpoint.get("start_manifest"))
    require(start_path.resolve().is_relative_to(run), "Integration manifest must be run-local")
    require(checkpoint.get("start_manifest_sha256") == sha(start_path), "Integration start manifest changed")
    start = manifest(value, repo, str(start_path))
    approved = timestamp(authorization.get("approved_at"))
    require(timestamp(anchored["at"]) <= approved <= timestamp(checkpoint.get("at")) <= datetime.now(timezone.utc),
            "Invalid integration authorization time")
    auth_start_path = absolute_file(authorization.get("start_manifest_path"))
    require(auth_start_path.resolve().is_relative_to(run), "Integration authorization snapshot must be run-local")
    require(authorization.get("start_manifest_sha256") == sha(auth_start_path),
            "Integration authorization snapshot changed")
    auth_start = manifest(value, repo, str(auth_start_path))
    anchored_source = manifest(value, repo, anchored["source_manifest"])
    require(auth_start["head"] == anchored_source["head"] == start["head"] == final["head"],
            "Integration HEAD changed")
    require(all(start["files"].get(name, {"kind": "missing"}) ==
                final["files"].get(name, {"kind": "missing"})
                for name in set(start["files"]) | set(final["files"])
                if not in_scope(name, items[slice_id]["write_scope"])),
            "Integration foreign source changed outside target scope")
    resumed, resume_seals = integration_resume(value, repo, run, authorization, start, events, slice_id)
    if resumed is not None:
        prior_checkpoint = state.load(absolute_file(resumed[1]["integration_path"]))
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
        for name in anchored["touched"]:
            require(start["files"].get(name, {"kind": "missing"}) ==
                    manifest(value, repo, origin["source_manifest"])["files"].get(name, {"kind": "missing"}),
                    "Integration start differs from the anchored slice source")
            if resumed is not None:
                require(final["files"].get(name, {"kind": "missing"}) ==
                        start["files"].get(name, {"kind": "missing"}),
                        "Integration resume cannot add target source edits")
    require(request.get("base_manifest", previous["base_manifest"]) == previous["base_manifest"],
            "Integration cannot replace the original slice base")
    require(set(request.get("touched", previous["touched"])) == set(previous["touched"]),
            "Integration checkpoint cannot expand the slice touched scope")
    own_changes = {name for name in set(start["files"]) | set(final["files"])
                   if in_scope(name, items[slice_id]["write_scope"])
                   and start["files"].get(name) != final["files"].get(name)}
    require(own_changes <= set(allowed) if mode == "repair" else not own_changes,
            "Source changed outside the authorized integration edits")
    foreign = set()
    sealed = [str(path), str(authorization_path), str(anchor), str(start_path), str(auth_start_path), *resume_seals]
    bindings = checkpoint.get("slice_states")
    require(isinstance(bindings, dict) and slice_id not in bindings, "Invalid integration sibling bindings")
    require(set(bindings) == set(authorization.get("starting_slices", {})) - {slice_id},
            "Integration checkpoint omits an authorized sibling binding")
    for identifier, binding in bindings.items():
        require(identifier in items, "Unknown integration sibling")
        directory = run / repo / "slices" / identifier
        sibling_events = history(directory)
        require(sibling_events, "Integration sibling has no history")
        event_path, event = sibling_events[-1][1:]
        require(Path(binding["path"]).resolve() == event_path.resolve()
                and binding["sha256"] == sha(event_path),
                "Integration sibling state changed")
        require(event.get("status") == "accepted", "Integration sibling is not accepted")
        inherited = {}
        for _, _, historical in sibling_events:
            bound(value, repo, historical)
            require(historical.get("slice_id") == identifier
                    and historical.get("schedule_sha256") == plan["schedule_sha256"], "Foreign sibling history")
            check_event_seals(value, repo, historical, inherited)
            inherited = historical.get("artifact_hashes", {})
        for name, recorded in event["accepted_files"].items():
            require(in_scope(name, items[identifier]["write_scope"]), "Sibling path outside its approved scope")
            require(start["files"].get(name, {"kind": "missing"}) == recorded,
                    "Integration sibling does not match its acceptance")
            require(final["files"].get(name, {"kind": "missing"}) == recorded,
                    "Integration sibling source changed")
            foreign.add(name)
        sealed.append(str(event_path))
    partial = checkpoint.get("partial_slices", {})
    require(isinstance(partial, dict), "Invalid partial integration slices")
    authorized_partial = authorization.get("partial_slices", {})
    require(partial == authorized_partial, "Integration checkpoint omits authorized partial provenance")
    for identifier, paths in partial.items():
        require(identifier in items and identifier != slice_id and identifier not in bindings,
                "Invalid partial slice owner")
        require(not history(run / repo / "slices" / identifier), "Partial slice already has lifecycle history")
        require(isinstance(paths, list) and paths == authorized_partial.get(identifier),
                "Partial integration paths lack explicit authorization")
        for name in paths:
            require(in_scope(name, items[identifier]["write_scope"]), "Partial path outside its approved scope")
            recorded = auth_start["files"].get(name, {"kind": "missing"})
            require(recorded != {"kind": "missing"}, "Partial integration path missing from authorized snapshot")
            require(start["files"].get(name) == recorded and final["files"].get(name) == recorded,
                    "Partial integration source changed")
            foreign.add(name)
        sealed.append(authorization["start_manifest_path"])
    base = manifest(value, repo, anchored["base_manifest"])
    foreign_at_start = {name for name in set(base["files"]) | set(start["files"])
                        if not in_scope(name, items[slice_id]["write_scope"])
                        and base["files"].get(name) != start["files"].get(name)}
    require(foreign_at_start <= foreign, "Integration checkpoint omits foreign source provenance")
    return foreign, sealed


def predecessor_provenance(value, repo, contract_path, items, slice_id, final, request):
    """First implementation may include source accepted by its dependencies, pinned to each accepted state."""
    bindings = request.get("predecessor_states")
    require(isinstance(bindings, dict) and bindings, "Predecessor provenance needs predecessor_states")
    require(set(bindings) == set(items[slice_id]["depends_on"]),
            "Predecessor provenance must bind every dependency exactly")
    run = Path(contract_path).parent.resolve()
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
            require(in_scope(name, items[identifier]["write_scope"]), "Predecessor path outside its approved scope")
            covered[name] = recorded
        sealed.append(str(event_path))
    # Only paths whose current bytes equal the predecessor's accepted bytes are foreign-provenanced.
    matched = {name for name, recorded in covered.items()
               if final["files"].get(name, {"kind": "missing"}) == recorded}
    return matched, sealed


def slice_transition(contract_path, repo, slice_id, schedule_path, request_path, out):
    """Advance one slice through its own bounded lifecycle (own review budget), sequentially.

    The repo lifecycle is unchanged: it starts after every slice is accepted (see slice_gate) and keeps its own
    final review/fix budget over the whole repo diff.
    """
    value = contract(contract_path)
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
    if "deliverable_manifest_path" in request:
        require(status == "pre-review-passed", "Deliverable manifest can change only before review")
    require("capsule_handoff" not in request or request.get("status") == "edited-unverified",
            "Capsule handoff requires edited-unverified")
    before = previous.get("status")
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
    elif continuation_limit is None and not dependency_recheck:
        require(status in SLICE_TRANSITIONS.get(before, set()), f"Illegal slice transition: {before} -> {status}")
    require(before not in {"blocked", "capped"} or continuation_limit is not None,
            "Exhausted slice needs explicit human continuation")
    expected_out = directory / f"state-{len(events) + 1}.json"
    require(Path(out).resolve() == expected_out.resolve(), "Slice transition output must be next immutable state")
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
            require(sorted(request.get("touched", [])) == sorted(barrier["owned"][slice_id]),
                    "Touched files must equal what the barrier attributed to this slice")
        for other, paths in barrier["owned"].items():
            if other != slice_id:
                siblings |= set(paths)
    base_path = request.get("base_manifest") or previous.get("base_manifest")
    base = manifest(value, repo, base_path)
    touched = sorted(set(previous.get("touched", [])) | set(request.get("touched", [])))
    require(set(previous.get("touched", [])) <= set(touched), "Slice touched files must not shrink")
    outside = [p for p in touched if not in_scope(p, item["write_scope"])]
    require(not outside, "Touched file outside slice write_scope: " + ", ".join(outside))
    final = fresh(value, repo, request.get("source_manifest"), touched)
    if continuation_limit is not None:
        require(final["fingerprint"] == previous["source_fingerprint"], "Continuation source must be unchanged")
        require(touched == previous["touched"] and base_path == previous["base_manifest"],
                "Continuation cannot change source scope or base")
    foreign, integration_seals = integration_checkpoint(
        value, repo, contract_path, plan, items, slice_id, previous, events, request, final)
    require(not dependency_recheck or integration_seals, "Dependency recheck needs integration authorization")
    if dependency_recheck:
        require(all(final["files"].get(name, {"kind": "missing"}) == recorded
                    for name, recorded in previous["accepted_files"].items()),
                "Dependency recheck cannot change this slice's source")
    bound_predecessors = previous.get("predecessor_states")
    if "predecessor_states" in request:
        require(status == "implemented" and not events,
                "Predecessor provenance only accompanies the first implementation")
        bound_predecessors = request["predecessor_states"]
    predecessor, predecessor_seals = set(), []
    if bound_predecessors is not None:
        predecessor, predecessor_seals = predecessor_provenance(
            value, repo, contract_path, items, slice_id, final, {"predecessor_states": bound_predecessors})
    changed = {p for p in set(base["files"]) | set(final["files"]) if base["files"].get(p) != final["files"].get(p)}
    require(changed <= set(touched) | siblings | foreign | predecessor,
            "Changes outside the slice's declared touched files: "
            + ", ".join(sorted(changed - set(touched) - siblings - foreign - predecessor)))
    data = {**previous, **request, "schema_version": VERSION, "run_id": value["run_id"], "repo": repo,
            "slice_id": slice_id, "sequence": len(events) + 1, "at": datetime.now(timezone.utc).isoformat(),
            "schedule_sha256": plan["schedule_sha256"], "base_manifest": base_path, "touched": touched,
            "batch_barrier_path": barrier_path,
            "source_fingerprint": final["fingerprint"]}
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
    if status == "edited-unverified":
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
    if status == "pre-review-passed":
        sealed += slice_matrix(value, repo, item, request.get("matrix_path", previous.get("matrix_path")), final)
        sealed += validate_deliverables(value, repo, request.get("deliverable_manifest_path", previous.get("deliverable_manifest_path")),
                                       {c["criterion_id"] for c in item["contributions"]})
        sealed += check_set(value, repo, "pre_review_checks", request.get("pre_review_evidence_paths"), final)
    if status in {"needs-fix", "reviewed"} and continuation_limit is None:
        sealed += validate_deliverables(value, repo, previous.get("deliverable_manifest_path"),
                                       {c["criterion_id"] for c in item["contributions"]})
        prior_paths = [e[2]["review_path"] for e in events
                       if e[2]["status"] in {"needs-fix", "reviewed"} and not e[2].get("is_continuation")]
        result, paths = review(value, repo, request.get("review_path"), final, touched, prior_paths,
                               Path(contract_path).parent.resolve(), previous.get("artifact_hashes", {}))
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
    if status == "accepted":
        require(previous.get("review_fingerprint") == final["fingerprint"], "Slice review is stale")
        accepted_review = state.load(absolute_file(previous["review_path"]))
        require(not accepted_review["findings"], "Slice review has open findings")
        contributions = {c["criterion_id"] for c in item["contributions"]}
        for applied in value.get("_amendments", []):
            if applied["criterion_id"] not in contributions:
                continue
            require(accepted_review.get("amendment_sha256") in requirement_amendment_suffix(value, applied["criterion_id"]),
                    "Slice acceptance requires review of amended requirement")
        data["accepted_files"] = {p: final["files"].get(p, {"kind": "missing"}) for p in touched}
        data["dependency_states"] = {}
        require(set(item["depends_on"]) <= accepted_slices(contract_path, repo, set(item["depends_on"])),
                "Slice predecessors need current source and dependency acceptance")
        for dep in item["depends_on"]:
            dep_events = history(Path(contract_path).parent / repo / "slices" / dep)
            require(dep_events and dep_events[-1][2]["status"] == "accepted", f"Predecessor not accepted: {dep}")
            data["dependency_states"][dep] = sha(dep_events[-1][1])
    data["artifact_hashes"] = {**previous.get("artifact_hashes", {}), **seal_files([p for p in sealed if p])}
    state.write_once(out, data)
    return data


def accepted_slices(contract_path, repo, ids):
    value = contract(contract_path)
    root = Path(value["repo_paths"][repo]).resolve()
    memo = {}

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
        for dep, expected in event["dependency_states"].items():
            dep_events = history(Path(contract_path).parent / repo / "slices" / dep)
            valid = (valid and bool(dep_events) and sha(dep_events[-1][1]) == expected
                     and current(dep, visiting | {identifier}))
        memo[identifier] = valid
        return valid

    return {identifier for identifier in ids if current(identifier, set())}


def slice_gate(contract_path, repo, schedule_path, initial_path, source_path, out, amendment_path=None):
    """Certify that every slice is accepted, current and not stale before the repo-level lifecycle starts."""
    value = contract(contract_path)
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
    for identifier, (_, path, event) in latest.items():
        for name, recorded in event["accepted_files"].items():
            require(final["files"].get(name, {"kind": "missing"}) == recorded,
                    f"Slice {identifier} is stale: {name} changed after acceptance; re-review it")
        for dep, dep_sha in event["dependency_states"].items():
            require(sha(latest[dep][1]) == dep_sha, f"Slice {identifier} is stale: predecessor {dep} was re-accepted")
    changed = {p for p in set(initial["files"]) | set(final["files"]) if initial["files"].get(p) != final["files"].get(p)}
    require(changed <= touched | set(additions), "Repo changes not owned by any accepted slice: " +
            ", ".join(sorted(changed - touched - set(additions))))
    if amendment_path:
        before = manifest(value, repo, state.load(amendment_path)["before_manifest"])
        amendment_delta = {p for p in set(before["files"]) | set(final["files"])
                           if before["files"].get(p) != final["files"].get(p)}
        require(amendment_delta <= set(additions), "Scope amendment changed unapproved source")
    record = {"schema_version": VERSION, "run_id": value["run_id"], "repo": repo,
                           "source_fingerprint": final["fingerprint"], "slice_authors": authors,
                           "amendment_sha256": value.get("_amendment", {}).get("sha256"),
                           "slices": {i: sha(p) for i, (_, p, _) in latest.items()},
                           "touched_union": sorted(touched), "cross_slice_criteria": plan["cross_slice_criteria"],
                           "schedule_sha256": plan["schedule_sha256"], "artifact_hashes": seal_files(sealed)}
    if amendment_path:
        record["scope_amendment_path"] = str(amendment_path)
        record["amended_paths"] = sorted(additions)
    state.write_once(out, record)


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
    changed = sorted(p for p in set(base["files"]) | set(final["files"]) if base["files"].get(p) != final["files"].get(p))
    owned, violations = {i: [] for i in start["members"]}, []
    if base.get("head") != final.get("head"):
        violations.append("git HEAD moved during the batch")
    for path in changed:
        owners = [i for i in start["members"] if in_scope(path, start["write_scopes"][i])]
        if len(owners) == 1:
            owned[owners[0]].append(path)
        else:
            violations.append(f"{path}: " + ("no member owns it" if not owners else "several members own it"))
    if not violations and changed:
        try:
            delta = state.delta(value["repo_paths"][repo], start["baseline_dir"], changed)
        except ValueError as error:
            violations.append(str(error))
        else:
            for entry in delta["changes"]:
                if entry["before"] != base["files"].get(entry["path"], {"kind": "missing"}):
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
    value = contract(contract_path)
    require(repo in value["repos"], "Unknown slice repo")
    path = absolute_file(schedule_path)
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
            scope_prefix(scope)
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
    return {"groups": groups, "cross_slice_criteria": sorted(cross), "schedule_sha256": sha(path)}


def validate_history(contract_path, repo, visited_contracts=None, read_excludes=()):
    """Audit immutable lifecycle state and grant chains without recapturing today's source."""
    contract_file = absolute_file(contract_path)
    visited = set(visited_contracts or ())
    require(contract_file.resolve() not in visited,
            "Historical budget reference cycle/current-run ancestry detected")
    visited.add(contract_file.resolve())
    value = state.load(contract_file)
    if read_excludes:
        value = dict(value)
        exclusions = dict(value.get("read_excludes", {}))
        exclusions[repo] = sorted(set(exclusions.get(repo, [])) | set(read_excludes))
        value["read_excludes"] = exclusions
    require(isinstance(value, dict) and value.get("schema_version") == VERSION
            and value.get("workflow") in {"ship", "fix"}, "Invalid historical workflow contract")
    require(contract_file.parent.name == value.get("run_id"), "Contract/run directory mismatch")
    require(repo in value.get("repos", []), "Unknown historical repo")
    events = history(contract_file.parent / repo)
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
        if prior_status is not None:
            require(status in TRANSITIONS.get(prior_status, set()) or status in TERMINAL
                    or grant_same_state or grant_resumes_capped or gate_correction,
                    "Invalid historical lifecycle transition")
        if status == "edited-unverified":
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
    for _, path, event in events:
        bound(value, repo, event)
        check_event_seals(value, repo, event, inherited)
        inherited = event.get("artifact_hashes", {})
    limit, grants = review_budget(events, contract_path, value, repo, visited)
    latest = events[-1][2]
    if latest.get("scope_amendment_path"):
        scope_amendment(contract_path, value, repo, latest["scope_amendment_path"], events,
                        manifest(value, repo, latest["initial_manifest"]))
    result = {"run_id": value["run_id"], "repo": repo, "latest_status": latest["status"],
            "review_fix_count": latest.get("review_fix_count", 0), "review_fix_limit": limit,
            "state_path": str(events[-1][1]), "state_sha256": sha(events[-1][1]),
            "granted_additional_passes": sum(item["additional_passes"] for item in grants),
            "grants": grants,
            "states": len(events), "status": "valid"}
    if historical_budget_used(grants):
        result["historical_used_passes"] = historical_budget_used(grants)
    return result


def review_budget_status(contract_path, repo):
    """Read-only pre-dispatch budget check bound to the repo's latest immutable state."""
    contract_file = absolute_file(contract_path)
    value = contract(contract_file)
    require(repo in value["repos"], "Unknown review-budget repo")
    events = history(contract_file.parent / repo)
    require(events, "No lifecycle history")
    inherited = {}
    for _, path, event in events:
        bound(value, repo, event)
        check_event_seals(value, repo, event, inherited)
        inherited = event.get("artifact_hashes", {})
    limit, grants = review_budget(events, contract_file, value, repo)
    latest_path, latest = events[-1][1:]
    scope = sorted(set(latest.get("scope", [])) | set(latest.get("source_scope", [])))
    current = state.capture(value["repo_paths"][repo], scope, latest.get("excludes", []),
                            value.get("read_excludes", {}).get(repo, []))
    recorded = manifest(value, repo, latest["source_manifest"])
    changed = sorted(name for name in set(current["files"]) | set(recorded["files"])
                     if current["files"].get(name, {"kind": "missing"})
                     != recorded["files"].get(name, {"kind": "missing"}))
    context = latest
    if latest.get("status") == "capped" and len(events) >= 2:
        context = events[-2][2]
    findings = []
    review_path = context.get("review_path")
    if context.get("status") == "needs-fix" and review_path:
        findings = sorted(item["id"] for item in state.load(absolute_file(review_path))["findings"])
    used = latest.get("review_fix_count", 0)
    historical_used = historical_budget_used(grants)
    result = {"run_id": value["run_id"], "repo": repo, "state_path": str(latest_path),
            "state_sha256": sha(latest_path), "latest_status": latest["status"],
            "limit": limit, "used_passes": used, "remaining_passes": max(0, limit - used),
            "exhausted": used >= limit, "open_finding_ids": findings,
            "source_matches_state": not changed, "changed_source_paths": changed,
            "grants": grants, "status": "valid"}
    continuity = budget_continuity(value, repo, contract_file)
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
    root = Path(contract_path).parent
    require(Path(out).resolve() == (root / "completion.json").resolve(), "Invalid completion path")
    repos = {}
    paths = [contract_path]
    for applied in value.get("_amendments", []):
        paths.append(applied["path"])
    for repo in value["repos"]:
        events = history(root / repo)
        require(events and events[-1][2]["status"] == "verified", f"Repo not verified: {repo}")
        latest = events[-1][2]
        effective_limit, grants = review_budget(events, contract_path, value, repo)
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
        check_set(value, repo, "required_gates", latest["gate_evidence_paths"], final)
        assessment(value, repo, latest["assessment_path"], final, manifest(value, repo, latest["initial_manifest"]))
        paths += validate_deliverables(value, repo, latest.get("deliverable_manifest_path"))
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
        repos[repo] = {"source_fingerprint": final["fingerprint"], "state_path": str(events[-1][1]),
                       "review_budget": budget_summary}
        paths += [str(events[-1][1]), *latest["artifact_hashes"]]
    if value.get("telemetry_policy", {}).get("run_local_seals"):
        require_run_local(value, root, paths)
    state.write_once(out, {"schema_version": VERSION, "run_id": value["run_id"],
                          "workflow": value["workflow"], "status": "completed",
                          "amendment": value.get("_amendment"),
                          "amendments": [dict(applied) for applied in value.get("_amendments", [])],
                          "at": datetime.now(timezone.utc).isoformat(), "repos": repos,
                          "artifact_hashes": seal_files(paths)})


def packet(contract_path, repo, scope_path, baseline_dir, out):
    value = contract(contract_path)
    require(repo in value["repos"], "Unknown packet repo")
    scope = state.scope_paths(scope_path)
    directory = Path(out)
    require(directory.is_absolute() and directory.resolve().is_relative_to(Path(contract_path).parent.resolve()),
            "Review packet must remain in run directory")
    require(not directory.exists(), "Review packet directory already exists")
    events = history(Path(contract_path).parent / repo)
    previous_state = events[-1][2] if events else {}
    source_scope = sorted(set(scope) | set(previous_state.get("source_scope", [])))
    excludes = previous_state.get("excludes", [])
    current = state.capture(value["repo_paths"][repo], source_scope, excludes, value.get("read_excludes", {}).get(repo, []))
    changes = state.delta(value["repo_paths"][repo], baseline_dir, scope)
    state.match(current, state.capture(value["repo_paths"][repo], source_scope, excludes,
                                       value.get("read_excludes", {}).get(repo, [])))
    reviews = [e[2] for e in events if e[2]["status"] in {"needs-fix", "reviewed"}
               and not e[2].get("budget_grant_applied")]
    previous = manifest(value, repo, reviews[-1]["source_manifest"]) if reviews else None
    changed = [p for p in current["files"] if previous is None
               or current["files"][p] != previous["files"].get(p)]
    if previous:
        changed += sorted(set(previous["files"]) - set(current["files"]))
    state.write_once(directory / "source.json", current)
    state.write_once(directory / "delta.json", changes)
    state.write_once(directory / "index.json", {
        "schema_version": VERSION, "run_id": value["run_id"], "repo": repo,
        "source_manifest": str(directory / "source.json"), "delta_path": str(directory / "delta.json"),
        "scope": scope, "changed_since_review": changed, "criteria": criteria_for(value, repo),
        "required_passes": value["required_passes"][repo],
        "prior_reviews": [r["review_path"] for r in reviews],
        "open_findings": state.load(reviews[-1]["review_path"])["findings"] if reviews else [],
    })


def record_evidence(contract_path, repo, request_path, out):
    record = state.load(absolute_file(request_path))
    return record_evidence_record(contract_path, repo, record, out)


def prepare_evidence_record(contract_path, repo, record):
    """Validate one evidence record and seal its artifacts without writing it yet."""
    value = contract(contract_path)
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
    state.evidence(prepared, final)
    execution_binding(prepared)
    prepared["artifact_hashes"] = seal_files([
        prepared["before_manifest"], prepared["after_manifest"], *prepared["artifact_paths"]])
    return prepared


def record_evidence_record(contract_path, repo, record, out):
    prepared = prepare_evidence_record(contract_path, repo, record)
    require(Path(out).resolve().is_relative_to(Path(contract_path).parent.resolve()),
            "Evidence must remain in run directory")
    state.write_once(out, prepared)
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
            require(not Path(out).exists(), f"Evidence output already exists: {out}")
            record = resolve_batch_record(entry)
            identifier = record.get("criterion_id") or \
                (record["claims"][0]["id"] if isinstance(record.get("claims"), list) and record["claims"] else None)
            prepared_record = prepare_evidence_record(contract_path, repo, record)
            identifier = prepared_record["criterion_id"]
            prepared.append((out, prepared_record))
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
        state.write_once(out, record)
    return results, not failed


def phase(contract_path, request_path, out):
    return phase_from_record(contract_path, state.load(absolute_file(request_path)), out)


def phase_from_flags(contract_path, out, *, phase_id, phase, event, repo=None, status=None,
                     end_reason=None, agent_id=None, round=None, attempt=None, session_id=None, at=None,
                     action=None, previous_state_path=None, previous_state_sha256=None,
                     checkpoint_path=None, capsule_path=None, slice_id=None):
    record = {"phase_id": phase_id, "phase": phase, "event": event, "repo": repo,
              "at": at or datetime.now(timezone.utc).isoformat()}
    for key, value in (("status", status), ("end_reason", end_reason), ("agent_id", agent_id),
                       ("round", round), ("attempt", attempt), ("session_id", session_id), ("action", action),
                       ("previous_state_path", previous_state_path),
                       ("previous_state_sha256", previous_state_sha256),
                       ("checkpoint_path", checkpoint_path), ("capsule_path", capsule_path), ("slice_id", slice_id)):
        if value is not None:
            record[key] = value
    return phase_from_record(contract_path, record, out)


def phase_from_record(contract_path, record, out):
    value = contract(contract_path)
    require(isinstance(record, dict), "Telemetry request must be an object")
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
            capsule_path = absolute_file(record.get("capsule_path"))
            require(capsule_path.resolve().is_relative_to(Path(contract_path).parent.resolve()), "Capsule must be run-local")
            capsule = state.load(capsule_path)
            bound(value, repo, capsule)
            require(capsule.get("written_by") == author
                    and capsule.get("source_fingerprint") == previous.get("source_fingerprint"),
                    "Repair phase capsule author/source mismatch")
            manifest = state.checked_manifest(absolute_file(capsule.get("manifest_path")))
            require(manifest["fingerprint"] == previous.get("source_fingerprint"), "Repair phase capsule manifest mismatch")
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
        record["start_sha256"] = sha(start_path)
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
               "checks": {}, "review_budget": None}
    errors = {}
    try:
        value = contract(contract_path)
        summary["run_id"] = value["run_id"]
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
    try:
        limit, grants = review_budget(events, contract_path, value, repo)
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
            if latest.get("pre_review_evidence_paths"):
                check_set(value, repo, "pre_review_checks", latest["pre_review_evidence_paths"], final)
                evidence["pre_review_checks"] = "passed"
            if latest.get("gate_evidence_paths"):
                check_set(value, repo, "required_gates", latest["gate_evidence_paths"], final)
                evidence["required_gates"] = "passed"
            if latest.get("assessment_path"):
                assessment(value, repo, latest["assessment_path"], final, initial)
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
        value = contract(contract_path)
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
                                      "remaining_findings", "artifacts", "checks", "review_budget")
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
