"""Synthetic CLI coverage for slicer telemetry and exclusive contract publication."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "ship_state.py"
spec = importlib.util.spec_from_file_location("slicer_test_workflow", SCRIPT.with_name("ship_workflow.py"))
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SlicerSetupTests(unittest.TestCase):
    def setUp(self):
        project = tempfile.TemporaryDirectory(dir=os.environ["SHIP_TEST_TMPDIR"])
        self.addCleanup(project.cleanup)
        self.project = Path(project.name).resolve()
        self.repo = self.project / "api"
        self.repo.mkdir()
        (self.repo / "source.txt").write_text("fixture")
        self.run = self.project / "plans/.ship-runs/ship-1"
        self.plan = self.project / "approved.md"
        self.plan.write_text("Approved plan\n")
        self.ok("init-run", "--run-dir", self.run, "--workflow", "ship", "--repo", "api",
                "--description", "Test a slicer attempt", "--plan-path", self.plan, "--session-id", "session")
        self.snapshot = self.run / "approved-plan.md"
        self.snapshot.write_bytes(self.plan.read_bytes())
        self.draft = {
            "schema_version": 2, "workflow": "ship", "run_id": self.run.name, "repos": ["api"],
            "repo_paths": {"api": str(self.repo)}, "read_excludes": {"api": ["private"]},
            "slicer_draft": True, "plan_path": str(self.plan), "approved_snapshot": str(self.snapshot),
            "plan_sha256": sha(self.plan),
            "criteria": [{"id": "AC", "kind": "static", "repos": ["api"], "mandatory": True,
                          "requirement": "Correct behavior", "source": "Plan", "verification": "Inspect source",
                          "environment": "fixture", "data_scope": "synthetic"}],
            "required_passes": {"api": ["logic"]},
        }
        for group in ("required_gates", "pre_review_checks", "preflight_checks"):
            self.draft[group] = {"api": [{"id": group, "kind": "static", "verification": "Inspect source"}]}
        self.draft_path = self.save("slicer-draft.json", self.draft)
        self.policy = json.dumps({key: self.draft[key] for key in ("repo_paths", "read_excludes")})
        self.slice = {"id": "s1", "title": "Implement behavior",
                      "contributions": [{"criterion_id": "AC", "part": "Prove behavior"}],
                      "write_scope": ["src/**"], "depends_on": [], "interfaces": [], "targeted_checks": [],
                      "resources": [], "parallel_allowed": False}
        self.proposal = {"repo": "api", "recommendation": "sliced", "reason": "Independent proof",
                         "slices": [self.slice], "cross_slice_criteria": [], "max_parallel": 1}
        self.result = self.save("proposal-input.json", self.proposal)
        self.start_path = self.run / "telemetry/slicer-proposal-first-start.json"
        self.end_path = self.run / "telemetry/slicer-proposal-first-end.json"
        self.contract = self.run / "contract.json"

    def save(self, name, data):
        path = self.run / name
        path.write_text(json.dumps(data))
        return path

    def call(self, *args):
        return self.command(SCRIPT, *args)

    def command(self, script, *args):
        return subprocess.run(["python3", "-I", "-B", str(script), *map(str, args)],
                              cwd=self.project, text=True, capture_output=True)

    def ok(self, *args):
        response = self.call(*args)
        self.assertEqual(response.returncode, 0, response.stderr)

    def assert_blocked(self, response, message):
        self.assertEqual(response.returncode, 1, response.stderr)
        self.assertIn(message, response.stderr)
        self.assertNotIn("Traceback", response.stderr)

    def boundary(self, event, *extra, identifier="first", repo="api", policy=None):
        return self.call("slicer-proposal", "--run-dir", self.run, "--read-policy",
                         self.policy if policy is None else policy, "--repo", repo, "--attempt-id", identifier,
                         "--event", event, "--session-id", "session", *extra)

    def start(self, identifier="first"):
        response = self.boundary("start", identifier=identifier)
        self.assertEqual(0, response.returncode, response.stderr)
        return json.loads(response.stdout)

    def end(self, identifier="first"):
        response = self.boundary("end", "--status", "passed", "--agent-id", "agent-1", "--tool-use-id",
                                 "call-1", "--result", self.result, identifier=identifier)
        self.assertEqual(0, response.returncode, response.stderr)
        return json.loads(response.stdout)

    def approval(self, identifier="first", *, sliced=True):
        end = self.run / f"telemetry/slicer-proposal-{identifier}-end.json"
        decision = {"decision": "sliced" if sliced else "unsliced", "phase_id": f"slicer-proposal-{identifier}",
                    "end_sha256": sha(end), "approved_by": "user",
                    "approved_at": datetime.now(timezone.utc).isoformat(), "user_authorization": "Approve this choice"}
        if sliced:
            schedule = {"schema_version": 2, "repo": "api", "run_id": self.run.name, "approved": True,
                        "approved_by": decision["approved_by"], "approved_at": decision["approved_at"],
                        "max_parallel": 1, "slices": [self.slice], "cross_slice_criteria": []}
            path = self.save("approved-schedule.json", schedule)
            decision["schedule"] = {"path": str(path), "sha256": sha(path)}
        self.decisions = {"api": decision}
        self.decision_path = self.save("decisions.json", self.decisions)

    def seal(self):
        return self.call("seal-slicer-contract", "--run-dir", self.run, "--read-policy", self.policy,
                         "--decisions", self.decision_path)

    def test_sliced_contract_seals_exact_inputs_and_setup_stays_separate(self):
        start = self.start()
        self.assertFalse(self.contract.exists())
        self.assert_blocked(self.call("validate-contract", "--contract", self.draft_path), "not a lifecycle contract")
        self.assert_blocked(self.call("validate-run", "--contract", self.draft_path, "--attempt-id", "draft"),
                            "not a lifecycle contract")
        copied = self.save("copy-of-draft.json", self.draft)
        self.assert_blocked(self.call("validate-run", "--contract", copied, "--attempt-id", "copy"),
                            "before setup validation")
        self.assert_blocked(self.call("validate-run", "--contract", self.contract, "--attempt-id", "premature"),
                            "before setup validation")
        self.assertFalse((self.run / "setup-seals").exists())
        self.assertEqual(start["inputs"], {"run_sha256": sha(self.run / "run.json"),
                                           "plan_sha256": sha(self.plan), "draft_sha256": sha(self.draft_path)})
        end = self.end()
        self.assertEqual(end["start_sha256"], sha(self.start_path))
        self.assertEqual(end["proposal"], self.proposal)
        self.assertEqual((end["agent_id"], end["tool_use_id"], end["session_id"]), ("agent-1", "call-1", "session"))
        self.result.unlink()
        self.approval()
        result = self.seal()
        self.assertEqual(result.returncode, 0, result.stderr)
        sealed = self.contract.read_bytes()
        value = json.loads(sealed)
        self.assertEqual(value["slicer_proposals"], self.decisions)
        self.assertEqual(value["slice_schedules"]["api"], self.decisions["api"]["schedule"])
        self.assertEqual(value["slicer_telemetry"], {"slicer-proposal-first": sha(self.end_path)})
        self.assertFalse((self.run / "setup-seals").exists())
        self.ok("validate-run", "--contract", self.contract, "--attempt-id", "final")
        self.ok("validate-slices", "--contract", self.contract, "--repo", "api",
                "--file", self.decisions["api"]["schedule"]["path"])
        with self.assertRaisesRegex(ValueError, "Non-setup telemetry exists"):
            workflow.setup_history(self.contract, workflow.contract(self.contract))
        self.assert_blocked(self.seal(), "contract.json")
        self.assertEqual(self.contract.read_bytes(), sealed)

    def test_failed_end_can_be_retried_and_an_interrupted_attempt_cannot_be_approved(self):
        self.start()
        self.assert_blocked(self.boundary("end", "--status", "passed", "--agent-id", "agent-1",
                                          "--result", self.run / "missing.json"), "No such file")
        self.assertFalse(self.end_path.exists())
        self.assert_blocked(self.boundary("start", identifier="second"), "open slicer attempt")
        ended = self.boundary("end", "--status", "interrupted", "--end-reason", "timeout")
        self.assertEqual(ended.returncode, 0, ended.stderr)
        self.approval(sliced=False)
        self.assert_blocked(self.seal(), "result or exact input hashes mismatch")
        self.start("second")
        self.end("second")
        self.approval("second", sliced=False)
        result = self.seal()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.contract.read_bytes())["slice_schedules"], {})
        self.ok("validate-run", "--contract", self.contract, "--attempt-id", "final")
        self.end_path.unlink()
        self.start_path.unlink()
        self.assert_blocked(self.call("validate-contract", "--contract", self.contract), "seal every recorded attempt")

    def test_missing_or_modified_inputs_block_and_leave_start_intact(self):
        self.start()
        start = self.start_path.read_bytes()
        for path in (self.plan, self.snapshot, self.draft_path, self.run / "run.json"):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.unlink()
                self.assert_blocked(self.boundary("end", "--status", "failed", "--end-reason", "unknown"),
                                    "No such file")
                path.write_bytes(original + b"\n")
                self.assert_blocked(self.boundary("end", "--status", "failed", "--end-reason", "unknown"),
                                    "mismatch" if path in (self.plan, self.snapshot) else "changed after dispatch")
                path.write_bytes(original)
                self.assertEqual(self.start_path.read_bytes(), start)
                self.assertFalse(self.end_path.exists())
        self.end()
        self.approval()
        self.draft_path.write_bytes(self.draft_path.read_bytes() + b"\n")
        self.assert_blocked(self.seal(), "changed after dispatch")
        self.assertFalse(self.contract.exists())

    def test_mismatched_run_repo_and_returned_result_are_refused(self):
        self.assert_blocked(self.boundary("start", repo="wrong"), "Unknown slicer repo")
        meta = self.run / "run.json"
        original = json.loads(meta.read_bytes())
        for changes in ({"repos": ["wrong"]}, {"run_id": "wrong"}, {"workflow": "fix"},
                        {"plan_path": str(self.snapshot)}):
            with self.subTest(changes=changes):
                self.save("run.json", {**original, **changes})
                self.assert_blocked(self.boundary("start"), "metadata/draft identity mismatch")
        self.save("run.json", original)
        self.start()
        self.save(self.result.name, {**self.proposal, "repo": "wrong"})
        self.assert_blocked(self.boundary("end", "--status", "passed", "--agent-id", "agent-1",
                                          "--result", self.result), "proposal repo mismatch")
        self.assertFalse(self.end_path.exists())

    def test_missing_duplicate_and_misnamed_boundaries_block_without_overwriting(self):
        self.assert_blocked(self.boundary("end", "--status", "failed", "--end-reason", "unknown"), "unmatched start")
        self.start()
        start = self.start_path.read_bytes()
        self.assert_blocked(self.boundary("start"), "open slicer attempt")
        self.assertEqual(self.start_path.read_bytes(), start)
        self.end()
        self.approval()
        end = self.end_path.read_bytes()
        self.assert_blocked(self.boundary("end", "--status", "failed", "--end-reason", "unknown"), "unmatched start")
        self.assertEqual(self.end_path.read_bytes(), end)
        for missing in (self.start_path, self.end_path):
            data = missing.read_bytes()
            missing.unlink()
            self.assert_blocked(self.seal(), "Incomplete slicer telemetry")
            missing.write_bytes(data)
        duplicate = self.end_path.with_name("slicer-proposal-copy-end.json")
        duplicate.write_bytes(end)
        self.assert_blocked(self.seal(), "Invalid slicer telemetry identity")
        self.assertFalse(self.contract.exists())

    def test_unapproved_changed_or_invalid_schedule_never_publishes(self):
        self.start()
        self.end()
        self.approval()
        decision = self.decisions["api"]
        schedule = Path(decision["schedule"]["path"])
        approved = json.loads(schedule.read_bytes())
        self.save(schedule.name, {**approved, "approved": False})
        self.assert_blocked(self.seal(), "schedule hash mismatch")
        decision["schedule"]["sha256"] = sha(schedule)
        self.save(self.decision_path.name, self.decisions)
        self.assert_blocked(self.seal(), "exact explicit slicer approval")
        self.save(schedule.name, {**approved, "slices": [{**self.slice, "write_scope": ["private/**"]}]})
        decision["schedule"]["sha256"] = sha(schedule)
        self.save(self.decision_path.name, self.decisions)
        self.assert_blocked(self.seal(), "read_excludes")
        self.assertFalse(self.contract.exists())

    def test_human_decision_and_selected_end_hash_are_required(self):
        self.start()
        self.end()
        self.approval(sliced=False)
        decision = self.decisions["api"]
        for changes, message in (({"user_authorization": ""}, "slicer user authorization"),
                                 ({"approved_by": "slicer"}, "approved by the user"),
                                 ({"end_sha256": "0" * 64}, "result or exact input hashes mismatch"),
                                 ({"approved_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()},
                                  "outside the approval window")):
            self.save(self.decision_path.name, {"api": {**decision, **changes}})
            self.assert_blocked(self.seal(), message)
            self.assertFalse(self.contract.exists())

    def test_bootstrap_exclusions_apply_before_following_draft_references(self):
        self.draft["approved_snapshot"] = str(self.run / "missing-snapshot.md")
        self.save(self.draft_path.name, self.draft)
        self.assert_blocked(self.boundary("start", policy="{}"), "bootstrap read policy")
        self.assertFalse(self.start_path.exists())
        private = self.repo / "private"
        private.mkdir()
        self.snapshot.unlink()
        self.snapshot.symlink_to(private / "missing.md")
        self.draft["approved_snapshot"] = str(self.snapshot)
        self.save(self.draft_path.name, self.draft)
        self.assert_blocked(self.boundary("start"), "read_excludes")
        self.assertFalse(self.start_path.exists())

    def test_simultaneous_seals_have_one_winner_and_later_tampering_is_rejected(self):
        self.start()
        self.end()
        self.approval()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.seal(), range(2)))
        self.assertEqual(sorted(result.returncode for result in results), [0, 1])
        self.ok("validate-run", "--contract", self.contract, "--attempt-id", "final")
        schedule = Path(self.decisions["api"]["schedule"]["path"])
        original = schedule.read_bytes()
        schedule.write_bytes(original + b"\n")
        self.assert_blocked(self.call("validate-contract", "--contract", self.contract), "schedule hash mismatch")
        schedule.write_bytes(original)
        event = json.loads(self.end_path.read_bytes())
        event["proposal"]["reason"] = "Changed after sealing"
        self.end_path.write_text(json.dumps(event))
        self.assert_blocked(self.call("validate-contract", "--contract", self.contract),
                            "seal every recorded attempt")

    def test_preflight_runner_waits_for_the_sealed_and_validated_contract(self):
        marker = self.project / "preflight-ran"
        self.draft["preflight_checks"]["api"][0].update(
            kind="empirical", argv=[sys.executable, "-c", "from pathlib import Path; import sys; "
                                    "Path(sys.argv[1]).write_text('executed')", str(marker)])
        self.save(self.draft_path.name, self.draft)
        self.start()
        scope = self.save("scope.json", ["source.txt"])
        out = self.run / "preflight-results"

        def preflight(contract):
            return self.command(SCRIPT.with_name("ship_run_checks.py"), "--contract", contract, "--repo", "api",
                                "--scope", scope, "--out-dir", out, "--group", "preflight_checks")

        self.assert_blocked(preflight(self.draft_path), "not a lifecycle contract")
        self.assertFalse(out.exists())
        self.end()
        self.approval(sliced=False)
        sealed = self.seal()
        self.assertEqual(sealed.returncode, 0, sealed.stderr)
        self.assert_blocked(preflight(self.contract), "with validate-run before starting work")
        self.assertFalse(marker.exists())
        self.assertFalse(out.exists())
        self.ok("validate-run", "--contract", self.contract, "--attempt-id", "final")
        result = preflight(self.contract)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists())

    def test_archived_plan_remains_usable_in_predecessor_audit_without_repair_charges(self):
        self.start()
        self.end()
        self.approval(sliced=False)
        sealed = self.seal()
        self.assertEqual(sealed.returncode, 0, sealed.stderr)
        self.ok("validate-run", "--contract", self.contract, "--attempt-id", "final")
        scope = self.save("scope.json", ["source.txt"])
        manifest = self.save("source.json", workflow.state.capture(self.repo, ["source.txt"], read_excludes=["private"]))
        baseline = self.run / "api/baselines"
        baseline.mkdir(parents=True)
        request = self.save("initialize.json", {"status": "initialized", "source_manifest": str(manifest),
                                                "scope_path": str(scope), "baseline_dir": str(baseline)})
        previous = self.run / "api/state-1.json"
        self.ok("transition", "--contract", self.contract, "--repo", "api", "--file", request, "--out", previous)
        self.plan.rename(self.project / "archived.md")
        audited = workflow.validate_history(self.contract, "api")
        self.assertEqual(audited["gate_fix_budget"]["cumulative_used_attempts"], 0)
        self.assertEqual(audited["review_fix_count"], 0)
        continuation = {"repo": "api", "work_item": "fixture", "source_contract_path": str(self.contract),
                        "source_contract_sha256": sha(self.contract), "source_state_path": str(previous),
                        "source_state_sha256": sha(previous), "source_history_sha256": audited["history_sha256"]}
        successor = {"repos": ["api"], "repo_paths": {"api": str(self.repo)}, "budget_continuity": [continuation]}
        diagnostics = []
        workflow.reserve_predecessors(successor, self.run.parent / "ship-2/contract.json", "a" * 64, successor,
                                      diagnostics=diagnostics, audit_only=True, direct_only=True)
        self.assertEqual(diagnostics, [])

    def test_init_task_refuses_unapproved_slicing_and_requires_successful_setup(self):
        self.start()
        self.end()
        self.approval(sliced=False)
        result = self.seal()
        self.assertEqual(result.returncode, 0, result.stderr)
        task = self.run / "tasks/s1.json"
        touched = self.run / "touched.json"
        scope = self.save("scope.json", ["source.txt"])
        script = SCRIPT.with_name("ship_artifacts.py")
        args = ("init-task", "--contract", self.contract, "--repo", "api", "--out", task, "--touched", touched,
                "--baseline", self.run / "baseline", "--evidence-dir", self.run / "evidence")
        denied = self.command(script, *args, "--scope-file", scope)
        self.assertIn("with validate-run before starting work", denied.stderr)
        self.assertFalse(task.exists())
        self.ok("validate-run", "--contract", self.contract, "--attempt-id", "final")
        unapproved = self.save("unapproved.json", {"slices": [self.slice]})
        denied = self.command(script, *args, "--slice", "s1", "--schedule", unapproved)
        self.assertNotEqual(denied.returncode, 0)
        self.assertFalse(touched.exists())
        self.assertFalse(task.exists())
        accepted = self.command(script, *args, "--scope-file", scope)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_legacy_setup_runs_remain_valid_and_manual_contract_cannot_omit_decisions(self):
        legacy = {key: item for key, item in self.draft.items() if key != "slicer_draft"}
        self.start()
        self.end()
        self.save("contract.json", legacy)
        self.assert_blocked(self.call("validate-contract", "--contract", self.contract), "per-repo slicer decisions")
        for path in (self.start_path, self.end_path, self.draft_path):
            path.unlink()
        self.ok("validate-run", "--contract", self.contract, "--attempt-id", "legacy")
        value = workflow.contract(self.contract)
        self.assertIn(str(self.contract), workflow.setup_history(self.contract, value))
        self.assert_blocked(self.boundary("start"), "contract.json")

    def test_legacy_run_with_unrelated_draft_filename_remains_valid(self):
        legacy = {key: item for key, item in self.draft.items() if key != "slicer_draft"}
        self.save("contract.json", legacy)
        self.save("slicer-draft.json", {"notes": "pre-existing preparation"})
        self.ok("validate-run", "--contract", self.contract, "--attempt-id", "legacy")
        self.ok("validate-contract", "--contract", self.contract)


if __name__ == "__main__":
    unittest.main()
