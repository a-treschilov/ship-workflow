#!/usr/bin/env python3
"""Opt-in execution of ONE declared argv check; not wired into /ship or /fix.

Use python3 -I -B ship_checks.py describe ... then run ... --expected-digest DIGEST.
The digest detects drift, NOT permission. Disclose argv/cwd/timeout and obtain normal
harness authorization before run: Python children do not receive separate harness
permission checks. No retries, lifecycle transitions, agents, or settings changes.

Logs default to suppression. Redaction is best-effort, never a secret guarantee;
argv and descriptive contract metadata must themselves be safe to persist. Captures
only bind scoped source, not DB/network effects or transient edit-and-restore.
Local process groups cannot guarantee cancellation inside Docker/remote services.

Eligible empirical check declarations add:
    "execution": {"cwd": ".", "timeout_seconds": 120}
Legacy command strings remain manual; the runner never converts them to a shell.
CLI selectors: --contract PATH --repo NAME --group GROUP --check ID --scope PATH
--attempt UNIQUE_ID, plus --environment/--data-scope when not declared by the check.
Timeout is 1..86400 seconds; output defaults to suppress, --output-policy redact
retains best-effort sanitized excerpts (at most 64 KiB input per stream).

Artifacts: checks/REPO/ATTEMPT/{execution-spec,before,after,stdout,stderr,
execution-result,evidence-input,evidence,finalization-result}.json under RUN_DIR.
Execution facts are immutable and hash-sealed by evidence; finalization-result
separately records evidence publication/telemetry failure. Incomplete attempts
are never reused. CLI exits: 0 certified check pass, 1 completed check failure,
2 pre-execution/CLI refusal, 3 execution/finalization not certifiable. These are
NOT child return codes. No output means neither success nor safe cleanup.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time

# Do not write shared __pycache__ when loaded by a session alongside active /ship.
sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("check_workflow", Path(__file__).with_name("ship_workflow.py"))
workflow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(workflow)
state = workflow.state

GROUPS = {"preflight_checks": "preflight", "pre_review_checks": "pre-review", "required_gates": "gates"}
LIMIT = 65536
GRACE = 1.0
NOTICE = "Digest is drift detection, not approval; child commands need disclosed harness authorization."


def now():
    return datetime.now(timezone.utc).isoformat()


def sanitize(raw, truncated=False):
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|$)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"[^\S\n\t]|[\x00-\x08\x0b-\x1f\x7f]", " ", text)
    # Discard an incomplete retained line rather than exposing a cut credential.
    if truncated:
        text = text.rsplit("\n", 1)[0] if "\n" in text else ""
    text = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]*@", r"\1[REDACTED]@", text)
    text = re.sub(r"(?i)\b(bearer\s+)\S+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)\b(password|passwd|pwd|token|api[_-]?key|secret)(\s*[=:]\s*)([^\s,;]+)",
                  r"\1\2[REDACTED]", text)
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*", "[REDACTED PRIVATE KEY]", text, flags=re.S)
    return text[:LIMIT]


def describe(contract_path, repo, group, check_id, scope_path, attempt,
             environment=None, data_scope=None, output_policy="suppress", excludes=()):
    value = workflow.contract(contract_path)
    state.require(repo in value["repos"] and group in GROUPS, "Unknown repo/check group")
    entries = [c for c in value[group][repo] if c["id"] == check_id]
    state.require(len(entries) == 1, "Unknown check ID")
    check = entries[0]
    argv = check.get("argv")
    state.require(check["kind"] == "empirical" and "command" not in check
                  and isinstance(argv, list) and argv
                  and all(isinstance(a, str) and a and "\0" not in a for a in argv),
                  "Runner requires a declared empirical argv check")
    execution = check.get("execution")
    state.require(isinstance(execution, dict) and set(execution) == {"cwd", "timeout_seconds"},
                  "Check requires explicit execution cwd and timeout_seconds")
    timeout = execution["timeout_seconds"]
    state.require(type(timeout) is int and 0 < timeout <= 86400, "Timeout must be an integer in 1..86400 seconds")
    relative = execution["cwd"]
    state.require(isinstance(relative, str), "Invalid cwd")
    if relative != ".":
        state.relative_path(relative)
    root = Path(value["repo_paths"][repo]).resolve()
    cwd = (root / relative).resolve(strict=True)
    state.require(cwd.is_dir() and cwd.is_relative_to(root), "cwd escapes repository")
    search_path = os.pathsep.join(str(cwd / p) for p in os.environ.get("PATH", os.defpath).split(os.pathsep))
    executable = shutil.which(argv[0], path=search_path) if "/" not in argv[0] else str(cwd / argv[0])
    state.require(executable is not None, "Executable unavailable")
    # Preserve the invocation path: resolving a venv's Python symlink changes its environment.
    executable = Path(os.path.abspath(executable))
    state.require(executable.is_file() and os.access(executable, os.X_OK), "Executable is not executable")
    state.require(output_policy in {"suppress", "redact"}, "Unknown output policy")
    state.require(isinstance(attempt, str) and re.fullmatch(r"[A-Za-z0-9_-]+", attempt), "Invalid attempt ID")
    run = Path(contract_path).resolve().parent
    # Evidence outputs must not become untracked source or require hidden exclusions.
    state.require(not any(run.is_relative_to(Path(p).resolve()) for p in value["repo_paths"].values()),
                  "Run artifacts must be outside assigned source repositories")
    output = run / "checks" / repo / attempt
    state.require(output.resolve().is_relative_to(run), "Output escapes run directory")
    scope = state.scope_paths(scope_path)
    excludes = sorted({state.relative_path(x) for x in excludes})
    for field, supplied in (("environment", environment), ("data_scope", data_scope)):
        state.require(not check.get(field) or supplied is None or supplied == check[field], "Check metadata mismatch")
    environment = check.get("environment") or environment
    data_scope = check.get("data_scope") or data_scope
    state.text(environment, "execution environment")
    state.text(data_scope, "execution data scope")
    spec = {
        "artifact_type": "check-execution-spec", "execution_schema_version": 1,
        "run_id": value["run_id"], "workflow": value["workflow"], "repo": repo,
        "contract_path": str(Path(contract_path).resolve()), "contract_sha256": workflow.sha(contract_path),
        "group": group, "check_id": check_id, "argv": argv,
        "resolved_argv": [str(executable), *argv[1:]], "executable_sha256": workflow.sha(executable),
        "command": workflow.check_command(check), "cwd": str(cwd), "timeout_seconds": timeout,
        "termination_grace_seconds": GRACE, "phase_id": f"check-{repo}-{attempt}",
        "output_dir": str(output), "output_policy": output_policy, "stream_limit_bytes": LIMIT,
        "scope_path": str(Path(scope_path).resolve()), "scope": scope, "excludes": excludes,
        "environment": environment, "data_scope": data_scope, "verification": check["verification"],
        "environment_policy": "inherited, not recorded; no overrides", "permission_notice": NOTICE,
        "cancellation_limit": "Local process group only; no Docker/remote/daemon cancellation guarantee",
    }
    return {**spec, "spec_digest": state.digest(spec)}


def group_alive(pid):
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # A group we cannot inspect is not confirmed gone.
        return True


def signal_group(pid, number):
    try:
        os.killpg(pid, number)
        return True
    except ProcessLookupError:
        return True
    except PermissionError:
        return False


def execute(spec):
    """Bounded POSIX process execution. Returned buffers are in-memory only."""
    state.require(os.name == "posix", "Process-group execution requires POSIX")
    result = {"outcome": "spawn-error", "child_returncode": None, "cleanup": "not-started",
              "interruption_signal": None, "started_at": now()}
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    counts = {"stdout": 0, "stderr": 0}
    caught = []
    old = {}
    proc = None
    selector = selectors.DefaultSelector()
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            old[sig] = signal.signal(sig, lambda number, frame: caught.append(number))
        proc = subprocess.Popen(spec["resolved_argv"], cwd=spec["cwd"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                                start_new_session=True)
        for name in buffers:
            stream = getattr(proc, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        deadline = time.monotonic() + spec["timeout_seconds"]
        term_at = None
        kill_at = None
        result.update(outcome="completed", cleanup="local-group-exited")
        while True:
            current = time.monotonic()
            rc = proc.poll()
            if term_at is None:
                reason = "interrupted" if caught else "timeout" if current >= deadline else None
                if reason is None and rc is not None and not selector.get_map() and group_alive(proc.pid):
                    reason = "cleanup-uncertain"
                if reason:
                    result["outcome"] = reason
                    result["interruption_signal"] = caught[0] if caught else None
                    term_at = current
                    if not signal_group(proc.pid, signal.SIGTERM):
                        result["cleanup"] = "uncertain"
            if term_at is not None and kill_at is None and current >= term_at + GRACE:
                if not signal_group(proc.pid, signal.SIGKILL):
                    result["cleanup"] = "uncertain"
                kill_at = current
            for key, _ in selector.select(0.05):
                chunk = os.read(key.fileobj.fileno(), 16384)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                name = key.data
                counts[name] += len(chunk)
                if spec["output_policy"] != "suppress":
                    buffers[name].extend(chunk[:max(0, LIMIT - len(buffers[name]))])
            if rc is not None and not selector.get_map() and not group_alive(proc.pid):
                break
            if kill_at is not None and current >= kill_at + GRACE:
                result["cleanup"] = "uncertain" if group_alive(proc.pid) or rc is None or selector.get_map() else "local-group-exited"
                break
        try:
            result["child_returncode"] = proc.wait(timeout=GRACE)
        except subprocess.TimeoutExpired:
            result["cleanup"] = "uncertain"
        if result["cleanup"] == "uncertain" and result["outcome"] == "completed":
            result["outcome"] = "cleanup-uncertain"
    except OSError as error:
        result.update(outcome="spawn-error" if proc is None else "execution-error", error_type=type(error).__name__)
    finally:
        if proc is not None:
            # Also clean up on unexpected collection errors; never leave our child running.
            if proc.poll() is None or group_alive(proc.pid):
                if not signal_group(proc.pid, signal.SIGKILL):
                    result["cleanup"] = "uncertain"
                try:
                    proc.wait(timeout=GRACE)
                except subprocess.TimeoutExpired:
                    result["cleanup"] = "uncertain"
            result["child_returncode"] = proc.poll()
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        selector.close()
        for sig, previous in old.items():
            signal.signal(sig, previous)
    result["ended_at"] = now()
    streams = {}
    for name, raw in buffers.items():
        truncated = counts[name] > len(raw)
        streams[name] = {"bytes_seen": counts[name], "truncated": truncated,
                         "suppressed": spec["output_policy"] == "suppress",
                         "text": sanitize(bytes(raw), truncated) if raw else ""}
    return result, streams


def run(spec, expected_digest):
    """Execute a freshly resolved specification; digest is NOT authorization."""
    state.require(spec["spec_digest"] == expected_digest, "Execution specification changed; describe again")
    # Check again immediately before claiming any output or launching the child.
    fresh = describe(spec["contract_path"], spec["repo"], spec["group"], spec["check_id"],
                     spec["scope_path"], Path(spec["output_dir"]).name, spec["environment"],
                     spec["data_scope"], spec["output_policy"], spec["excludes"])
    state.require(fresh == spec, "Execution specification changed; describe again")
    output = Path(spec["output_dir"])
    state.require(output.resolve() == output, "Symlinked output path")
    output.parent.mkdir(parents=True, exist_ok=True)
    state.require(output.resolve() == output, "Symlinked output path")
    output.mkdir()  # Exclusive reservation BEFORE any child execution.
    state.write_once(output / "execution-spec.json", spec)
    contract_path, repo = spec["contract_path"], spec["repo"]
    value = workflow.contract(contract_path)
    phase = {"run_id": value["run_id"], "workflow": value["workflow"], "repo": repo,
             "phase_id": spec["phase_id"], "phase": GROUPS[spec["group"]]}

    def telemetry(event, **kwargs):
        request = output / (event + "-input.json")
        state.write_once(request, {**phase, "event": event, "at": now(), **kwargs})
        suffix = "start" if event == "phase-start" else "end"
        workflow.phase(contract_path, str(request), str(Path(contract_path).parent / "telemetry" / f"{spec['phase_id']}-{suffix}.json"))

    telemetry("phase-start")
    result = {"artifact_type": "check-execution", "execution_schema_version": 1,
              "run_id": value["run_id"], "repo": repo, "spec_digest": spec["spec_digest"],
              "outcome": "capture-error", "child_returncode": None}
    code, evidence_path = 3, None
    phase_status, end_reason = "blocked", "unknown"
    try:
        before = state.capture(value["repo_paths"][repo], spec["scope"], spec["excludes"])
        state.write_once(output / "before.json", before)
        actual, streams = execute(spec)
        result.update(actual)
        result["process_outcome"] = actual["outcome"]
        for name, stream in streams.items():
            state.write_once(output / f"{name}.json", stream)
        after = state.capture(value["repo_paths"][repo], spec["scope"], spec["excludes"])
        state.write_once(output / "after.json", after)
        result["source_unchanged"] = before == after
        if before != after:
            result["outcome"] = "source-changed"
        state.write_once(output / "execution-result.json", result)
        if result["outcome"] == "completed" and result["cleanup"] == "local-group-exited":
            state.require(workflow.sha(contract_path) == spec["contract_sha256"], "Contract changed during execution")
            passed = result["child_returncode"] == 0
            status = "passed" if passed else "failed"
            evidence = {
                "criterion_id": spec["check_id"], "claims": [{"id": spec["check_id"], "assertion": spec["verification"]}],
                "kind": "empirical", "producer": "orchestrator", "status": status,
                "command": spec["command"], "exit_code": result["child_returncode"],
                "environment": spec["environment"], "data_scope": spec["data_scope"],
                "before_manifest": str(output / "before.json"), "after_manifest": str(output / "after.json"),
                "artifact_paths": [str(output / f"{name}.json") for name in ("stdout", "stderr")]
                                  + [str(output / name) for name in ("execution-spec.json", "execution-result.json")],
            }
            if not passed:
                evidence["reason"] = "Declared check completed with a nonzero child return code"
            state.write_once(output / "evidence-input.json", evidence)
            evidence_path = str(output / "evidence.json")
            workflow.record_evidence(contract_path, repo, str(output / "evidence-input.json"), evidence_path)
            workflow.proof(value, repo, evidence_path, after, spec["check_id"], "orchestrator", status)
            code = 0 if passed else 1
            phase_status, end_reason = status, "done"
        elif result["outcome"] in {"timeout", "interrupted"}:
            phase_status = "interrupted"
            end_reason = "timeout" if result["outcome"] == "timeout" else "user-stop"
    except Exception as error:
        # No exception text: subprocess/path/contract errors may contain secrets.
        result["finalization_error_type"] = type(error).__name__
        code = 3
    if not (output / "execution-result.json").exists():
        result["outcome"] = "capture-error" if "process_outcome" in result else result["outcome"]
        state.write_once(output / "execution-result.json", result)
    result = {**result, "evidence_path": evidence_path, "runner_exit_code": code}
    try:
        telemetry("phase-end", status=phase_status, end_reason=end_reason)
    except Exception as error:
        result.update(runner_exit_code=3, finalization_error_type=type(error).__name__)
        code = 3
    state.write_once(output / "finalization-result.json", result)
    return code, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("describe", "run"))
    for field in ("contract", "repo", "group", "check", "scope", "attempt", "environment", "data-scope"):
        parser.add_argument("--" + field, required=field not in {"environment", "data-scope"})
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--output-policy", choices=("suppress", "redact"), default="suppress")
    parser.add_argument("--expected-digest")
    args = parser.parse_args()
    try:
        spec = describe(args.contract, args.repo, args.group, args.check, args.scope, args.attempt,
                        args.environment, args.data_scope, args.output_policy, args.exclude)
        if args.action == "describe":
            print(json.dumps(spec, indent=2))
            return 0
        state.require(args.expected_digest, "run requires --expected-digest from describe")
        code, result = run(spec, args.expected_digest)
        print(json.dumps({"outcome": result["outcome"], "runner_exit_code": code,
                          "output_dir": spec["output_dir"]}))
        return code
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "status": "not-passed"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
