"""Mechanical artifact steps for implementer: CLI behaviour on a temporary git checkout."""

import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "ship_artifacts.py"
spec = importlib.util.spec_from_file_location("artifacts", SCRIPT)
artifacts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifacts)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("SHIP_TEST_TMPDIR"), prefix="artifacts-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src/a.txt").write_text("alpha\n")
        (self.repo / "other.txt").write_text("outside\n")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "-m", "init"], check=True)
        # The run directory is named after run_id: workflow.contract refuses any other name.
        self.run_dir = self.root / "run-1"
        self.evidence = self.run_dir / "repo/evidence"
        self.baseline = self.run_dir / "repo/baselines"
        self.touched = self.run_dir / "repo/touched.json"
        self.touched.parent.mkdir(parents=True, exist_ok=True)
        self.touched.write_text("[]\n")
        self.contract = self.run_dir / "contract.json"
        self.contract.write_text(json.dumps({
            "schema_version": 2, "workflow": "fix", "run_id": "run-1", "repos": ["repo"],
            "repo_paths": {"repo": str(self.repo)},
            "telemetry_policy": {"repair_phase_required": True, "end_reason_required": True, "run_local_seals": True},
            "author_rotation": "capsule",
            "criteria": [{"id": "c1", "kind": "empirical", "mandatory": True, "repos": ["repo"],
                          "requirement": "r", "source": "s", "verification": "v",
                          "environment": "scratch", "data_scope": "fixture"}],
            "pre_review_checks": {"repo": [{"id": "c1-check", "kind": "empirical",
                                            "argv": ["python3", "-c", "print('check')"], "verification": "v"}]},
            "required_gates": {"repo": [{"id": "gate.unit", "kind": "empirical",
                                         "argv": ["python3", "-c", "print('gate')"], "verification": "v"}]},
            "preflight_checks": {"repo": [{"id": "env.runtime", "kind": "static", "verification": "inspected"}]},
            "required_passes": {"repo": ["logic"]}, "prerequisites": [], "cross_repo_contracts": [],
            "acceptance": {"original_scenario": "alpha", "expected_behavior": "beta", "non_goals": "none",
                           "mode": "improvement", "criterion_ids": ["c1"]},
        }))
        self.scope = self.run_dir / "scope.json"
        self.scope.write_text(json.dumps(["src/a.txt"]))
        self.task = self.run_dir / "task.json"
        self.task.write_text(json.dumps({
            "contract": str(self.contract), "repo": "repo", "repo_path": str(self.repo),
            "baseline_dir": str(self.baseline), "touched": str(self.touched), "scope_file": str(self.scope),
            "write_scope": ["src"], "evidence_dir": str(self.evidence),
        }))

    def cli(self, *args, task=None):
        return subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "--task", str(task or self.task), *args],
                              capture_output=True, text=True)

    def init(self, out, *extra):
        return subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "init-task", "--contract", str(self.contract),
                               "--repo", "repo", "--touched", str(self.touched), "--baseline", str(self.baseline),
                               "--evidence-dir", str(self.evidence), "--out", str(out), *extra],
                              capture_output=True, text=True)

    def test_begin_edit_refuses_out_of_scope_path(self):
        result = self.cli("begin-edit", "other.txt")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Outside write_scope", result.stderr)
        self.assertEqual(json.loads(self.touched.read_text()), [])

    def test_begin_edit_baselines_backs_up_and_tracks_touched(self):
        self.assertEqual(self.cli("begin-edit", "src/a.txt").returncode, 0)
        self.assertEqual(json.loads(self.touched.read_text()), ["src/a.txt"])
        backup = self.evidence / "backups/src/1.a.txt.bak"
        self.assertEqual(backup.read_text(), "alpha\n")
        self.assertTrue((self.baseline / "src/a.txt").is_file())
        # A second edit gets a new backup name; earlier backups are never replaced.
        self.assertEqual(self.cli("begin-edit", "src/a.txt").returncode, 0)
        self.assertTrue((self.evidence / "backups/src/2.a.txt.bak").is_file())

    def test_check_file_flags_inflation_against_backup(self):
        self.cli("begin-edit", "src/a.txt")
        (self.repo / "src/a.txt").write_text("x" * 2_000_000)
        result = self.cli("check-file", "src/a.txt")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Suspicious growth", result.stderr)

    def test_run_writes_log_and_never_overwrites(self):
        result = self.cli("run", "r1", "--", "python3", "-c", "print('hello')")
        self.assertEqual(result.returncode, 0, result.stderr)
        log = (self.evidence / "logs/r1.log").read_text()
        self.assertIn("hello", log)
        self.assertIn("EXIT=0", log)
        again = self.cli("run", "r1", "--", "python3", "-c", "print('other')")
        self.assertEqual(again.returncode, 2)
        self.assertIn("already exists", again.stderr)

    def test_run_respects_initialized_exclusions(self):
        ignored = self.repo / "generated"
        ignored.mkdir()
        (ignored / "output.txt").write_text("output")
        (self.run_dir / "repo/state-1.json").write_text(json.dumps({
            "status": "initialized", "source_scope": ["src/a.txt"], "excludes": ["generated"]}))
        result = self.cli("run", "excluded", "--", "python3", "-c", "print('ok')")
        self.assertEqual(result.returncode, 0, result.stderr)
        for suffix in ("before", "after"):
            manifest = json.loads((self.evidence / f"runs/excluded-{suffix}.json").read_text())
            self.assertNotIn("generated/output.txt", manifest["files"])
            self.assertIn("src/a.txt", manifest["files"])

    def test_scope_check_ignores_unchanged_preexisting_wip(self):
        (self.repo / "other.txt").write_text("existing work\n")
        initial = self.run_dir / "repo/initial.json"
        initial.write_text(json.dumps(artifacts.state.capture(self.repo, ["src/a.txt"])))
        (self.run_dir / "repo/state-1.json").write_text(json.dumps({
            "status": "initialized", "initial_manifest": str(initial), "source_scope": [], "excludes": []}))
        result = self.cli("scope-check")
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.repo / "other.txt").write_text("changed again\n")
        result = self.cli("scope-check")
        self.assertEqual(result.returncode, 2)
        self.assertIn("other.txt", result.stdout)

    def test_capture_read_exclusion_skips_tracked_and_untracked(self):
        (self.repo / "secret").mkdir()
        (self.repo / "secret/private.txt").write_text("private")
        subprocess.run(["git", "-C", str(self.repo), "add", "secret/private.txt"], check=True)
        result = artifacts.state.capture(self.repo, ["src/a.txt"], read_excludes=["secret"])
        self.assertNotIn("secret/private.txt", result["files"])
        with self.assertRaisesRegex(ValueError, "read_excludes"):
            artifacts.state.capture(self.repo, ["secret/private.txt"], read_excludes=["secret"])

    def test_capture_does_not_open_excluded_directory(self):
        secret = self.repo / "secret"
        secret.mkdir()
        (secret / "untracked.txt").write_text("private")
        scan = os.scandir

        def guarded(path):
            self.assertFalse(Path(path) == secret or secret in Path(path).parents,
                             "capture traversed excluded directory")
            return scan(path)

        with patch.object(artifacts.state.os, "scandir", side_effect=guarded):
            manifest = artifacts.state.capture(self.repo, ["src/a.txt"], read_excludes=["secret"])
        self.assertNotIn("secret/untracked.txt", manifest["files"])
        self.assertIn("secret/untracked.txt", artifacts.state.capture(self.repo, ["src/a.txt"])["files"])

    def test_capture_exclusion_is_literal_and_respects_gitignore(self):
        (self.repo / "private[1]").mkdir()
        (self.repo / "private[1]" / "tracked.txt").write_text("private")
        (self.repo / "private1").mkdir()
        (self.repo / "private1" / "visible.txt").write_text("visible")
        (self.repo / ".gitignore").write_text("ignored/\n")
        (self.repo / "ignored").mkdir()
        (self.repo / "ignored" / "hidden.txt").write_text("ignored")
        subprocess.run(["git", "-C", str(self.repo), "add", ":(literal)private[1]/tracked.txt"], check=True)
        result = artifacts.state.capture(self.repo, ["src/a.txt"], read_excludes=["private[1]"])
        self.assertNotIn("private[1]/tracked.txt", result["files"])
        self.assertIn("private1/visible.txt", result["files"])
        self.assertNotIn("ignored/hidden.txt", result["files"])

    def test_exclusion_guard_stops_symlink_before_forbidden_target(self):
        secret = self.repo / "secret"
        secret.mkdir()
        (secret / "private.txt").write_text("private")
        (self.repo / "alias").symlink_to(secret, target_is_directory=True)
        original = Path.lstat

        def guarded(path, *args, **kwargs):
            self.assertFalse(path == secret or secret in path.parents, "probed forbidden target")
            return original(path, *args, **kwargs)

        with patch.object(Path, "lstat", guarded):
            with self.assertRaisesRegex(ValueError, "read_excludes"):
                artifacts.state.guard_excluded_path(self.repo / "alias/private.txt", self.repo, ["secret"])

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("sandbox-exec"), "macOS sandbox required")
    def test_capture_with_excluded_directory_denied_to_process_tree(self):
        secret = self.repo / "secret"
        secret.mkdir()
        (secret / "private.txt").write_text("private")
        subprocess.run(["git", "-C", str(self.repo), "add", "secret/private.txt"], check=True)
        (secret / "untracked.txt").write_text("private")
        profile = '(version 1)(allow default)(deny file-read* (subpath ' + json.dumps(str(secret.resolve())) + '))'
        code = ("import importlib.util,sys; s=importlib.util.spec_from_file_location('state',sys.argv[1]); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                "m.capture(sys.argv[2], ['src/a.txt'], read_excludes=['secret'] if sys.argv[3]=='yes' else [])")
        command = ["sandbox-exec", "-p", profile, sys.executable, "-I", "-B", "-c", code,
                   str(SCRIPTS / "ship_state.py"), str(self.repo)]
        good = subprocess.run([*command, "yes"], capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stderr)
        bad = subprocess.run([*command, "no"], capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0, "negative control unexpectedly read forbidden directory")

    def test_matrix_repair_preserves_unmodified_rows(self):
        task = artifacts.task_file(self.task)
        old_rows = [{"criterion_id": "c1", "evidence_paths": ["old"]},
                    {"criterion_id": "c2", "evidence_paths": ["keep"]}]
        base = self.run_dir / "base-matrix.json"
        base.write_text(json.dumps({"schema_version": 2, "run_id": "run-1", "repo": "repo", "criteria": old_rows}))
        records = self.evidence / "records"
        records.mkdir(parents=True)
        (records / "replacement.json").write_text("{}")
        rows = self.run_dir / "replacement.json"
        rows.write_text(json.dumps([{"criterion_id": "c1", "runs": ["replacement"],
                                     "replaces_evidence_paths": ["old"]}]))
        artifacts.matrix(task, str(rows), str(base))
        result = json.loads((self.evidence / "matrix/1.json").read_text())
        self.assertEqual(result["criteria"][1], old_rows[1])
        self.assertEqual(result["criteria"][0]["evidence_paths"], [str(records / "replacement.json")])

    def test_run_check_uses_contract_argv_only(self):
        result = self.cli("run", "r2", "--check", "c1-check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.evidence / "runs/r2.json").read_text())["command"],
                         shlex.join(["python3", "-c", "print('check')"]))
        self.assertEqual(self.cli("run", "r3", "--check", "missing").returncode, 2)

    def test_evidence_input_comes_from_contract_and_rejects_substitution(self):
        contract = json.loads(self.contract.read_text())
        good = {"name": "r2", "command": "python3 -c \"print('check')\"", "exit_code": 0,
                "before_manifest": "b", "after_manifest": "a", "log": "l"}
        body = artifacts.evidence_input(contract, "repo", good, [("c1", "passes")], "ignored", "ignored")
        self.assertEqual(body["environment"], "scratch")
        self.assertEqual(body["data_scope"], "fixture")
        self.assertEqual(body["kind"], "empirical")
        self.assertEqual(body["status"], "passed")
        swapped = dict(good, command="python3 -c 'pass'")
        with self.assertRaises(ValueError):
            artifacts.evidence_input(contract, "repo", swapped, [("c1-check", "passes")], "e", "d")

    def test_applied_takes_verification_kind_from_review(self):
        findings = self.run_dir / "round-1-findings.json"
        findings.write_text(json.dumps({"schema_version": 2, "run_id": "run-1", "repo": "repo", "round": 1,
                                        "findings": [{"id": "repo-1", "verification_kind": "empirical"}]}))
        task = json.loads(self.task.read_text())
        task.update(findings=str(findings), round_dir=str(self.run_dir), slice_id=None)
        self.task.write_text(json.dumps(task))
        result = self.run_dir / "result.json"
        result.write_text(json.dumps({"applied": [{"id": "repo-1", "verified": "fixed", "runs": []}],
                                      "failed": [], "skipped": []}))
        self.assertEqual(self.cli("applied", str(result)).returncode, 0)
        written = json.loads((self.run_dir / "round-1-applied.json").read_text())
        self.assertEqual(written["applied"][0]["verification_kind"], "empirical")
        bad = self.run_dir / "bad.json"
        bad.write_text(json.dumps({"applied": [{"id": "repo-9", "verified": "x", "runs": []}],
                                   "failed": [], "skipped": []}))
        self.assertEqual(self.cli("applied", str(bad)).returncode, 2)

    def test_capsule_is_sealed_with_the_observed_agent_and_detects_later_change(self):
        self.cli("begin-edit", "src/a.txt")
        draft = self.run_dir / "draft.json"
        fields = {field: [] for field in artifacts.CAPSULE_FIELDS}
        fields["task"] = "fix alpha"
        draft.write_text(json.dumps(fields))
        self.assertEqual(self.cli("capsule", str(draft), "--agent-id", "agent-x").returncode, 0)
        capsule = next((self.evidence / "capsules").glob("1.json"))
        self.assertEqual(json.loads(capsule.read_text())["written_by"], "agent-x")
        same = self.cli("since-capsule", str(capsule))
        self.assertIn("no change since capsule", same.stdout)
        (self.repo / "src/a.txt").write_text("beta\n")
        changed = self.cli("since-capsule", str(capsule))
        self.assertIn("changed since capsule: src/a.txt", changed.stdout)

    def test_capsule_needs_agent_id_and_refuses_bad_drafts(self):
        self.cli("begin-edit", "src/a.txt")
        fields = {field: [] for field in artifacts.CAPSULE_FIELDS}
        draft = self.run_dir / "good.json"
        draft.write_text(json.dumps(fields))
        self.assertEqual(self.cli("capsule", str(draft)).returncode, 2)
        short = self.run_dir / "short.json"
        short.write_text(json.dumps({"task": "x"}))
        result = self.cli("capsule", str(short), "--agent-id", "agent-x")
        self.assertEqual(result.returncode, 2)
        self.assertIn("lacks", result.stderr)
        fields["task"] = "y" * 20000
        big = self.run_dir / "big.json"
        big.write_text(json.dumps(fields))
        self.assertEqual(self.cli("capsule", str(big), "--agent-id", "agent-x").returncode, 2)

    def test_capsule_check_validates_without_writing_and_refuses_binding_fields(self):
        fields = {field: [] for field in artifacts.CAPSULE_FIELDS}
        draft = self.run_dir / "draft.json"
        draft.write_text(json.dumps(fields))
        result = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "capsule-check", str(draft)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.evidence / "capsules").exists())
        fields["written_by"] = "someone-else"
        draft.write_text(json.dumps(fields))
        result = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "capsule-check", str(draft)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("binding fields", result.stderr)

    def test_scope_check_reports_untracked_file_outside_touched(self):
        self.cli("begin-edit", "src/a.txt")
        (self.repo / "src/a.txt").write_text("changed\n")
        self.assertEqual(self.cli("scope-check").returncode, 0)
        (self.repo / "src/stray.txt").write_text("stray\n")
        result = self.cli("scope-check")
        self.assertEqual(result.returncode, 2)
        self.assertIn("NOT in touched: src/stray.txt", result.stdout)

    def test_init_task_repo_task_feeds_the_artifact_steps(self):
        out = self.run_dir / "tasks/t1.json"
        result = self.init(out, "--scope-file", str(self.scope))
        self.assertEqual(result.returncode, 0, result.stderr)
        task = json.loads(out.read_text())
        self.assertEqual(task["repo_path"], str(self.repo))
        self.assertEqual(task["write_scope"], ["**"])
        self.assertEqual(self.cli("begin-edit", "src/a.txt", task=out).returncode, 0)
        (self.repo / "src/a.txt").write_text("beta\n")
        self.assertEqual(self.cli("check-file", "src/a.txt", task=out).returncode, 0)
        self.assertEqual(self.cli("run", "c1-pre", "--check", "c1-check", task=out).returncode, 0)
        recorded = self.cli("record", "c1-pre", "--claim", "c1=check passes on the current tree", task=out)
        self.assertEqual(recorded.returncode, 0, recorded.stderr)

    def test_init_task_slice_takes_write_scope_from_the_schedule(self):
        schedule = self.run_dir / "slices-approved.json"
        schedule.write_text(json.dumps({"slices": [{"id": "s1", "write_scope": ["src"]}]}))
        out = self.run_dir / "tasks/s1.json"
        result = self.init(out, "--slice", "s1", "--schedule", str(schedule))
        self.assertEqual(result.returncode, 0, result.stderr)
        task = json.loads(out.read_text())
        self.assertEqual(task["write_scope"], ["src"])
        self.assertEqual(task["slice_id"], "s1")
        self.assertNotIn("scope_file", task)

    def test_init_task_refuses_bad_arguments_and_overwrites(self):
        inside = self.run_dir / "tasks/ok.json"
        self.assertEqual(self.init(inside, "--scope-file", str(self.scope)).returncode, 0)
        result = self.init(inside, "--scope-file", str(self.scope))
        self.assertEqual(result.returncode, 2, "write-once: an existing task file must not be replaced")
        outside = self.root / "outside.json"
        result = self.init(outside, "--scope-file", str(self.scope))
        self.assertEqual(result.returncode, 2)
        self.assertIn("inside the run directory", result.stderr)
        result = self.init(self.run_dir / "tasks/both.json", "--scope-file", str(self.scope), "--slice", "s1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("exactly one", result.stderr)
        result = self.init(self.run_dir / "tasks/none.json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("exactly one", result.stderr)

    def test_task_commands_need_a_task_file(self):
        result = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "scope-check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--task is required", result.stderr)


if __name__ == "__main__":
    unittest.main()
