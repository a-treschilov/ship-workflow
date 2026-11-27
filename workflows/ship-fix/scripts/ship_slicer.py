"""Record and verify read-only slicer dispatches before publishing a ship contract."""

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re


PREFIX = "slicer-proposal-"
DRAFT = "slicer-draft.json"
BINDINGS = {"slicer_proposals", "slicer_telemetry", "slice_schedules"}


def now():
    return datetime.now(timezone.utc).isoformat()


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def proposal_path(state, path, policies):
    guarded = state.guard_excluded_paths(path, policies, pinned=True)
    state.require(guarded == path, f"Slicer path must be canonical: {path}")
    return guarded


def read(state, path, policies):
    return state.capture_bytes(proposal_path(state, path, policies))


def local_file(state, path, root):
    state.require(isinstance(path, (str, Path)) and Path(path).is_absolute()
                  and ".." not in Path(path).parts and root in Path(path).parents,
                  "Slicer evidence needs an absolute path inside this run")
    return Path(path)


def inputs(state, root, draft, policies, *, historical=False):
    state.require(draft.get("workflow") == "ship" and draft.get("schema_version") == 2
                  and draft.get("run_id") == root.name, "Invalid ship slicer draft identity")
    meta_bytes = read(state, root / "run.json", policies)
    meta = json.loads(meta_bytes)
    state.require(isinstance(meta, dict) and all(meta.get(key) == draft.get(key) for key in
                  ("schema_version", "workflow", "run_id", "repos", "plan_path")),
                  "Slicer run metadata/draft identity mismatch")
    plan = Path(draft["plan_path"])
    state.require(plan.is_absolute() and ".." not in plan.parts, "Slicer plan path must be absolute")
    snapshot = local_file(state, draft.get("approved_snapshot"), root)
    state.require(plan != snapshot, "Slicer needs a separate approved-plan snapshot")
    planned = sha256(read(state, snapshot, policies))
    state.require(planned == draft.get("plan_sha256"), "Slicer approved-plan snapshot/hash mismatch")
    if not historical:
        state.require(sha256(read(state, plan, policies)) == planned, "Slicer approved plan hash mismatch")
    return {"run_sha256": sha256(meta_bytes), "plan_sha256": planned}


def load_draft(state, root, policies):
    content = read(state, root / DRAFT, policies)
    draft = json.loads(content)
    state.require(isinstance(draft, dict) and draft.get("slicer_draft") is True
                  and not BINDINGS & draft.keys(),
                  "Expected a slicer draft without schedules or approvals")
    return draft, sha256(content)


def phase_name(state, attempt_id):
    state.require(isinstance(attempt_id, str) and re.fullmatch(r"[A-Za-z0-9_-]+", attempt_id),
                  "Invalid slicer attempt ID")
    return PREFIX + attempt_id


def attempts(state, root, value, policies, *, before_contract=False):
    """Read only the reserved slicer namespace within phase telemetry."""
    directory = root / "telemetry"
    guarded = state.guard_excluded_paths(directory, policies, pinned=bool(policies))
    if not policies:
        guarded = guarded.resolve()
    try:
        with state.open_directory(guarded) as fd:
            names = os.listdir(fd)
    except FileNotFoundError:
        return {}

    pairs = {}
    for name in sorted(names):
        if not name.startswith(PREFIX):
            state.require(not before_contract or not name.endswith(("-start.json", "-end.json")),
                          "Slicer setup cannot follow ordinary phase telemetry")
            continue
        match = re.fullmatch(r"(slicer-proposal-[A-Za-z0-9_-]+)-(start|end)\.json", name)
        state.require(match, f"Invalid slicer telemetry filename: {name}")
        identifier, boundary = match.groups()
        content = read(state, directory / name, policies)
        event = json.loads(content)
        state.require(isinstance(event, dict) and event.get("schema_version") == 2
                      and event.get("phase") == "slicer" and event.get("phase_id") == identifier
                      and event.get("event") == "phase-" + boundary and event.get("workflow") == "ship"
                      and event.get("run_id") == value["run_id"] and event.get("repo") in value["repos"],
                      f"Invalid slicer telemetry identity: {name}")
        state.timestamp(event.get("at"))
        pairs.setdefault(identifier, {})[boundary] = (event, sha256(content))
    return pairs


def check_result(state, repo, result):
    state.require(isinstance(result, dict) and result.get("repo") == repo
                  and result.get("recommendation") in {"single", "sliced"}
                  and isinstance(result.get("slices"), list) and result["slices"],
                  "Invalid slicer proposal or proposal repo mismatch")
    state.text(result.get("reason"), "slicer proposal reason")


def verify_pairs(state, pairs):
    for identifier, pair in pairs.items():
        state.require(set(pair) == {"start", "end"}, f"Incomplete slicer telemetry: {identifier}")
        start, start_sha256 = pair["start"]
        end, _ = pair["end"]
        state.require(all(start.get(field) == end.get(field) for field in ("repo", "session_id", "inputs"))
                      and end.get("start_sha256") == start_sha256
                      and state.timestamp(start["at"]) <= state.timestamp(end["at"]),
                      f"Slicer start/end mismatch: {identifier}")
        if end.get("status") == "passed":
            state.text(end.get("agent_id"), "slicer agent ID")
            check_result(state, end["repo"], end.get("proposal"))
        else:
            state.require(end.get("status") in {"blocked", "interrupted", "failed"}
                          and end.get("end_reason") in state.END_REASONS and "proposal" not in end,
                          f"Invalid unsuccessful slicer boundary: {identifier}")


def schedules_for(state, decisions):
    state.require(isinstance(decisions, dict) and decisions, "Expected per-repo slicer decisions")
    schedules = {}
    fields = {"decision", "phase_id", "end_sha256", "approved_by", "approved_at", "user_authorization"}
    for repo, decision in decisions.items():
        state.require(isinstance(decision, dict), "Invalid slicer decision")
        if decision.get("decision") == "sliced":
            state.require(set(decision) == fields | {"schedule"}, "Sliced decision requires a schedule binding")
            schedules[repo] = decision["schedule"]
        else:
            state.require(decision.get("decision") == "unsliced" and set(decision) == fields,
                          "Expected an explicit sliced or unsliced decision")
    return schedules


def validate_binding(state, path, value, policies, *, historical=False):
    """Validate proposal bindings on every contract load, including validate-run."""
    root = path.parent
    pairs = attempts(state, root, value, policies)
    decisions = value.get("slicer_proposals")
    if not pairs and not {"slicer_proposals", "slicer_telemetry"} & value.keys():
        return
    state.require(value["workflow"] == "ship" and path.name == "contract.json", "Slicer requires a ship contract")
    verify_pairs(state, pairs)
    schedules = schedules_for(state, decisions)
    state.require(set(decisions) == {pair["start"][0]["repo"] for pair in pairs.values()},
                  "Slicer decisions must cover every dispatched repository")
    state.require(value.get("slicer_telemetry") == {phase: pair["end"][1] for phase, pair in pairs.items()},
                  "Slicer contract must seal every recorded attempt")
    state.require(schedules == value.get("slice_schedules", {}), "Schedule bindings must match slicer decisions")
    draft, draft_hash = load_draft(state, root, policies)
    basis = {key: item for key, item in value.items()
             if key not in BINDINGS and key != "_read_policy"}
    state.require(state.digest(basis) == state.digest({key: item for key, item in draft.items()
                                                     if key != "slicer_draft"}),
                  "Final contract differs from slicer draft")
    hashes = {**inputs(state, root, draft, policies, historical=historical), "draft_sha256": draft_hash}
    state.require(all(pair["start"][0].get("inputs") == hashes for pair in pairs.values()),
                  "Slicer draft, plan or run metadata changed after dispatch")
    for repo, decision in decisions.items():
        phase_id = decision["phase_id"]
        state.require(isinstance(phase_id, str) and phase_id in pairs,
                      f"Missing slicer proposal telemetry for {repo}")
        end, end_hash = pairs[phase_id]["end"]
        state.require(end["status"] == "passed" and end["repo"] == repo
                      and decision["end_sha256"] == end_hash and end["inputs"] == hashes,
                      f"Slicer result or exact input hashes mismatch for {repo}")
        state.require(decision["approved_by"] == "user", "Slicer decision must be approved by the user")
        state.text(decision["user_authorization"], "slicer user authorization")
        state.require(state.timestamp(end["at"]) <= state.timestamp(decision["approved_at"])
                      <= state.timestamp(now()), "Slicer decision timestamp is outside the approval window")
        if repo in schedules:
            schedule = schedules[repo]
            state.require(isinstance(schedule, dict) and set(schedule) == {"path", "sha256"},
                          "Invalid slicer schedule binding")
            schedule_path = local_file(state, schedule["path"], root)
            content = read(state, schedule_path, policies)
            state.require(sha256(content) == schedule["sha256"], "Approved slicer schedule hash mismatch")
            approved = json.loads(content)
            state.require(isinstance(approved, dict) and approved.get("approved") is True
                          and all(approved.get(field) == decision[field] for field in ("approved_by", "approved_at")),
                          "Schedule must carry the exact explicit slicer approval")
    return True


def require_setup(state, path, value, policies):
    """Keep an approved slicer contract out of work helpers until validate-run succeeds."""
    state.require(path.name != DRAFT and "slicer_draft" not in value, "Slicer draft is not a lifecycle contract")
    if "slicer_proposals" not in value:
        return
    contract_hash = sha256(read(state, path, policies))
    directory = proposal_path(state, path.parent / "telemetry", policies)
    with state.open_directory(directory) as fd:
        names = os.listdir(fd)
    for name in names:
        match = re.fullmatch(r"(setup-[A-Za-z0-9_.-]+)-end\.json", name)
        if not match:
            continue
        phase = match[1]
        identity = {"schema_version": 2, "run_id": value["run_id"], "phase_id": phase}
        end = json.loads(read(state, directory / name, policies))
        if (not isinstance(end, dict) or end.get("status") != "passed"
                or any(end.get(key) != expected for key, expected in identity.items())):
            continue
        start_bytes = read(state, directory / (phase + "-start.json"), policies)
        start = json.loads(start_bytes)
        shared = {**identity, "phase": "setup", "workflow": "ship", "repo": None}
        if (not isinstance(start, dict) or start.get("event") != "phase-start" or end.get("event") != "phase-end"
                or any(start.get(key) != expected or end.get(key) != expected for key, expected in shared.items())
                or sha256(start_bytes) != end.get("start_sha256")
                or state.timestamp(end["at"]) < state.timestamp(start["at"])):
            continue
        seal = json.loads(read(state, path.parent / "setup-seals" / (phase + ".json"), policies))
        if seal == {**identity, "contract_sha256": contract_hash}:
            return
    raise ValueError("Validate the sealed slicer contract with validate-run before starting work")


def record_boundary(workflow, args, root, contract, draft, hashes, policies):
    state = workflow.state
    identifier = phase_name(state, args.attempt_id)
    state.require(args.repo in draft["repos"], "Unknown slicer repo")
    directory = root / "telemetry"
    start_path = directory / (identifier + "-start.json")
    end_path = directory / (identifier + "-end.json")
    event = {"schema_version": 2, "workflow": "ship", "run_id": draft["run_id"], "repo": args.repo,
             "phase_id": identifier, "phase": "slicer", "event": "phase-" + args.event,
             "at": now(), "inputs": hashes}
    pairs = attempts(state, root, draft, policies, before_contract=True)
    for pair in pairs.values():
        state.require("start" in pair and pair["start"][0].get("inputs") == hashes,
                      "Slicer draft, plan or run metadata changed after dispatch, or start is missing")
    if args.event == "start":
        state.require(not any((args.status, args.end_reason, args.agent_id, args.tool_use_id, args.result)),
                      "Slicer start cannot contain a result")
        session = args.session_id
        state.require(not session or re.fullmatch(r"[A-Za-z0-9_-]+", session), "Invalid slicer session ID")
        event["session_id"] = session
        state.require(not state.guarded_exists(end_path, policies), "Slicer end already exists")
        state.require(not any("end" not in pair and pair["start"][0]["repo"] == args.repo for pair in pairs.values()),
                      "Repository already has an open slicer attempt")
        workflow.contract(contract, draft=draft, slicer_draft=True)
        destination = start_path
    else:
        pair = pairs.get(identifier, {})
        state.require("start" in pair and "end" not in pair, "Slicer end needs one unmatched start")
        start, start_hash = pair["start"]
        state.require(start["repo"] == args.repo and start["inputs"] == hashes,
                      "Slicer start repo or exact input hashes mismatch")
        state.require(args.session_id == start["session_id"], "Slicer session changed")
        event.update(session_id=start["session_id"], start_sha256=start_hash, status=args.status)
        if args.end_reason is not None:
            state.require(args.end_reason in workflow.END_REASONS, "Invalid slicer end reason")
            event["end_reason"] = args.end_reason
        for field in ("agent_id", "tool_use_id"):
            field_value = getattr(args, field)
            if field_value is not None:
                state.text(field_value, field)
                event[field] = field_value
        if args.status == "passed":
            result_path = local_file(state, args.result, root)
            event["proposal"] = json.loads(read(state, result_path, policies))
        else:
            state.require(args.result is None, "Only a passed slicer attempt can contain a proposal")
        verify_pairs(state, {identifier: {"start": (start, start_hash), "end": (event, None)}})
        destination = end_path
    state.write_once(proposal_path(state, destination, policies), event, pinned=True)
    return event


def execute(workflow, args):
    state = workflow.state
    policies = state.read_policy_input(json.loads(args.read_policy))
    root = Path(args.run_dir)
    state.require(root.is_absolute() and root.parent == Path.cwd().resolve() / "plans/.ship-runs",
                  "Slicer run must be directly inside the project's ship runs directory")
    state.relative_path(root.name)
    contract = root / "contract.json"
    with state.open_directory(proposal_path(state, root, policies)) as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        for name in ("contract.json", "setup-seals", "setup-replacement.json", "activity-started.json"):
            state.require(not state.guarded_exists(root / name, policies), f"Slicer setup already passed: {name}")
        draft, draft_hash = load_draft(state, root, policies)
        state.require(policies == state.read_policy_input(draft), "Slicer bootstrap read policy differs from draft")
        hashes = {**inputs(state, root, draft, policies), "draft_sha256": draft_hash}
        for repo in draft["repos"]:
            state.relative_path(repo)
            state.require(not state.guarded_exists(root / repo / "successor-reservation.json", policies),
                          "Slicer run has a successor reservation")
            state.require(not state.guarded_exists(root / repo / "state-1.json", policies),
                          "Slicer setup cannot follow repository lifecycle state")
        if args.command == "slicer-proposal":
            return record_boundary(workflow, args, root, contract, draft, hashes, policies)
        path = local_file(state, args.decisions, root)
        decisions = json.loads(read(state, path, policies))
        schedules = schedules_for(state, decisions)
        pairs = attempts(state, root, draft, policies, before_contract=True)
        state.require(pairs, "Slicer contract needs recorded dispatch telemetry")
        verify_pairs(state, pairs)
        value = {key: item for key, item in draft.items() if key != "slicer_draft"}
        value.update(slicer_proposals=decisions, slice_schedules=schedules,
                     slicer_telemetry={phase: pair["end"][1] for phase, pair in pairs.items()})
        checked = workflow.contract(contract, draft=value)
        workflow.validate_store_roots(checked)
        for repo, schedule in schedules.items():
            workflow.checked_slice_schedule(checked, repo, schedule["path"])
        workflow.publish_once(contract, workflow.json_bytes(value), checked)
        return {"contract_path": str(contract), "status": "sealed"}
