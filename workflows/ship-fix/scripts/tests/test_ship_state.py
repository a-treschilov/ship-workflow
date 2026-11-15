"""Isolated workflow tests; set SHIP_TEST_TMPDIR to the session scratchpad."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "ship_state.py"
spec = importlib.util.spec_from_file_location("ship_state", SCRIPT)
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


def finding(identifier="api-1", status="new"):
    return {"id": identifier, "file": "source.txt", "line": 1, "severity": "Warning",
            "status": status, "category": "Logic", "description": "Incorrect branch",
            "recommendation": "Fix the branch", "failure_scenario": "Null returns the wrong value",
            "verification_expectation": "Test null and non-null values", "criterion_ids": []}


class WorkflowStateTests(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get("SHIP_TEST_TMPDIR")
        if not scratch:
            self.fail("Set SHIP_TEST_TMPDIR to the session scratchpad; do not use system temp")
        self.temp = tempfile.TemporaryDirectory(dir=scratch, prefix="ship-state-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.base = self.root / "baselines"
        self.source = self.repo / "source.txt"
        self.source.write_text("original\n")
        self.git("init", "-q")
        self.git("add", ".")
        # Local fixture index is enough; avoid commits and any user/global hooks.
        self.manifest = state.capture(self.repo, ["source.txt"])

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def save(self, name, value):
        path = self.root / name
        state.write_once(path, value)
        return path

    def review(self, entries=None, round_number=1):
        return {"repo": "api", "round": round_number, "source_fingerprint": self.manifest["fingerprint"],
                "coverage": {"read": ["source.txt"], "skipped": []},
                "findings": [finding()] if entries is None else entries}

    def test_same_file_wip_delta_excludes_preexisting_edit(self):
        self.source.write_text("existing WIP\nunchanged\n")
        state.baseline(self.repo, self.base, "source.txt")
        self.source.write_text("existing WIP\nnew implementation\n")
        changes = state.delta(self.repo, self.base, ["source.txt"])["changes"]
        self.assertEqual(1, len(changes))
        self.assertNotIn("+existing WIP", changes[0]["diff"])
        self.assertIn("+new implementation", changes[0]["diff"])
        state.baseline(self.repo, self.base, "source.txt")
        self.assertEqual("existing WIP\nunchanged\n", (self.base / "source.txt").read_text())

    def test_new_deleted_and_renamed_paths(self):
        for path in ("source.txt", "renamed.txt", "new.txt"):
            state.baseline(self.repo, self.base, path)
        self.source.rename(self.repo / "renamed.txt")
        (self.repo / "new.txt").write_text("new\n")
        changes = {entry["path"]: entry for entry in
                   state.delta(self.repo, self.base, ["source.txt", "renamed.txt", "new.txt"])["changes"]}
        self.assertEqual("missing", changes["source.txt"]["after"]["kind"])
        self.assertEqual("missing", changes["renamed.txt"]["before"]["kind"])
        self.assertIn("+new", changes["new.txt"]["diff"])

    def test_created_then_removed_is_empty_delta(self):
        state.baseline(self.repo, self.base, "new.txt")
        new = self.repo / "new.txt"
        new.write_text("transient")
        new.unlink()
        self.assertEqual([], state.delta(self.repo, self.base, ["new.txt"])["changes"])

    def test_untracked_source_mutation_changes_fingerprint(self):
        new = self.repo / "untracked.txt"
        new.write_text("one")
        first = state.capture(self.repo, ["source.txt"])
        new.write_text("two")
        self.assertNotEqual(first["fingerprint"], state.capture(self.repo, ["source.txt"])["fingerprint"])

    def test_project_ignored_dependencies_preserves_source_and_rejects_drift(self):
        (self.repo / ".gitignore").write_text("vendor/\n")
        (self.repo / "vendor").mkdir()
        dependency = self.repo / "vendor" / "shared.js"
        dependency.write_text("original")
        initial = self.save("initial.json", state.capture(self.repo, ["vendor/shared.js"]))
        scope = self.save("dependencies.json", ["vendor/shared.js"])
        projected = state.project_ignored_dependencies(initial, scope)
        self.assertNotIn("vendor/shared.js", projected["files"])
        self.assertEqual(state.capture(self.repo, [])['fingerprint'], projected['fingerprint'])
        original_file_state = state.file_state
        calls = []

        def changing_file_state(path):
            result = original_file_state(path)
            calls.append(path)
            if len(calls) == 1:
                dependency.write_text("changed mid-scan")
            return result

        with patch.object(state, "file_state", side_effect=changing_file_state):
            with self.assertRaisesRegex(ValueError, "changed during projection"):
                state.project_ignored_dependencies(initial, scope)
        dependency.write_text("changed")
        with self.assertRaisesRegex(ValueError, "Dependency changed"):
            state.project_ignored_dependencies(initial, scope)
        tracked = self.save("tracked.json", ["source.txt"])
        with self.assertRaisesRegex(ValueError, "tracked source"):
            state.project_ignored_dependencies(initial, tracked)

    def test_tracked_deletion_changes_fingerprint(self):
        self.source.unlink()
        final = state.capture(self.repo, ["source.txt"])
        self.assertEqual("missing", final["files"]["source.txt"]["kind"])
        with self.assertRaises(ValueError):
            state.match(self.manifest, final)

    def test_evidence_output_does_not_invalidate_source(self):
        directory = self.repo / ".claude"
        directory.mkdir()
        (directory / "review-findings.json").write_text("{}")
        state.write_once(self.root / "evidence.json", {"status": "passed"})
        self.assertEqual(self.manifest, state.capture(self.repo, ["source.txt"]))

    def test_exclusions_cannot_hide_tracked_or_scoped_source(self):
        self.source.write_text("changed")
        self.assertNotEqual(self.manifest["fingerprint"],
                            state.capture(self.repo, ["source.txt"], ["source.txt"])["fingerprint"])
        (self.repo / ".gitignore").write_text("ignored.txt\n")
        (self.repo / "ignored.txt").write_text("ignored source")
        self.assertIn("ignored.txt", state.capture(self.repo, ["ignored.txt"], ["ignored.txt"])["files"])

    def test_file_mode_changes_invalidate_state(self):
        self.source.chmod(0o755)
        self.assertNotEqual(self.manifest["fingerprint"], state.capture(self.repo, ["source.txt"])["fingerprint"])

    def test_symlink_hashes_target_not_external_contents(self):
        external = self.root / "outside.txt"
        external.write_text("secret")
        (self.repo / "link").symlink_to(external)
        before = state.capture(self.repo, ["link"])
        external.write_text("changed secret")
        self.assertEqual(before, state.capture(self.repo, ["link"]))
        state.baseline(self.repo, self.base, "link")
        self.assertTrue((self.base / "link").is_symlink())

    def test_parent_symlink_and_traversal_are_rejected(self):
        (self.repo / "outside").symlink_to(self.root, target_is_directory=True)
        for path in ("../outside.txt", "/absolute", ".git/config", "outside/file"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                state.capture(self.repo, [path])

    def test_baseline_tampering_is_rejected(self):
        state.baseline(self.repo, self.base, "source.txt")
        (self.base / "source.txt").write_text("tampered")
        with self.assertRaises(ValueError):
            state.delta(self.repo, self.base, ["source.txt"])
        with self.assertRaises(ValueError):
            state.baseline(self.repo, self.base, "source.txt")

    def test_non_git_requires_scope(self):
        plain = self.root / "plain"
        plain.mkdir()
        (plain / "nginx.conf").write_text("config")
        with self.assertRaises(ValueError):
            state.capture(plain, [])
        self.assertIn("nginx.conf", state.capture(plain, ["nginx.conf"])["files"])

    def test_unstable_capture_is_rejected(self):
        different = {**self.manifest, "fingerprint": "changed"}
        with patch.object(state, "capture_once", side_effect=[self.manifest, different]):
            with self.assertRaises(ValueError):
                state.capture(self.repo, ["source.txt"])

    def test_append_only_conflict_preserves_original(self):
        path = self.save("once.json", {"original": True})
        with self.assertRaises(FileExistsError):
            state.write_once(path, {"overwrite": True})
        self.assertEqual({"original": True}, state.load(path))

    def test_manifest_tampering_is_rejected(self):
        value = copy.deepcopy(self.manifest)
        value["files"]["source.txt"]["sha256"] = "tampered"
        with self.assertRaises(ValueError):
            state.checked_manifest(self.save("bad.json", value))

    def test_duplicate_and_stale_findings_are_rejected(self):
        for review in (self.review([finding(), finding()]),
                       {**self.review(), "source_fingerprint": "old"}):
            with self.assertRaises(ValueError):
                state.findings(review, "api", 1, self.manifest)
        state.findings(self.review([]), "api", 1, self.manifest)

    def test_carry_forward_ids_and_new_maximum(self):
        previous = self.review([finding("api-8")])
        current = self.review([finding("api-8", "regressed"), finding("api-9")], 2)
        state.findings(current, "api", 2, self.manifest, [previous])
        for entry in (finding("api-8"), finding("api-2"), finding("api-10", "recurring")):
            with self.assertRaises(ValueError):
                state.findings(self.review([entry], 2), "api", 2, self.manifest, [previous])

    def test_incomplete_or_duplicate_applied_results_are_rejected(self):
        good = {"round": 1, "applied": [{"id": "api-1", "verified": "Relevant assertion passed"}],
                "failed": [], "skipped": []}
        state.applied(good, self.review())
        for result in ({**good, "applied": []},
                       {**good, "skipped": [{"id": "api-1", "reason": "duplicated"}]},
                       {**good, "round": 2}):
            with self.assertRaises(ValueError):
                state.applied(result, self.review())

    def evidence_record(self):
        before = self.save("before.json", self.manifest)
        after = self.save("after.json", self.manifest)
        log = self.root / "check.log"
        log.write_text("Relevant assertion passed\n")
        return {"criterion_id": "item.ac-1", "producer": "orchestrator", "kind": "empirical",
                "status": "passed", "command": "test the fixture", "exit_code": 0,
                "environment": "scratch fixture", "data_scope": "null case",
                "before_manifest": str(before), "after_manifest": str(after),
                "artifact_paths": [str(log)]}

    def test_evidence_binds_to_final_source(self):
        proof = self.evidence_record()
        state.evidence(proof, self.manifest)
        self.source.write_text("gate fix")
        with self.assertRaises(ValueError):
            state.evidence(proof, state.capture(self.repo, ["source.txt"]))

    def test_source_changing_check_and_missing_log_rejected(self):
        proof = self.evidence_record()
        self.source.write_text("gate generated source")
        changed = self.save("changed.json", state.capture(self.repo, ["source.txt"]))
        with self.assertRaises(ValueError):
            state.evidence({**proof, "after_manifest": str(changed)}, state.checked_manifest(changed))
        with self.assertRaises(ValueError):
            state.evidence({**proof, "artifact_paths": [str(self.root / "missing.log")]}, self.manifest)

    def test_failed_unverified_and_false_green_evidence(self):
        proof = self.evidence_record()
        state.evidence({**proof, "status": "failed", "exit_code": 1, "reason": "assertion failed"}, self.manifest)
        state.evidence({**proof, "status": "unverified", "exit_code": None, "reason": "no test environment"}, self.manifest)
        for changes in ({"exit_code": 1}, {"exit_code": True}, {"status": "unverified", "exit_code": 0}):
            with self.assertRaises(ValueError):
                state.evidence({**proof, **changes}, self.manifest)

    def test_cli_capture_and_append_only(self):
        scope = self.save("scope.json", ["source.txt"])
        output = self.root / "cli.json"
        command = ["python3", "-I", str(SCRIPT), "capture", "--repo", str(self.repo),
                   "--scope", str(scope), "--out", str(output)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(self.manifest, state.checked_manifest(output))
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(1, result.returncode)


if __name__ == "__main__":
    unittest.main()
