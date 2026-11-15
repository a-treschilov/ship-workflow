#!/usr/bin/env python3
"""Run contract-declared checks as the orchestrator and record them as bound evidence.

Usage:
  python3 -I -B ship_run_checks.py --contract C --repo R --scope SCOPE_JSON --out-dir DIR \
      --group {preflight_checks,pre_review_checks,required_gates} [--ids a,b] [--stop-on-fail]
  python3 -I -B ship_run_checks.py gates --contract C --repo R --scope SCOPE_JSON \
      --out-dir DIR --phase-id ID [--ids a,b] [--stop-on-fail]

`gates` runs the required_gates group and wraps it with phase-start/phase-end telemetry.

For each check that has an `argv` in the contract group this script:
  1. captures the source manifest before (helper `capture`, same scope the transition will use),
  2. runs the argv exactly as declared (no shell re-expansion), saving stdout/stderr and the real exit code,
  3. captures the manifest after,
  4. writes the evidence input (producer=orchestrator, claim = check id, command = shlex.join(argv)),
  5. calls the helper `record-evidence`, preserving failed results as `status: failed` with a `reason`.

It never decides success semantics: the exit code of the declared command is the result. Probes that guard shared
resources (for example a preflight check that exits non-zero when a scratch DB is busy) therefore bind real
immediately-before-run observations to the run. Existing outputs are never overwritten (write-once).
"""
import argparse
import importlib.util
import json
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HELPER = Path(__file__).with_name("ship_state.py")
DEFAULT_SCOPE = "Inputs used by the declared command."


def run(argv, **kwargs):
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


def capture(repo_path, scope, out, excludes=()):
    argv = [sys.executable, "-I", "-B", str(HELPER), "capture", "--repo", repo_path, "--scope", scope, "--out", str(out)]
    for path in excludes:
        argv.extend(["--exclude", path])
    done = run(argv)
    if done.returncode != 0:
        raise SystemExit("capture failed: " + (done.stdout + done.stderr).strip())


def workflow_module():
    spec = importlib.util.spec_from_file_location("run_checks_workflow", HELPER.with_name("ship_workflow.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def checks_module():
    spec = importlib.util.spec_from_file_location("run_checks_checks", HELPER.with_name("ship_checks.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_group(contract_path, contract, repo, scope, out_dir, group, wanted, stop_on_fail):
    repo_path = contract["repo_paths"][repo]
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    workflow = workflow_module()
    sanitize = checks_module().sanitize
    dependencies, exclusions = workflow.capture_policy(contract_path, repo)
    effective_scope = out_dir / "capture-scope.json"
    if effective_scope.exists():
        raise SystemExit("capture scope already exists; use a new --out-dir")
    effective_scope.write_text(json.dumps(sorted(set(json.loads(Path(scope).read_text())) | set(dependencies))))
    checks = [c for c in contract[group][repo] if "argv" in c and (not wanted or c["id"] in wanted)]
    if wanted and {c["id"] for c in checks} != wanted:
        raise SystemExit("Unknown or non-argv check id requested")
    results = {}
    for check in checks:
        cid = check["id"]
        marker = out_dir / f"{cid}.exit"
        if marker.exists():
            raise SystemExit(f"{cid}: output already exists in {out_dir}; use a new --out-dir (write-once)")
        before, after = out_dir / f"{cid}-before.json", out_dir / f"{cid}-after.json"
        log = out_dir / f"{cid}.log"
        capture(repo_path, str(effective_scope), before, exclusions)
        done = run(check["argv"], cwd=repo_path)
        log.write_text(sanitize((done.stdout + "\n--- stderr ---\n" + done.stderr).encode()))
        marker.write_text(f"{done.returncode}\n")
        capture(repo_path, str(effective_scope), after, exclusions)
        status = "passed" if done.returncode == 0 else "failed"
        body = {
            "kind": check["kind"], "status": status, "producer": "orchestrator",
            "command": shlex.join(check["argv"]), "exit_code": done.returncode,
            "environment": check.get("environment") or f"Declared command run from {repo_path}.",
            "data_scope": check.get("data_scope") or DEFAULT_SCOPE,
            "claims": [{"id": cid, "assertion": f"{check.get('verification', cid)} Actual exit {done.returncode}."}],
            "before_manifest": str(before), "after_manifest": str(after),
            "artifact_paths": [str(log), str(marker)], "criterion_id": cid,
        }
        if status == "failed":
            body["reason"] = f"Declared command exited {done.returncode}; see {log.name}."
        inp = out_dir / f"{cid}-input.json"
        inp.write_text(json.dumps(body, indent=1, ensure_ascii=False))
        rec = run([sys.executable, "-I", "-B", str(HELPER), "record-evidence", "--contract", contract_path,
                   "--repo", repo, "--file", str(inp), "--out", str(out_dir / f"{cid}-record.json")])
        note = (rec.stdout + rec.stderr).strip().splitlines()[-1:] or [""]
        results[cid] = (done.returncode, note[0])
        print(f"{cid}: exit={done.returncode} {note[0]}", flush=True)
        if rec.returncode != 0:
            raise SystemExit(f"{cid}: record-evidence refused: {note[0]}")
        if status == "failed" and stop_on_fail:
            break
    return results


def gates(contract_path, repo, scope, out_dir, phase_id, ids="", stop_on_fail=False,
          session_id=None, at=None):
    contract = json.loads(Path(contract_path).read_text())
    if repo not in contract["repo_paths"]:
        raise SystemExit("Unknown repo")
    workflow = workflow_module()
    run_dir = Path(contract_path).resolve().parent
    start_path = run_dir / "telemetry" / f"{phase_id}-start.json"
    end_path = run_dir / "telemetry" / f"{phase_id}-end.json"
    workflow.phase_from_flags(contract_path, str(start_path), phase_id=phase_id, phase="gates",
                              event="phase-start", repo=repo, session_id=session_id, at=at)
    wanted = {i for i in ids.split(",") if i}
    try:
        results = run_group(contract_path, contract, repo, scope, out_dir, "required_gates", wanted, stop_on_fail)
    except (Exception, SystemExit):
        workflow.phase_from_flags(contract_path, str(end_path), phase_id=phase_id, phase="gates",
                                  event="phase-end", repo=repo, status="blocked",
                                  end_reason="unknown", session_id=session_id)
        raise
    passed = all(code == 0 for code, _ in results.values())
    workflow.phase_from_flags(contract_path, str(end_path), phase_id=phase_id, phase="gates",
                              event="phase-end", repo=repo,
                              status="passed" if passed else "failed",
                              end_reason="done", session_id=session_id)
    print(f"gates: {sum(1 for code, _ in results.values() if code == 0)}/{len(results)} passed", flush=True)
    return results, "passed" if passed else "failed"


def gates_all_repos(contract_path, repos, scope_dir, out_dir, phase_id_template="{repo}-gates-1",
                    ids="", stop_on_fail=False, session_id=None, at=None):
    contract = json.loads(Path(contract_path).read_text())
    repo_list = (list(contract["repos"]) if repos is None
                 else [r.strip() for r in repos.split(",") if r.strip()])
    unknown = [r for r in repo_list if r not in contract.get("repo_paths", {})]
    if unknown:
        raise SystemExit("Unknown repos: " + ",".join(unknown))
    if "{repo}" not in phase_id_template:
        raise SystemExit("--phase-id-template must contain {repo}")
    scope_dir = Path(scope_dir)
    results = {}
    for repo in repo_list:
        scope = scope_dir / f"{repo}-scope.json"
        if not scope.is_file():
            raise SystemExit(f"Missing scope for {repo}: {scope}")
        phase_id = phase_id_template.format(repo=repo)
        repo_out = Path(out_dir) / repo
        try:
            _, status = gates(contract_path, repo, str(scope), str(repo_out), phase_id,
                              ids, stop_on_fail, session_id, at)
        except (Exception, SystemExit):
            if stop_on_fail:
                raise
            status = "blocked"
        results[repo] = status
        print(f"gates {repo}: {status}", flush=True)
        if status != "passed" and stop_on_fail:
            break
    passed = sum(1 for value in results.values() if value == "passed")
    overall = "passed" if passed == len(results) else "failed"
    print(f"gates: {passed}/{len(results)} repos passed", flush=True)
    return results, overall


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("checks", "gates"), default="checks")
    parser.add_argument("--contract", required=True)
    parser.add_argument("--repo")
    parser.add_argument("--repos", help="Comma-separated repo names for a batched gates run")
    parser.add_argument("--all-repos", action="store_true")
    parser.add_argument("--scope", help="JSON array of source paths (same as the transition scope_path)")
    parser.add_argument("--scope-dir", help="Directory holding <repo>-scope.json files (default <run>/inputs)")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--group", choices=["preflight_checks", "pre_review_checks", "required_gates"])
    parser.add_argument("--ids", default="")
    parser.add_argument("--stop-on-fail", action="store_true")
    parser.add_argument("--phase-id")
    parser.add_argument("--phase-id-template", default="{repo}-gates-1")
    parser.add_argument("--session-id")
    parser.add_argument("--at")
    args = parser.parse_args()
    if args.command == "gates":
        if args.group and args.group != "required_gates":
            raise SystemExit("gates runs required_gates only")
        if args.repo:
            if not args.scope or not args.phase_id:
                raise SystemExit("gates --repo requires --scope and --phase-id")
            _, status = gates(args.contract, args.repo, args.scope, args.out_dir, args.phase_id,
                              args.ids, args.stop_on_fail, args.session_id, args.at)
            sys.exit(0 if status == "passed" else 1)
        if args.all_repos or args.repos:
            run_dir = Path(args.contract).resolve().parent
            scope_dir = args.scope_dir or str(run_dir / "inputs")
            _, status = gates_all_repos(args.contract, args.repos, scope_dir, args.out_dir,
                                        args.phase_id_template, args.ids, args.stop_on_fail,
                                        args.session_id, args.at)
            sys.exit(0 if status == "passed" else 1)
        raise SystemExit("gates requires --repo or --all-repos/--repos")
    if not args.group:
        raise SystemExit("checks requires --group")
    contract = json.loads(Path(args.contract).read_text())
    if not args.repo or not args.scope:
        raise SystemExit("checks requires --repo and --scope")
    wanted = {i for i in args.ids.split(",") if i}
    results = run_group(args.contract, contract, args.repo, args.scope, args.out_dir,
                        args.group, wanted, args.stop_on_fail)
    sys.exit(0 if all(code == 0 for code, _ in results.values()) else 1)


if __name__ == "__main__":
    main()
