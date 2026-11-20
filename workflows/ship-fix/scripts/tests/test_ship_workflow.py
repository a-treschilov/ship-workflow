"""Executable v2 lifecycle fixtures, without agents, Docker or product repositories."""

import copy
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "ship_state.py"
spec = importlib.util.spec_from_file_location("workflow", SCRIPT.with_name("ship_workflow.py"))
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)
state = workflow.state


class LifecycleTests(unittest.TestCase):
    def test_ship_contract_with_exclusions_and_deliverables_keeps_contract_path(self):
        plan = self.root / 'plan.md'
        plan.write_text('approved requirement')
        snapshot = self.run / 'approved-plan.md'
        snapshot.write_bytes(plan.read_bytes())
        self.c.update(workflow='ship', plan_path=str(plan), approved_snapshot=str(snapshot),
                      plan_sha256=workflow.sha(plan), read_excludes={'api': ['private']},
                      deliverables={'api': [{'id': 'document', 'path': str(self.root / 'output.md'),
                                             'criterion_ids': ['fix.ac-1']}]})
        self.save('contract.json', self.c)
        self.assertEqual('ship', workflow.contract(self.contract)['workflow'])

    def test_setup_validation_records_failure_then_pass_without_rewriting(self):
        self.save('run.json', {'schema_version': 2, 'run_id': 'run-1', 'workflow': 'fix', 'repos': ['api']})
        broken = copy.deepcopy(self.c)
        broken['criteria'] = []
        self.save('contract.json', broken)
        with self.assertRaisesRegex(ValueError, 'criteria cannot be empty'):
            state.validate_run(self.contract, 'one', workflow)
        start = self.run / 'telemetry/setup-one-start.json'
        end = self.run / 'telemetry/setup-one-end.json'
        first = start.read_bytes(), end.read_bytes()
        self.assertEqual('failed', state.load(end)['status'])
        self.assertEqual(workflow.sha(start), state.load(end)['start_sha256'])
        self.save('contract.json', self.c)
        with self.assertRaises((FileExistsError, ValueError)):
            state.validate_run(self.contract, 'one', workflow)
        self.assertEqual(first, (start.read_bytes(), end.read_bytes()))
        self.assertEqual('passed', state.validate_run(self.contract, 'two', workflow)['status'])

    def test_recovery_reconciles_independent_histories_and_seals_decision(self):
        sources, audits = [], {}
        for number, used in enumerate((2, 1, 1), 1):
            run = self.root / f"old-{number}"
            run.mkdir()
            contract = run / "contract.json"
            contract.write_text(json.dumps({"run_id": run.name, "repos": ["api"],
                                            "repo_paths": {"api": str(self.repo)}}))
            history = run / "state.json"
            history.write_text("{}")
            source = {"run_id": run.name, "contract_path": str(contract),
                      "contract_sha256": workflow.sha(contract), "state_path": str(history),
                      "state_sha256": workflow.sha(history), "used_passes": used}
            sources.append(source)
            audits[str(contract)] = {"run_id": run.name, "state_path": str(history),
                                     "state_sha256": workflow.sha(history), "review_fix_count": used,
                                     "grants": [], "granted_additional_passes": 0}
        item = {"repo": "api", "work_item": "work-1", "approved_by": "user",
                "user_authorization": "4 used and +2", "approval_question": "Approve 4 and +2?",
                "approved_at": "2026-01-01T00:00:00Z", "reason": "Reconcile histories",
                "additional_passes": 2, "used_passes": 4, "effective_limit": 5,
                "recovery_sources": sources}
        decision = self.save("recovery.json", item)
        item.update({"decision_path": decision, "decision_sha256": workflow.sha(decision)})
        self.c["budget_continuity"] = [item]
        with patch.object(workflow, "validate_history", side_effect=lambda path, repo, **kw: audits[str(path)]):
            self.assertEqual(1, workflow.review_budget([], self.contract, self.c, "api")[0])
            item["recovery_sources"] = [sources[0], sources[0]]
            Path(decision).write_text(json.dumps({k: v for k, v in item.items()
                                                  if k not in {"decision_path", "decision_sha256"}}))
            item["decision_sha256"] = workflow.sha(decision)
            with self.assertRaisesRegex(ValueError, "Duplicate or cyclic"):
                workflow.review_budget([], self.contract, self.c, "api")
            item["recovery_sources"] = sources
            Path(decision).write_text(json.dumps({k: v for k, v in item.items()
                                                  if k not in {"decision_path", "decision_sha256"}}))
            item["decision_sha256"] = workflow.sha(decision)
            item["used_passes"] = 3
            with self.assertRaisesRegex(ValueError, "decision does not match"):
                workflow.review_budget([], self.contract, self.c, "api")
            item["used_passes"] = 4
            Path(sources[0]["contract_path"]).write_text("{}")
            with self.assertRaisesRegex(ValueError, "contract seal mismatch"):
                workflow.review_budget([], self.contract, self.c, "api")

    def test_identified_related_run_requires_continuity(self):
        plans = self.root / "plans"
        current = plans / ".fix-runs" / "current" / "contract.json"
        current.parent.mkdir(parents=True)
        other = plans / ".ship-runs" / "earlier" / "contract.json"
        other.parent.mkdir(parents=True)
        other.write_text(json.dumps({"budget_work_items": {"api": "work-1"},
                                     "repo_paths": {"api": str(self.repo)}}))
        value = {"budget_work_items": {"api": "work-1"}, "repo_paths": {"api": str(self.repo)}}
        with self.assertRaisesRegex(ValueError, "missing from budget continuity"):
            workflow.check_related_budgets(value, "api", current)
        other.write_text(json.dumps({"budget_work_items": {"api": "other"},
                                     "repo_paths": {"api": str(self.repo)}}))
        workflow.check_related_budgets(value, "api", current)

    def test_input_snapshot_rejects_symlink_into_excluded_tree(self):
        secret = self.repo / "secret"
        secret.mkdir()
        (secret / "input.txt").write_text("private")
        alias = self.repo / "alias"
        alias.symlink_to(secret, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "read_excludes"):
            workflow.snapshot_inputs([str(alias / "input.txt")], self.run / "inputs", self.repo, ["secret"])
        self.assertFalse((self.run / "inputs").exists())

    def test_repo_review_budget_uses_continuity_allowance(self):
        continuity = {"used_passes": 10, "effective_limit": 12}
        with patch.object(workflow, "budget_continuity", return_value=continuity):
            limit, grants = workflow.review_budget([], self.contract, self.c, "api")
        self.assertEqual((2, []), (limit, grants))

    def test_declared_successor_requires_budget_continuity(self):
        self.c["successor_scopes"] = [{"repo": "api", "slice_id": "s2", "work_item": "work-1"}]
        self.save("contract.json", self.c)
        with self.assertRaisesRegex(ValueError, "requires matching budget continuity"):
            workflow.contract(self.contract)

    def test_budget_continuity_preserves_human_decision_and_rejects_changed_source(self):
        prior = self.root / "prior-run"
        prior.mkdir()
        contract = prior / "contract.json"
        contract.write_text(json.dumps({"schema_version": 2, "workflow": "fix", "run_id": "prior-run",
                                        "repos": ["api"], "repo_paths": {"api": str(self.repo)}}))
        directory = prior / "api" / "slices" / "s2"
        directory.mkdir(parents=True)
        source = directory / "state-1.json"
        source.write_text(json.dumps({"schema_version": 2, "run_id": "prior-run", "repo": "api",
                                      "slice_id": "s2", "status": "implemented",
                                      "review_fix_count": 0,
                                      "artifact_hashes": {str(contract): workflow.sha(contract)}}))
        decision = self.root / "decision.txt"
        decision.write_text("User decision: 10 of 12")
        item = {"repo": "api", "slice_id": "s2", "source_slice_id": "s2", "approved_by": "user",
                "work_item": "work-1", "user_authorization": "10 of 12", "reason": "reconciled history",
                "approved_at": "2026-01-01T00:00:00Z", "used_passes": 10, "effective_limit": 12,
                "source_contract_path": str(contract), "source_contract_sha256": workflow.sha(contract),
                "source_state_path": str(source), "source_state_sha256": workflow.sha(source),
                "decision_path": str(decision), "decision_sha256": workflow.sha(decision)}
        self.c["budget_continuity"] = [item]
        self.c["successor_scopes"] = [{"repo": "api", "slice_id": "s2", "work_item": "work-1"}]
        self.save("contract.json", self.c)
        workflow.contract(self.contract)
        self.assertEqual(2, item["effective_limit"] - workflow.budget_continuity(
            self.c, "api", self.contract, "s2")["used_passes"])
        original = source.read_bytes()
        source.write_text("{}")
        with self.assertRaisesRegex(ValueError, "state seal mismatch"):
            workflow.budget_continuity(self.c, "api", self.contract, "s2")
        source.write_bytes(original)
        decision.write_text("modified")
        with self.assertRaisesRegex(ValueError, "decision seal mismatch"):
            workflow.budget_continuity(self.c, "api", self.contract, "s2")

    def test_validate_contract_rejects_foreign_slice_schedule(self):
        schedule = self.save("foreign-schedule.json", {"schema_version": 2, "run_id": "another-run", "repo": "api"})
        self.c["slice_schedules"] = {"api": {"path": schedule, "sha256": workflow.sha(schedule)}}
        self.save("contract.json", self.c)
        result = subprocess.run(["python3", "-I", "-B", str(SCRIPT), "validate-contract", "--contract", self.contract],
                                capture_output=True, text=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("not bound to this run/repo", result.stderr)

    def test_deferred_cross_repo_assessment_preserves_local_and_legacy_requirements(self):
        local_a = {"id": "a.local", "mandatory": True, "repos": ["a"]}
        local_b = {"id": "b.local", "mandatory": True, "repos": ["b"]}
        cross = {"id": "cross", "mandatory": True, "repos": ["a", "b"]}
        value = {"criteria": [local_a, local_b, cross]}
        self.assertEqual(["a.local", "cross"], [x["id"] for x in workflow.criteria_for(value, "a")])
        cross["assessment_repo"] = "b"
        self.assertEqual(["a.local"], [x["id"] for x in workflow.criteria_for(value, "a")])
        self.assertEqual(["b.local", "cross"], [x["id"] for x in workflow.criteria_for(value, "b")])

    def test_cross_repo_proof_honors_dependency_exclusions(self):
        web = self.root / "web"
        web.mkdir()
        subprocess.run(["git", "init", "-q", str(web)], check=True)
        (web / "source.txt").write_text("tracked")
        subprocess.run(["git", "-C", str(web), "add", "source.txt"], check=True)
        (web / "generated").mkdir()
        (web / "generated/output.txt").write_text("ignored output")
        dependency = self.save("web-manifest.json", state.capture(web, ["source.txt"], ["generated"]))
        self.save("web/state-1.json", {"status": "initialized", "source_scope": ["source.txt"],
                                       "excludes": ["generated"]})
        value = copy.deepcopy(self.c)
        value["repos"].append("web")
        value["repo_paths"]["web"] = str(web)
        value["criteria"][0]["repos"].append("web")
        value["_contract_path"] = self.contract
        record_path = self.evidence("fix.ac-1")
        record = state.load(record_path)
        record["dependency_manifests"] = {"web": dependency}
        self.save(str(Path(record_path).relative_to(self.run)), record)
        workflow.proof(value, "api", record_path, state.checked_manifest(record["after_manifest"]), "fix.ac-1")
        del value["_contract_path"]
        with self.assertRaisesRegex(ValueError, "source states differ"):
            workflow.proof(value, "api", record_path, state.checked_manifest(record["after_manifest"]), "fix.ac-1")

    def test_deferred_cross_repo_contract_rejects_wrong_owner_and_missing_local_criterion(self):
        other = self.root / "other"
        other.mkdir()
        self.c["repos"].append("web")
        self.c["repo_paths"]["web"] = str(other)
        for group in ("required_gates", "pre_review_checks", "preflight_checks"):
            self.c[group]["web"] = copy.deepcopy(self.c[group]["api"])
        self.c["required_passes"]["web"] = ["logic"]
        template = copy.deepcopy(self.c["criteria"][0])
        template.update(id="cross", repos=["api", "web"], assessment_repo="api")
        self.c["criteria"].append(template)
        self.save("contract.json", self.c)
        with self.assertRaisesRegex(ValueError, "last participating repo"):
            workflow.contract(self.contract)
        template["assessment_repo"] = "web"
        self.save("contract.json", self.c)
        with self.assertRaisesRegex(ValueError, "local criterion for web"):
            workflow.contract(self.contract)
        local = copy.deepcopy(template)
        local.update(id="web.local", repos=["web"])
        del local["assessment_repo"]
        self.c["criteria"].append(local)
        self.save("contract.json", self.c)
        workflow.contract(self.contract)

    def test_final_owner_cannot_omit_deferred_criterion_from_assessment(self):
        self.c["repos"].append("web")
        local = copy.deepcopy(self.c["criteria"][0])
        local.update(id="web.local", repos=["web"])
        cross = copy.deepcopy(local)
        cross.update(id="cross", repos=["api", "web"], assessment_repo="web")
        self.c["criteria"].extend([local, cross])
        assessment = self.save("incomplete-assessment.json", self.artifact(
            repo="web", producer="orchestrator", requirements_match=True,
            criteria=[{"criterion_id": "web.local"}]))
        with self.assertRaisesRegex(ValueError, "Missing acceptance assessment"):
            workflow.assessment(self.c, "web", assessment, state.checked_manifest(self.initial))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ["SHIP_TEST_TMPDIR"], prefix="workflow-v2-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        self.source = self.repo / "source.txt"
        self.source.write_text("before\n")
        self.run = self.root / "run-1"
        self.run.mkdir()
        self.scope = self.save("scope.json", ["source.txt"])
        self.initial = self.capture("initial.json")
        self.counter = 0
        self.base = self.run / "api/baselines"
        state.baseline(self.repo, self.base, "source.txt")
        self.c = {
            "schema_version": 2, "workflow": "fix", "run_id": "run-1", "repos": ["api"],
            "repo_paths": {"api": str(self.repo)}, "criteria": [
                {"id": "fix.ac-1", "kind": "empirical", "mandatory": True, "repos": ["api"],
                 "requirement": "return correct result", "source": "original brief",
                 "verification": "assert observable output", "environment": "fixture", "data_scope": "null"}],
            "required_gates": {"api": [{"id": "gate.tests", "kind": "empirical", "command": "fixture tests",
                                         "verification": "tests pass"}]},
            "pre_review_checks": {"api": [{"id": "early.tests", "kind": "empirical", "command": "targeted tests",
                                           "verification": "targeted tests pass"}]},
            "preflight_checks": {"api": [{"id": "env.runtime", "kind": "static", "verification": "safe mount"}]},
            "required_passes": {"api": ["logic", "wiring"]}, "prerequisites": [], "cross_repo_contracts": [],
            "acceptance": {"original_scenario": "null input", "expected_behavior": "correct output",
                           "non_goals": "no unrelated changes", "mode": "improvement", "criterion_ids": ["fix.ac-1"]},
        }
        self.contract = self.save("contract.json", self.c)

    def save(self, name, data):
        path = self.run / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return str(path)

    def capture(self, name):
        return self.save(name, state.capture(self.repo, ["source.txt"]))

    def artifact(self, **kwargs):
        return {"schema_version": 2, "run_id": "run-1", "repo": "api", **kwargs}

    def evidence(self, identifier, kind="empirical", status="passed", source=None, producer="orchestrator"):
        self.counter += 1
        source = source or self.capture(f"proof-{self.counter}-source.json")
        log = self.run / f"proof-{self.counter}.log"
        log.write_text("assert expected output\n")
        cmd = {"gate.tests": "fixture tests", "early.tests": "targeted tests"}.get(identifier, "acceptance test")
        record = self.artifact(
            criterion_id=identifier, claims=[{"id": identifier, "assertion": "observable output checked"}],
            kind=kind, producer=producer, status=status, command=cmd, assertion="runtime mount inspected", scenario="null input",
            exit_code=0 if status == "passed" else 1, environment="fixture", data_scope="null",
            before_manifest=source, after_manifest=source, artifact_paths=[str(log)],
            artifact_hashes=workflow.seal_files([source, str(log)]), reason="fixture failed" if status != "passed" else "")
        return self.save(f"proof-{self.counter}.json", record)

    def move(self, status, **kwargs):
        events = workflow.history(self.run / "api")
        self.counter += 1
        request = self.save(f"request-{self.counter}.json", {
            "status": status, "source_manifest": self.capture(f"source-{self.counter}.json"),
            "scope_path": self.scope, "baseline_dir": str(self.base), **kwargs})
        out = self.run / "api" / f"state-{len(events)+1}.json"
        return workflow.transition(self.contract, "api", request, str(out))

    def ready(self):
        self.move("initialized")
        proof = self.evidence("env.runtime", "static")
        pre = self.save("preflight.json", self.artifact(status="passed", evidence_paths=[proof]))
        self.move("ready", preflight_path=pre)

    def implement(self):
        self.source.write_text("after\n")
        matrix = self.save("matrix.json", self.artifact(criteria=[{
            "criterion_id": "fix.ac-1", "implementation": "source.txt", "assertion": "null returns expected value",
            "status": "unverified", "reason": "independent acceptance pending", "evidence_paths": []}]))
        identity = {"original_agent_id": "original-1"} if self.c.get("telemetry_policy") else {}
        self.move("implemented", matrix_path=matrix, **identity)
        self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])

    def review(self, findings=None, dispositions=None):
        n = len([e for e in workflow.history(self.run / "api")
                 if e[2]["status"] in {"needs-fix", "reviewed"}
                 and not e[2].get("budget_grant_applied")]) + 1
        current = state.capture(self.repo, ["source.txt"])
        result = self.artifact(repo="api", round=n, source_fingerprint=current["fingerprint"], accepted=True,
            coverage={"read": ["source.txt"], "skipped": [], "mandatory_passes": [
                {"id": "logic", "status": "inspected", "reason": "checked branches", "sites": ["source.txt"]},
                {"id": "wiring", "status": "not-applicable", "reason": "no constructors", "sites": []}]},
            findings=findings or [], dispositions=dispositions or [])
        return self.save(f"round-{n}-findings.json", result)

    def verify(self):
        self.move("reviewed", review_path=self.review())
        self.move("gates-passed", gate_evidence_paths=[self.evidence("gate.tests")])
        assessment = self.save("assessment.json", self.artifact(producer="orchestrator", requirements_match=True,
            criteria=[{"criterion_id": "fix.ac-1", "implementation": "met", "verification": "verified",
                       "reason": "the assertion exercises the original null scenario",
                       "evidence_paths": [self.evidence("fix.ac-1")]}]))
        self.move("verified", assessment_path=assessment)

    def test_gate_evidence_correction_preserves_history(self):
        self.ready()
        self.implement()
        self.move("reviewed", review_path=self.review())
        original = self.evidence("gate.tests")
        self.move("gates-passed", gate_evidence_paths=[original])
        replacement = self.evidence("gate.tests")
        with self.assertRaisesRegex(ValueError, "gate correction reason"):
            self.move("gates-passed", gate_evidence_paths=[replacement])
        with self.assertRaisesRegex(ValueError, "replacement evidence"):
            self.move("gates-passed", gate_evidence_paths=[original], gate_correction_reason="wrong metadata")
        corrected = self.move("gates-passed", gate_evidence_paths=[replacement],
                              gate_correction_reason="correct metadata")
        self.assertEqual([replacement], corrected["gate_evidence_paths"])
        self.assertIn(original, corrected["artifact_hashes"])
        self.assertIn(replacement, corrected["artifact_hashes"])
        self.assertEqual(0, corrected["review_fix_count"])

    def test_legacy_alias_manifest_can_continue_without_rewriting_review(self):
        if not (self.repo / "SOURCE.TXT").exists():
            self.skipTest("filesystem distinguishes case")
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "source.txt"], check=True)
        self.source.rename(self.repo / "rename.tmp")
        (self.repo / "rename.tmp").rename(self.repo / "SOURCE.TXT")
        self.c["read_excludes"] = {"api": ["private"]}
        self.save("contract.json", self.c)

        def legacy_manifest(label):
            current = state.capture(self.repo, [], read_excludes=["private"])
            current.pop("fingerprint")
            current["files"]["SOURCE.TXT"] = current["files"]["source.txt"]
            return self.save(label, {**current, "fingerprint": state.digest(current)})

        initial = legacy_manifest("legacy-initial.json")
        self.move("initialized", source_manifest=initial)
        preflight = self.save("preflight.json", self.artifact(
            status="passed", evidence_paths=[self.evidence("env.runtime", "static", source=initial)]))
        self.move("ready", preflight_path=preflight, source_manifest=initial)
        self.scope = self.save("alias-scope.json", ["source.txt", "SOURCE.TXT"])
        self.implement()
        reviewed_source = legacy_manifest("legacy-reviewed.json")
        review_path = self.review()
        review = state.load(review_path)
        review["source_fingerprint"] = state.checked_manifest(reviewed_source)["fingerprint"]
        review["coverage"]["read"] = ["SOURCE.TXT"]
        self.save("round-1-findings.json", review)
        self.move("reviewed", source_manifest=reviewed_source, review_path=review_path)
        state_path = self.run / "api/state-5.json"
        history = {path: Path(path).read_bytes() for path in [initial, reviewed_source, review_path, state_path]}
        self.assertTrue(workflow.review_budget_status(self.contract, "api")["source_matches_state"])
        unowned = self.repo / "unowned.txt"
        unowned.write_text("concurrent change")
        with self.assertRaisesRegex(ValueError, "Changes outside touched scope"):
            self.move("gates-passed", gate_evidence_paths=[self.evidence("gate.tests")])
        unowned.unlink()
        self.move("gates-passed", gate_evidence_paths=[self.evidence("gate.tests")])
        self.assertEqual("valid", workflow.validate_history(self.contract, "api")["status"])
        self.assertEqual(history, {path: Path(path).read_bytes() for path in history})

    def test_full_lifecycle_and_cli_completion(self):
        self.ready()
        self.implement()
        self.verify()
        result = subprocess.run(["python3", "-I", "-B", str(SCRIPT), "complete", "--contract", self.contract,
                                 "--out", str(self.run / "completion.json")], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        certificate = state.load(self.run / "completion.json")
        self.assertEqual("completed", certificate["status"])
        self.assertEqual({"standard_limit": 3, "granted_additional_passes": 0, "effective_limit": 3,
                          "used_passes": 0, "extra_passes_used": 0, "grants": []},
                         certificate["repos"]["api"]["review_budget"])
        history_before = [path.read_bytes() for path in sorted((self.run / "api").glob("state-*.json"))]
        self.assertEqual("valid", workflow.validate_history(self.contract, "api")["status"])
        self.assertEqual(history_before,
                         [path.read_bytes() for path in sorted((self.run / "api").glob("state-*.json"))])
        with self.assertRaises(FileExistsError):
            workflow.complete(self.contract, str(self.run / "completion.json"))

    def test_ship_checker_required_and_snapshot_preserved(self):
        plan = self.root / "plan.md"
        plan.write_text("approved requirement")
        snapshot = self.run / "approved-plan.md"
        snapshot.write_bytes(plan.read_bytes())
        self.c.update(workflow="ship", plan_path=str(plan), approved_snapshot=str(snapshot), plan_sha256=workflow.sha(plan))
        self.save("contract.json", self.c)
        self.ready()
        self.implement()
        self.move("reviewed", review_path=self.review())
        self.move("gates-passed", gate_evidence_paths=[self.evidence("gate.tests")])
        bad = self.save("assessment.json", self.artifact(producer="orchestrator", requirements_match=True, criteria=[]))
        with self.assertRaisesRegex(ValueError, "self-certified"):
            self.move("verified", assessment_path=bad)
        good = self.save("checker.json", self.artifact(producer="checker", requirements_match=True, criteria=[{
            "criterion_id": "fix.ac-1", "implementation": "met", "verification": "verified", "reason": "exact requirement",
            "evidence_paths": [self.evidence("fix.ac-1")]}]))
        self.move("verified", assessment_path=good)
        workflow.complete(self.contract, str(self.run / "completion.json"))
        plan.write_text("changed requirement")
        with self.assertRaisesRegex(ValueError, "plan changed"):
            workflow.contract(self.contract)

    def test_preflight_blocker_prevents_implementation(self):
        self.move("initialized")
        pre = self.save("preflight.json", self.artifact(status="blocked", reason="isolated DB unavailable"))
        self.move("preflight-blocked", preflight_path=pre, reason="isolated DB unavailable")
        self.assertEqual("before\n", self.source.read_text())
        with self.assertRaises((ValueError, TypeError)):
            self.move("implemented", matrix_path="missing")
        with self.assertRaises(ValueError):
            workflow.complete(self.contract, str(self.run / "completion.json"))

    def test_wrong_evidence_kind_producer_repo_and_hash(self):
        initial = state.checked_manifest(self.initial)
        for changes, message in [({"kind": "static"}, "kind"), ({"producer": "implementer"}, "independently"),
                                 ({"repo": "other"}, "run/repo"), ({"run_id": "other"}, "run/repo"),
                                 ({"artifact_hashes": {}}, "hashes")]:
            p = self.evidence("gate.tests", source=self.initial)
            record = state.load(p)
            record.update(changes)
            self.save(Path(p).name, record)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                workflow.proof(self.c, "api", p, initial, "gate.tests", "orchestrator")

    def test_clean_review_does_not_prove_original_scenario(self):
        self.ready()
        self.implement()
        self.move("reviewed", review_path=self.review())
        self.move("gates-passed", gate_evidence_paths=[self.evidence("gate.tests")])
        a = self.save("bad-assessment.json", self.artifact(producer="orchestrator", requirements_match=True, criteria=[{
            "criterion_id": "fix.ac-1", "implementation": "met", "verification": "failed", "reason": "null still wrong",
            "evidence_paths": [self.evidence("fix.ac-1", status="failed")]}]))
        with self.assertRaisesRegex(ValueError, "not met and verified"):
            self.move("verified", assessment_path=a)

    def test_stale_review_and_post_checker_edit_block_completion(self):
        self.ready()
        self.implement()
        self.verify()
        self.source.write_text("later edit\n")
        with self.assertRaisesRegex(ValueError, "Stale"):
            workflow.complete(self.contract, str(self.run / "completion.json"))

    def test_unregistered_edit_detected(self):
        self.ready()
        # Explicit dependency recorded in initial manifest but not writable scope.
        extra = self.repo / "consumer.txt"
        extra.write_text("outside scope")
        self.save("scope.json", ["source.txt", "consumer.txt"])
        self.source.write_text("after")
        # Initial manifest does not include this dependency; union comparison still catches it.
        self.save("scope.json", ["source.txt"])
        # Non-git captures require explicit dependency scope, use direct fixture manifest.
        old = state.checked_manifest(self.initial)
        current = state.capture(self.repo, ["source.txt", "consumer.txt"])
        p = self.save("extra-source.json", current)
        request = self.save("extra-request.json", {"status": "implemented", "source_manifest": p, "scope_path": self.scope})
        with self.assertRaises(ValueError):
            workflow.transition(self.contract, "api", request, str(self.run / "api/state-3.json"))
        self.assertNotEqual(old, current)

    def test_review_requires_coverage_and_mandatory_passes(self):
        self.ready()
        self.implement()
        for coverage in ({"read": [], "skipped": [], "mandatory_passes": []},
                         {"read": ["source.txt"], "skipped": [], "mandatory_passes": []}):
            p = self.review()
            record = state.load(p)
            record["coverage"] = coverage
            self.save(Path(p).name, record)
            with self.assertRaises(ValueError):
                self.move("reviewed", review_path=p)

    def test_missing_gate_and_matrix_cannot_pass(self):
        self.ready()
        bad = self.save("bad-matrix.json", self.artifact(criteria=[]))
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            self.move("implemented", matrix_path=bad)
        self.implement()
        self.move("reviewed", review_path=self.review())
        with self.assertRaisesRegex(ValueError, "Missing check"):
            self.move("gates-passed", gate_evidence_paths=[])

    def test_review_fix_rounds_proof_and_counters(self):
        self.ready()
        self.implement()

        def finding(identifier):
            return {"id": identifier, "file": "source.txt", "line": 1, "severity": "Warning", "status": "new",
                    "category": "Logic", "description": "wrong output", "recommendation": "fix result",
                    "failure_scenario": "null returns wrong result", "verification_expectation": "assert null output",
                    "criterion_ids": ["fix.ac-1"], "verification_kind": "empirical"}

        def source_proof(identifier, name):
            proof_path = self.evidence("fix.ac-1")
            proof = state.load(proof_path)
            proof["claims"] = [{"id": identifier, "assertion": "observable fix verified"}]
            proof["artifact_paths"].append(str(self.source))
            proof["artifact_hashes"] = workflow.seal_files(
                [proof["before_manifest"], proof["after_manifest"], *proof["artifact_paths"]]
            )
            return self.save(name, proof)

        first_finding = finding("api-1")
        self.move("needs-fix", review_path=self.review([first_finding]))
        self.source.write_text("fixed round one\n")
        first_proof = source_proof("api-1", "round-1-source-proof.json")
        first_applied = self.save("round-1-applied.json", self.artifact(round=1, applied=[{
            "id": "api-1", "verified": "observable result checked", "verification_kind": "empirical",
            "evidence_paths": [first_proof]}], failed=[], skipped=[]))
        phase = ({"repair_phase_id": self.repair_pair(action="review-fix")}
                 if self.c.get("telemetry_policy") else {})
        event = self.move("edited-unverified", action="review-fix", applied_path=first_applied, **phase)
        self.assertEqual(1, event["review_fix_count"])
        self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])

        second_finding = finding("api-2")
        round_two_review = self.review(
            [second_finding],
            [{"id": "api-1", "status": "resolved", "reason": "round-one fix checked",
              "evidence_paths": [first_proof]}]
        )
        self.move("needs-fix", review_path=round_two_review)
        self.source.write_text("fixed round two\n")
        second_proof = source_proof("api-2", "round-2-source-proof.json")
        second_applied = self.save("round-2-applied.json", self.artifact(round=2, applied=[{
            "id": "api-2", "verified": "observable result checked", "verification_kind": "empirical",
            "evidence_paths": [second_proof]}], failed=[], skipped=[]))
        phase = ({"repair_phase_id": self.repair_pair("repair-round-2", action="review-fix")}
                 if self.c.get("telemetry_policy") else {})
        event = self.move("edited-unverified", action="review-fix", applied_path=second_applied, **phase)
        self.assertEqual(2, event["review_fix_count"])
        self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])
        with self.assertRaisesRegex(ValueError, "Stale review fingerprint"):
            self.move("reviewed", review_path=round_two_review)

        r = self.review(dispositions=[{"id": "api-2", "status": "resolved", "reason": "round-two fix checked",
                                      "evidence_paths": [second_proof]}])
        event = self.move("reviewed", review_path=r)
        self.assertEqual(3, event["latest_review_round"])
        self.assertEqual(2, event["review_fix_count"])
        self.assertTrue(Path(first_proof).is_file())
        self.assertTrue(Path(second_proof).is_file())

    def test_source_artifact_can_advance_across_repo_review_fix_and_stale_review_is_rejected(self):
        self.ready()
        self.implement()

        def finding(identifier):
            return {"id": identifier, "file": "source.txt", "line": 1, "severity": "Warning", "status": "new",
                    "category": "Logic", "description": "wrong output", "recommendation": "fix result",
                    "failure_scenario": "null returns wrong result", "verification_expectation": "assert null output",
                    "criterion_ids": ["fix.ac-1"], "verification_kind": "static"}

        def source_proof(identifier, name):
            manifest = self.capture(name + "-manifest.json")
            record = self.artifact(
                criterion_id="fix.ac-1", claims=[{"id": identifier, "assertion": "source assertion inspected"}],
                kind="static", producer="implementer", status="passed", assertion="source behavior inspected",
                environment="temporary repo fixture", data_scope="source.txt only",
                before_manifest=manifest, after_manifest=manifest, artifact_paths=[str(self.source)],
                artifact_hashes=workflow.seal_files([manifest, str(self.source)])
            )
            return self.save(name + ".json", record)

        self.move("needs-fix", review_path=self.review([finding("api-1")]))
        self.source.write_text("repo fix one\n")
        first_proof = source_proof("api-1", "repo-source-proof-1")
        first_applied = self.save("repo-applied-1.json", self.artifact(round=1, applied=[{
            "id": "api-1", "verified": "source inspection passed", "verification_kind": "static",
            "evidence_paths": [first_proof]}], failed=[], skipped=[]))
        self.move("edited-unverified", action="review-fix", applied_path=first_applied)
        self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])

        round_two = self.review(
            [finding("api-2")],
            [{"id": "api-1", "status": "resolved", "reason": "round-one source fix inspected",
              "evidence_paths": [first_proof]}]
        )
        self.move("needs-fix", review_path=round_two)
        self.source.write_text("repo fix two\n")
        second_proof = source_proof("api-2", "repo-source-proof-2")
        second_applied = self.save("repo-applied-2.json", self.artifact(round=2, applied=[{
            "id": "api-2", "verified": "source inspection passed", "verification_kind": "static",
            "evidence_paths": [second_proof]}], failed=[], skipped=[]))
        event = self.move("edited-unverified", action="review-fix", applied_path=second_applied)
        self.assertEqual(2, event["review_fix_count"])
        self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])

        with self.assertRaisesRegex(ValueError, "Stale review fingerprint"):
            self.move("reviewed", review_path=round_two)
        final_review = self.review(dispositions=[{
            "id": "api-2", "status": "resolved", "reason": "round-two source fix inspected",
            "evidence_paths": [second_proof]
        }])
        event = self.move("reviewed", review_path=final_review)
        self.assertEqual(3, event["latest_review_round"])
        self.assertEqual(2, event["review_fix_count"])

    def test_mutated_log_invalidates_immutable_history(self):
        self.ready()
        record = state.load(self.run / "preflight.json")
        log = state.load(record["evidence_paths"][0])["artifact_paths"][0]
        Path(log).write_text("overwritten")
        with self.assertRaisesRegex(ValueError, "Immutable artifact changed"):
            self.implement()

    def test_phase_append_only_and_negative_duration(self):
        start = {"run_id": "run-1", "workflow": "fix", "phase_id": "implement-1", "phase": "implement",
                 "repo": "api", "event": "phase-start", "at": "2026-10-12T00:00:00Z"}
        p = self.save("phase-start-request.json", start)
        out = self.run / "telemetry/implement-1-start.json"
        workflow.phase(self.contract, p, str(out))
        with self.assertRaises(FileExistsError):
            workflow.phase(self.contract, p, str(out))
        end = {**start, "event": "phase-end", "status": "passed", "at": "2026-10-11T23:59:59Z"}
        p = self.save("phase-end-request.json", end)
        with self.assertRaisesRegex(ValueError, "Negative"):
            workflow.phase(self.contract, p, str(self.run / "telemetry/implement-1-end.json"))

    def test_phase_end_reason_validated_and_required_by_policy(self):
        start = {"run_id": "run-1", "workflow": "fix", "phase_id": "implement-2", "phase": "implement",
                 "repo": "api", "event": "phase-start", "at": "2026-10-12T00:00:00Z"}
        workflow.phase(self.contract, self.save("s.json", start), str(self.run / "telemetry/implement-2-start.json"))
        end = {**start, "event": "phase-end", "status": "interrupted", "at": "2026-10-12T00:10:00Z"}
        out = str(self.run / "telemetry/implement-2-end.json")
        self.c["telemetry_policy"] = {"repair_phase_required": False, "end_reason_required": True}
        self.save("contract.json", self.c)
        with self.assertRaisesRegex(ValueError, "needs end_reason"):
            workflow.phase(self.contract, self.save("e.json", end), out)
        with self.assertRaisesRegex(ValueError, "Invalid phase end_reason"):
            workflow.phase(self.contract, self.save("e.json", {**end, "end_reason": "too-long"}), out)
        workflow.phase(self.contract, self.save("e.json", {**end, "end_reason": "unknown"}), out)
        self.assertEqual(state.load(out)["end_reason"], "unknown")

    def test_phase_session_id_autofill_from_env(self):
        start = {"run_id": "run-1", "workflow": "fix", "phase_id": "sess-autofill", "phase": "implement",
                 "repo": "api", "event": "phase-start", "at": "2026-10-12T00:00:00Z"}
        out = self.run / "telemetry/sess-autofill-start.json"
        with patch.dict(os.environ, {"CODEX_SESSION_ID": "session-abc", "CODEX_THREAD_ID": "thread-abc"}):
            workflow.phase(self.contract, self.save("sess-autofill.json", start), str(out))
        self.assertEqual("session-abc", state.load(out)["session_id"])

    def test_phase_session_id_falls_back_to_thread_env(self):
        start = {"run_id": "run-1", "workflow": "fix", "phase_id": "sess-fallback", "phase": "implement",
                 "repo": "api", "event": "phase-start", "at": "2026-10-12T00:00:00Z"}
        out = self.run / "telemetry/sess-fallback-start.json"
        with patch.dict(os.environ, {"CODEX_SESSION_ID": "", "CODEX_THREAD_ID": "thread-abc"}):
            workflow.phase(self.contract, self.save("sess-fallback.json", start), str(out))
        self.assertEqual("thread-abc", state.load(out)["session_id"])

    def test_phase_session_id_explicit_preserved(self):
        start = {"run_id": "run-1", "workflow": "fix", "phase_id": "sess-explicit", "phase": "implement",
                 "repo": "api", "event": "phase-start", "at": "2026-10-12T00:00:00Z",
                 "session_id": "explicit-1"}
        out = self.run / "telemetry/sess-explicit-start.json"
        with patch.dict(os.environ, {"CODEX_SESSION_ID": "env-1", "CODEX_THREAD_ID": "env-1"}):
            workflow.phase(self.contract, self.save("sess-explicit.json", start), str(out))
        self.assertEqual("explicit-1", state.load(out)["session_id"])

    def test_phase_session_id_invalid_rejected(self):
        start = {"run_id": "run-1", "workflow": "fix", "phase_id": "sess-invalid", "phase": "implement",
                 "repo": "api", "event": "phase-start", "at": "2026-10-12T00:00:00Z",
                 "session_id": "bad id!"}
        with self.assertRaisesRegex(ValueError, "Invalid phase session_id"):
            workflow.phase(self.contract, self.save("sess-invalid.json", start),
                           str(self.run / "telemetry/sess-invalid-start.json"))

    def test_phase_session_id_empty_without_env(self):
        start = {"run_id": "run-1", "workflow": "fix", "phase_id": "sess-none", "phase": "implement",
                 "repo": "api", "event": "phase-start", "at": "2026-10-12T00:00:00Z"}
        out = self.run / "telemetry/sess-none-start.json"
        with patch.dict(os.environ, {"CODEX_SESSION_ID": "", "CODEX_THREAD_ID": ""}):
            workflow.phase(self.contract, self.save("sess-none.json", start), str(out))
        self.assertEqual("", state.load(out)["session_id"])

    def test_phase_session_id_invalid_env_empty(self):
        start = {"run_id": "run-1", "workflow": "fix", "phase_id": "sess-badenv", "phase": "implement",
                 "repo": "api", "event": "phase-start", "at": "2026-10-12T00:00:00Z"}
        out = self.run / "telemetry/sess-badenv-start.json"
        with patch.dict(os.environ, {"CODEX_SESSION_ID": "not valid!", "CODEX_THREAD_ID": ""}):
            workflow.phase(self.contract, self.save("sess-badenv.json", start), str(out))
        self.assertEqual("", state.load(out)["session_id"])

    def test_phase_end_session_id_mismatch_rejected(self):
        start = {"run_id": "run-1", "workflow": "fix", "phase_id": "sess-mismatch", "phase": "implement",
                 "repo": "api", "event": "phase-start", "at": "2026-10-12T00:00:00Z"}
        with patch.dict(os.environ, {"CODEX_SESSION_ID": "sess-a", "CODEX_THREAD_ID": "sess-a"}):
            workflow.phase(self.contract, self.save("sess-mismatch.json", start),
                           str(self.run / "telemetry/sess-mismatch-start.json"))
        end = {**start, "event": "phase-end", "status": "passed", "at": "2026-10-12T00:10:00Z",
               "session_id": "sess-b"}
        with self.assertRaisesRegex(ValueError, "mismatch"):
            workflow.phase(self.contract, self.save("sess-mismatch-end.json", end),
                           str(self.run / "telemetry/sess-mismatch-end.json"))

    def test_phase_from_flags_autofills_identity_and_time(self):
        out = self.run / "telemetry/flags-1-start.json"
        with patch.dict(os.environ, {"CODEX_SESSION_ID": "flags-session", "CODEX_THREAD_ID": ""}):
            workflow.phase_from_flags(self.contract, str(out), phase_id="flags-1",
                                      phase="implement", event="phase-start", repo="api")
        record = state.load(out)
        self.assertEqual("run-1", record["run_id"])
        self.assertEqual("fix", record["workflow"])
        self.assertEqual("flags-session", record["session_id"])
        self.assertEqual("api", record["repo"])
        self.assertEqual("phase-start", record["event"])
        self.assertTrue(record["at"].endswith("+00:00"))
        self.assertEqual(2, record["schema_version"])

    def test_phase_cli_accepts_flags(self):
        out = self.run / "telemetry/cli-phase-start.json"
        env = {**os.environ, "CODEX_SESSION_ID": "cli-session", "CODEX_THREAD_ID": ""}
        result = subprocess.run(
            ["python3", "-I", "-B", str(SCRIPT), "phase", "--contract", self.contract,
             "--repo", "api", "--phase-id", "cli-phase", "--phase", "review",
             "--event", "phase-start", "--out", str(out)],
            capture_output=True, text=True, env=env)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("cli-session", state.load(out)["session_id"])

    def test_phase_from_flags_end_without_start_rejected(self):
        out = self.run / "telemetry/no-start-end.json"
        with self.assertRaises((ValueError, FileNotFoundError)):
            workflow.phase_from_flags(self.contract, str(out), phase_id="no-start",
                                      phase="implement", event="phase-end", repo="api",
                                      status="passed", end_reason="done")
        self.assertFalse(out.exists())

    def test_phase_from_flags_invalid_out_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid telemetry output path"):
            workflow.phase_from_flags(self.contract, str(self.run / "wrong.json"),
                                      phase_id="wrong-out", phase="review", event="phase-start",
                                      repo="api")

    def test_phase_from_flags_end_binds_start_sha256(self):
        start = self.run / "telemetry/bound-start.json"
        end = self.run / "telemetry/bound-end.json"
        workflow.phase_from_flags(self.contract, str(start), phase_id="bound", phase="review",
                                  event="phase-start", repo="api", at="2026-10-12T00:00:00Z")
        workflow.phase_from_flags(self.contract, str(end), phase_id="bound", phase="review",
                                  event="phase-end", repo="api", status="passed", end_reason="done",
                                  at="2026-10-12T00:10:00Z")
        self.assertEqual(workflow.sha(str(start)), state.load(end)["start_sha256"])

    def test_digest_empty_run_reports_uninitialized(self):
        result = workflow.digest(self.contract, "api")
        self.assertEqual("ok", result["status"])
        self.assertEqual("uninitialized", result["current_status"])
        self.assertEqual(["initialized"], result["next_transitions"])
        self.assertEqual([], result["remaining_findings"])
        self.assertEqual({"contract": "passed", "history": "passed", "budget": "passed",
                          "findings": "empty", "evidence": "empty"}, result["checks"])
        self.assertIsNone(result["artifacts"]["state"])
        self.assertEqual(0, result["artifacts"]["sealed_count"])

    def test_digest_full_run_revalidates_evidence(self):
        self.ready()
        self.implement()
        self.verify()
        result = workflow.digest(self.contract, "api")
        self.assertEqual("ok", result["status"], result.get("errors"))
        self.assertEqual("verified", result["current_status"])
        self.assertIn("edited-unverified", result["next_transitions"])
        self.assertEqual([], result["remaining_findings"])
        self.assertEqual("passed", result["checks"]["evidence"])
        self.assertEqual("empty", result["checks"]["findings"])
        self.assertTrue(result["artifacts"]["state"])
        self.assertGreater(result["artifacts"]["sealed_count"], 0)

    def test_record_evidence_from_flags_matches_file_mode(self):
        self.ready()
        source = self.capture("flags-source.json")
        log = self.run / "flags.log"
        log.write_text("assert expected output\n")
        out = self.run / "flags-evidence.json"
        command = ["python3", "-I", "-B", str(SCRIPT), "record-evidence",
                   "--contract", self.contract, "--repo", "api",
                   "--claim-id", "env.runtime", "--claim-assertion", "mount inspected",
                   "--kind", "static", "--producer", "orchestrator", "--status", "passed",
                   "--assertion", "runtime mount inspected",
                   "--environment", "fixture", "--data-scope", "null",
                   "--before-manifest", source, "--after-manifest", source,
                   "--artifact", str(log), "--out", str(out)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        record = state.load(out)
        self.assertEqual("env.runtime", record["criterion_id"])
        self.assertEqual([{"id": "env.runtime", "assertion": "mount inspected"}], record["claims"])
        self.assertIn(str(log), record["artifact_hashes"])

    def add_second_repo(self, name="web"):
        other = self.root / name
        other.mkdir()
        self.c["repos"].append(name)
        self.c["repo_paths"][name] = str(other)
        for group in ("required_gates", "pre_review_checks", "preflight_checks"):
            self.c[group][name] = copy.deepcopy(self.c[group]["api"])
        self.c["required_passes"][name] = ["logic"]
        self.contract = self.save("contract.json", self.c)

    def batch_entry(self, name, identifier, repo="api"):
        source = self.capture(f"batch-{name}-source.json")
        log = self.run / f"batch-{name}.log"
        log.write_text("assert observable output\n")
        return {"repo": repo, "out": str(self.run / f"batch-{name}-record.json"),
                "claim_id": identifier, "claim_assertion": "observable output checked",
                "kind": "empirical", "producer": "orchestrator", "status": "passed",
                "command": "acceptance test", "exit_code": 0, "assertion": "runtime inspected",
                "environment": "fixture", "data_scope": "null",
                "before_manifest": source, "after_manifest": source,
                "artifact_paths": [str(log)]}

    def test_digest_all_repos_reports_every_repo(self):
        self.add_second_repo()
        result = workflow.digest_all_repos(self.contract)
        self.assertEqual("ok", result["status"])
        self.assertEqual("run-1", result["run_id"])
        self.assertEqual({"api", "web"}, set(result["repos"]))
        self.assertEqual("uninitialized", result["repos"]["api"]["current_status"])
        self.assertEqual({"contract", "history", "budget", "findings", "evidence"},
                         set(result["repos"]["api"]["checks"]))
        self.assertEqual(2, result["summary"]["total_repos"])
        self.assertEqual(2, result["checks"]["repos_ok"])

    def test_digest_all_repos_still_prints_failed_repo(self):
        self.add_second_repo()
        (self.run / "web").mkdir(exist_ok=True)
        (self.run / "web" / "state-1.json").write_text('{"foo": "bar"}')
        result = workflow.digest_all_repos(self.contract)
        self.assertEqual("failed", result["status"])
        self.assertEqual("ok", result["repos"]["api"]["status"])
        self.assertEqual("failed", result["repos"]["web"]["status"])
        self.assertIn("history", result["repos"]["web"]["errors"])
        self.assertEqual(1, result["summary"]["failed_repos"])

    def test_digest_cli_rejects_repo_and_all_repos(self):
        command = ["python3", "-I", "-B", str(SCRIPT), "digest", "--contract", self.contract,
                   "--repo", "api", "--all-repos"]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(2, result.returncode)

    def test_digest_cli_single_repo_still_works(self):
        command = ["python3", "-I", "-B", str(SCRIPT), "digest", "--contract", self.contract,
                   "--repo", "api"]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("ok", json.loads(result.stdout)["status"])

    def test_record_evidence_batch_writes_all_records(self):
        records = [self.batch_entry("a", "fix.ac-1"), self.batch_entry("b", "gate.tests"),
                   self.batch_entry("c", "early.tests")]
        results, ok = workflow.record_evidence_batch(self.contract, records)
        self.assertTrue(ok)
        self.assertEqual(["ok", "ok", "ok"], [r["status"] for r in results])
        for entry in records:
            self.assertTrue(Path(entry["out"]).is_file())
            self.assertEqual(entry["claim_id"], state.load(entry["out"])["criterion_id"])

    def test_record_evidence_batch_reports_invalid_without_losing_valid(self):
        good_a = self.batch_entry("a", "fix.ac-1")
        bad = {"repo": "unknown", "out": str(self.run / "batch-bad-record.json"),
               "claim_id": "bad", "claim_assertion": "x", "kind": "static", "producer": "orchestrator",
               "status": "passed", "assertion": "x", "environment": "fixture", "data_scope": "null"}
        good_c = self.batch_entry("c", "early.tests")
        results, ok = workflow.record_evidence_batch(self.contract, [good_a, bad, good_c])
        self.assertFalse(ok)
        failed = [r for r in results if r["status"] == "failed"]
        self.assertEqual(1, len(failed))
        self.assertIn("Unknown evidence repo", failed[0]["error"])
        self.assertEqual(bad["out"], failed[0]["path"])
        self.assertTrue(Path(good_a["out"]).is_file())
        self.assertTrue(Path(good_c["out"]).is_file())
        self.assertFalse(Path(bad["out"]).is_file())

    def test_record_evidence_batch_cli_writes_and_returns_error(self):
        good = self.batch_entry("a", "fix.ac-1")
        bad = {"repo": "unknown", "out": str(self.run / "batch-bad-record.json"),
               "claim_id": "bad", "claim_assertion": "x", "kind": "static", "producer": "orchestrator",
               "status": "passed", "assertion": "x", "environment": "fixture", "data_scope": "null"}
        batch = self.run / "batch.json"
        batch.write_text(json.dumps([good, bad]))
        command = ["python3", "-I", "-B", str(SCRIPT), "record-evidence", "--contract", self.contract,
                   "--batch", str(batch)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(1, result.returncode)
        self.assertEqual(["ok", "failed"], [r["status"] for r in json.loads(result.stdout)])
        self.assertTrue(Path(good["out"]).is_file())
        self.assertFalse(Path(bad["out"]).is_file())

    def test_run_local_seals_reject_mutable_documents_outside_run_dir(self):
        value = workflow.contract(self.contract)
        outside = self.root / "stage-status.md"
        outside.write_text("status: implemented\n")
        inside = self.run / "handoff.md"
        inside.write_text("sealed\n")
        workflow.require_run_local(value, self.run, [str(inside), str(self.source)])
        with self.assertRaisesRegex(ValueError, "outside the run directory"):
            workflow.require_run_local(value, self.run, [str(outside)])
        self.c["telemetry_policy"] = {"repair_phase_required": False, "run_local_seals": True}
        self.save("contract.json", self.c)
        self.ready()
        self.implement()
        self.verify()
        workflow.complete(self.contract, str(self.run / "completion.json"))

    def slice_item(self, identifier, scope, **kwargs):
        item = {"id": identifier, "title": identifier, "contributions": [{"criterion_id": "fix.ac-1", "part": "part"}],
                "write_scope": scope, "depends_on": [], "interfaces": [], "targeted_checks": [],
                "resources": [], "parallel_allowed": True}
        item.update(kwargs)
        return item

    def schedule(self, items, cross=None, **kwargs):
        data = {"schema_version": 2, "run_id": "run-1", "repo": "api", "approved": True, "approved_by": "user",
                "approved_at": "2026-10-16T00:00:00Z", "slices": items, "cross_slice_criteria": cross or [],
                **kwargs}
        path = self.save("slices-approved.json", data)
        self.c["slice_schedules"] = {"api": {"path": path, "sha256": workflow.sha(path)}}
        self.save("contract.json", self.c)
        return path

    def test_slices_group_by_dependencies_and_never_parallelize_overlaps(self):
        items = [self.slice_item("a", ["app/A"]), self.slice_item("b", ["app/B"]),
                 self.slice_item("c", ["app/**"]), self.slice_item("d", ["app/D"], depends_on=["a"])]
        path = self.schedule(items, cross=["fix.ac-1"], max_parallel=3)
        result = workflow.slice_schedule(self.contract, "api", path)
        self.assertEqual(result["groups"], [["a", "b"], ["c"], ["d"]])
        # default concurrency is serial even when every slice allows parallelism
        path = self.schedule(items, cross=["fix.ac-1"])
        self.assertTrue(all(len(g) == 1 for g in workflow.slice_schedule(self.contract, "api", path)["groups"]))

    def test_slice_phase_is_bound_to_approved_schedule(self):
        self.schedule([self.slice_item("a", ["app/A"])])
        self.enable_policy()
        start = self.run / "telemetry/slice-repair-start.json"
        end = self.run / "telemetry/slice-repair-end.json"
        fields = dict(phase_id="slice-repair", phase="review-fix", repo="api", slice_id="a")
        workflow.phase_from_flags(self.contract, str(start), event="phase-start", **fields)
        with self.assertRaisesRegex(ValueError, "Unknown telemetry slice"):
            workflow.phase_from_flags(self.contract, str(end), event="phase-end", status="passed",
                                      **{**fields, "slice_id": "unknown"})
        workflow.phase_from_flags(self.contract, str(end), event="phase-end", status="passed", **fields)

    def test_slice_resources_and_parallel_flag_block_batching(self):
        items = [self.slice_item("a", ["app/A"], resources=["scratch-db"]),
                 self.slice_item("b", ["app/B"], resources=["scratch-db"]),
                 self.slice_item("c", ["app/C"], parallel_allowed=False)]
        path = self.schedule(items, cross=["fix.ac-1"], max_parallel=3)
        self.assertEqual(workflow.slice_schedule(self.contract, "api", path)["groups"], [["a"], ["b"], ["c"]])

    def test_slice_schedule_rejects_unsafe_proposals(self):
        one = [self.slice_item("a", ["app/A"])]
        cases = {
            "cycle": ([self.slice_item("a", ["x"], depends_on=["b"]), self.slice_item("b", ["y"], depends_on=["a"])],
                      ["fix.ac-1"], "cycle"),
            "unknown dep": ([self.slice_item("a", ["x"], depends_on=["zzz"])], [], "invalid depends_on"),
            "self dep": ([self.slice_item("a", ["x"], depends_on=["a"])], [], "invalid depends_on"),
            "cross mismatch": ([self.slice_item("a", ["x"]), self.slice_item("b", ["y"])], [], "cross_slice_criteria"),
            "bad criterion": ([self.slice_item("a", ["x"], contributions=[{"criterion_id": "nope", "part": "p"}])],
                              [], "unknown criterion"),
            "absolute scope": ([self.slice_item("a", ["/etc"])], [], ""),
        }
        for name, (items, cross, message) in cases.items():
            with self.subTest(name):
                path = self.schedule(items, cross=cross)
                with self.assertRaisesRegex(ValueError, message):
                    workflow.slice_schedule(self.contract, "api", path)
        path = self.schedule(one)
        self.assertEqual(workflow.slice_schedule(self.contract, "api", path)["groups"], [["a"]])

    def test_slice_schedule_needs_approval_hash_and_full_criterion_coverage(self):
        path = self.schedule([self.slice_item("a", ["app/A"])])
        schedule = state.load(path)
        schedule["approved"] = False
        Path(path).write_text(json.dumps(schedule))
        with self.assertRaisesRegex(ValueError, "not explicitly approved"):
            workflow.slice_schedule(self.contract, "api", path)
        path = self.schedule([self.slice_item("a", ["app/A"])])
        Path(path).write_text(Path(path).read_text() + " ")  # altered after approval
        with self.assertRaisesRegex(ValueError, "declared in the contract with its hash"):
            workflow.slice_schedule(self.contract, "api", path)
        self.c["criteria"].append({**self.c["criteria"][0], "id": "fix.ac-2"})
        path = self.schedule([self.slice_item("a", ["app/A"])])
        with self.assertRaisesRegex(ValueError, "without any slice contribution: fix.ac-2"):
            workflow.slice_schedule(self.contract, "api", path)

    def test_argv_check_is_quoted_once_and_matches_real_exit_status(self):
        argv = ["sh", "-c", "find . -name '*.php' | grep -q nomatch; test $? -ne 0"]
        self.c["pre_review_checks"]["api"][0] = {"id": "early.tests", "kind": "empirical", "argv": argv,
                                                 "verification": "no match"}
        self.save("contract.json", self.c)
        entry = workflow.spec(workflow.contract(self.contract), "api", "early.tests")
        self.assertEqual(workflow.check_command(entry), __import__("shlex").join(argv))
        # The expanded $? must be evaluated by the inner shell, not by an outer wrapper.
        done = subprocess.run(entry["argv"], cwd=self.repo)
        self.assertEqual(done.returncode, 0)
        self.c["pre_review_checks"]["api"][0]["command"] = "x"
        self.save("contract.json", self.c)
        with self.assertRaisesRegex(ValueError, "exactly one of command or argv"):
            workflow.contract(self.contract)

    def test_reproduced_bug_requires_initial_failure_and_same_after_scenario(self):
        before = self.evidence("fix.ac-1", status="failed", source=self.initial)
        self.c["acceptance"].update(mode="reproduced-bug", before_evidence={"api": before})
        self.save("contract.json", self.c)
        self.ready()
        self.implement()
        self.verify()
        workflow.complete(self.contract, str(self.run / "completion.json"))

    def test_code_only_cannot_become_verified(self):
        self.move("initialized")
        pre = self.save("preflight.json", self.artifact(status="blocked", reason="DB unavailable"))
        self.move("preflight-blocked", preflight_path=pre, reason="DB unavailable")
        auth = self.save("authorization.json", self.artifact(user_authorization="User explicitly permits code-only scope"))
        matrix = self.save("matrix.json", self.artifact(criteria=[{
            "criterion_id": "fix.ac-1", "implementation": "source.txt", "assertion": "original scenario",
            "status": "unverified", "reason": "DB unavailable", "evidence_paths": []}]))
        self.source.write_text("code-only")
        self.move("implemented", matrix_path=matrix, code_only_authorization=auth)
        self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])
        self.move("reviewed", review_path=self.review())
        self.move("gates-passed", gate_evidence_paths=[self.evidence("gate.tests")])
        with self.assertRaisesRegex(ValueError, "Code-only"):
            self.move("verified", assessment_path="missing")

    def test_user_budget_grants_reject_premature_and_invalid_requests(self):
        from datetime import datetime, timezone
        self.ready()
        self.implement()
        finding = {"id": "api-1", "file": "source.txt", "line": 1, "severity": "Warning",
                   "status": "new", "category": "Logic", "description": "wrong output",
                   "recommendation": "fix result", "failure_scenario": "null returns wrong result",
                   "verification_expectation": "assert null output", "criterion_ids": ["fix.ac-1"],
                   "verification_kind": "static"}
        review_path = self.review([finding])
        self.move("needs-fix", review_path=review_path)

        def grant(name, previous_path, additional=1):
            grant_path = self.run / "budget-grants" / f"{name}.json"
            grant_path.parent.mkdir(parents=True, exist_ok=True)
            grant_path.write_text(json.dumps(self.artifact(
                workflow="fix", contract_sha256=workflow.sha(self.contract), additional_passes=additional,
                purpose="review-fix", reason=f"Continue {name}", approved_by="user",
                user_authorization=f"Grant {additional} pass", approval_question=f"Grant {additional}?",
                approved_at=datetime.now(timezone.utc).isoformat(), previous_state_path=str(previous_path),
                previous_state_sha256=workflow.sha(previous_path), review_path=review_path,
                review_sha256=workflow.sha(review_path), review_round=1, finding_ids=["api-1"], criterion_ids=[])))
            return str(grant_path)

        first_previous = workflow.history(self.run / "api")[-1][1]
        first_grant = grant("one", first_previous)
        with self.assertRaisesRegex(ValueError, "not exhausted"):
            self.move("needs-fix", budget_grant_path=first_grant)
        for index, invalid_count in enumerate((0, -1, 1.5, True)):
            invalid_grant = grant(f"invalid-{index}", first_previous, invalid_count)
            with self.subTest(additional_passes=invalid_count), self.assertRaisesRegex(
                    ValueError, "positive integer"):
                self.move("needs-fix", budget_grant_path=invalid_grant)
        with self.assertRaisesRegex(ValueError, "Request overrides"):
            self.move("needs-fix", budget_grant_path=first_grant, review_fix_limit=99)
        self.assertEqual(3, workflow.review_budget(
            workflow.history(self.run / "api"), self.contract, self.c, "api")[0])

    def test_review_budget_precheck_is_read_only_and_reports_open_findings(self):
        self.ready()
        self.implement()
        finding = {"id": "api-1", "file": "source.txt", "line": 1, "severity": "Warning",
                   "status": "new", "category": "Logic", "description": "wrong output",
                   "recommendation": "fix result", "failure_scenario": "null returns wrong result",
                   "verification_expectation": "assert null output", "criterion_ids": ["fix.ac-1"],
                   "verification_kind": "static"}
        for number in range(3):
            self.move("reviewed", review_path=self.review())
            self.source.write_text(f"plan-gap-{number}\n")
            self.move("edited-unverified", action="plan-gap")
            self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])
        latest_review = self.review([finding])
        self.move("needs-fix", review_path=latest_review)
        states_before = [path.read_bytes() for path in sorted((self.run / "api").glob("state-*.json"))]
        status = workflow.review_budget_status(self.contract, "api")
        self.assertEqual((3, 3, 0, True), (status["limit"], status["used_passes"],
                                           status["remaining_passes"], status["exhausted"]))
        self.assertEqual(["api-1"], status["open_finding_ids"])
        self.assertEqual([finding], status["open_findings"])
        self.assertEqual({"Critical": 0, "Warning": 1, "Suggestion": 0}, status["findings_by_severity"])
        self.assertTrue(status["source_matches_state"])
        with self.assertRaisesRegex(ValueError, "Review-fix budget exhausted"):
            self.move("edited-unverified", action="review-fix")
        self.assertEqual(states_before,
                         [path.read_bytes() for path in sorted((self.run / "api").glob("state-*.json"))])
        self.move("capped", reason="review-fix budget exhausted")
        capped = workflow.review_budget_status(self.contract, "api")
        self.assertEqual(("capped", ["api-1"]),
                         (capped["latest_status"], capped["open_finding_ids"]))
        from datetime import datetime, timezone
        previous = workflow.history(self.run / "api")[-1]
        decision = self.artifact(workflow="fix", approved_by="user", approval_question="Accept findings?",
                                 user_authorization="Accept", approved_at=datetime.now(timezone.utc).isoformat(),
                                 contract_sha256=workflow.sha(self.contract), previous_state_path=str(previous[1]),
                                 previous_state_sha256=workflow.sha(previous[1]), review_path=latest_review,
                                 review_sha256=workflow.sha(latest_review), review_round=4,
                                 finding_ids=["api-1"], source_fingerprint=previous[2]["source_fingerprint"])
        invalid = self.save("invalid-acceptance.json", {**decision, "finding_ids": []})
        with self.assertRaisesRegex(ValueError, "finding scope"):
            self.move("reviewed-with-findings", review_acceptance_path=invalid)
        for index, changes in enumerate(({"approved_by": "reviewer"}, {"previous_state_sha256": "bad"},
                                         {"review_sha256": "bad"}, {"repo": "other"})):
            invalid = self.save(f"invalid-acceptance-{index}.json", {**decision, **changes})
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.move("reviewed-with-findings", review_acceptance_path=invalid)
        accepted = self.save("acceptance.json", decision)
        original_source = self.source.read_text()
        self.source.write_text("unapproved change")
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.move("reviewed-with-findings", review_acceptance_path=accepted)
        self.source.write_text(original_source)
        result = self.move("reviewed-with-findings", review_acceptance_path=accepted)
        self.assertEqual(accepted, result["review_acceptance_path"])
        self.assertEqual(4, result["latest_review_round"])
        self.assertEqual("valid", workflow.validate_history(self.contract, "api")["status"])
        failed_gate = self.evidence("gate.tests", status="failed")
        with self.assertRaisesRegex(ValueError, "did not pass"):
            self.move("gates-passed", gate_evidence_paths=[failed_gate])
        with self.assertRaisesRegex(ValueError, "Repo not verified"):
            workflow.complete(self.contract, str(self.run / "completion.json"))
        self.move("gates-passed", gate_evidence_paths=[self.evidence("gate.tests")])
        failed_assessment = self.save("failed-assessment.json", self.artifact(producer="orchestrator", requirements_match=True,
            criteria=[{"criterion_id": "fix.ac-1", "implementation": "partial", "verification": "unverified",
                       "reason": "original scenario remains unverified", "evidence_paths": []}]))
        with self.assertRaises(ValueError):
            self.move("verified", assessment_path=failed_assessment)
        with self.assertRaisesRegex(ValueError, "Repo not verified"):
            workflow.complete(self.contract, str(self.run / "completion.json"))
        assessment = self.save("accepted-assessment.json", self.artifact(producer="orchestrator", requirements_match=True,
            criteria=[{"criterion_id": "fix.ac-1", "implementation": "met", "verification": "verified",
                       "reason": "original scenario verified", "evidence_paths": [self.evidence("fix.ac-1")]}]))
        self.move("verified", assessment_path=assessment)
        workflow.complete(self.contract, str(self.run / "completion.json"))
        recorded = state.load(self.run / "completion.json")["repos"]["api"]["open_findings"]
        self.assertEqual(["api-1"], [item["id"] for item in recorded])
        self.assertEqual(accepted, recorded[0]["review_acceptance_path"])

    def test_review_fix_grant_binds_unchanged_source_and_precedes_repair(self):
        from datetime import datetime, timezone
        self.ready()
        self.implement()
        finding = {"id": "api-1", "file": "source.txt", "line": 1, "severity": "Warning",
                   "status": "new", "category": "Logic", "description": "wrong output",
                   "recommendation": "fix result", "failure_scenario": "null returns wrong result",
                   "verification_expectation": "assert null output", "criterion_ids": ["fix.ac-1"],
                   "verification_kind": "static"}
        for number in range(3):
            self.move("reviewed", review_path=self.review())
            self.source.write_text(f"plan-gap-{number}\n")
            self.move("edited-unverified", action="plan-gap")
            self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])
        review_path = self.review([finding])
        self.move("needs-fix", review_path=review_path)
        previous = workflow.history(self.run / "api")[-1][1]
        grant_path = self.run / "budget-grants/review-fix.json"
        grant_path.parent.mkdir(parents=True, exist_ok=True)
        grant_path.write_text(json.dumps(self.artifact(
            workflow="fix", contract_sha256=workflow.sha(self.contract), additional_passes=2,
            purpose="review-fix", reason="Resolve remaining finding", approved_by="user",
            user_authorization="Grant two additional review-fix passes", approval_question="Grant +2 or stop?",
            approved_at=datetime.now(timezone.utc).isoformat(), previous_state_path=str(previous),
            previous_state_sha256=workflow.sha(previous), review_path=review_path,
            review_sha256=workflow.sha(review_path), review_round=workflow.history(self.run / "api")[-1][2]["latest_review_round"],
            finding_ids=["api-1"], criterion_ids=[])))
        self.source.write_text("unauthorized edit before grant\n")
        self.assertFalse(workflow.review_budget_status(self.contract, "api")["source_matches_state"])
        with self.assertRaisesRegex(ValueError, "Grant cannot accompany source edits"):
            self.move("needs-fix", budget_grant_path=str(grant_path))
        self.source.write_text("plan-gap-2\n")
        approved = self.move("needs-fix", budget_grant_path=str(grant_path))
        self.assertTrue(approved["budget_grant_applied"])
        self.assertEqual((3, 5), (approved["review_fix_count"], approved["review_fix_limit"]))
        self.assertEqual("needs-fix", approved["status"])
        self.source.write_text("approved review repair\n")
        proof_path = self.evidence("fix.ac-1", kind="static")
        proof_record = state.load(proof_path)
        proof_record["claims"] = [{"id": "api-1", "assertion": "finding is resolved"}]
        Path(proof_path).write_text(json.dumps(proof_record))
        applied_path = self.save("review-fix-applied.json", self.artifact(
            round=approved["latest_review_round"], applied=[{"id": "api-1", "verified": "output checked",
            "verification_kind": "static", "evidence_paths": [proof_path]}], failed=[], skipped=[]))
        repaired = self.move("edited-unverified", action="review-fix", applied_path=applied_path)
        self.assertEqual((4, 5), (repaired["review_fix_count"], repaired["review_fix_limit"]))

    def test_extra_review_pass_then_accept_remaining_findings(self):
        from datetime import datetime, timezone
        self.ready()
        self.implement()
        for number in range(3):
            self.move("reviewed", review_path=self.review())
            self.source.write_text(f"plan-gap-{number}\n")
            self.move("edited-unverified", action="plan-gap")
            self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])
        finding = {"id": "api-1", "file": "source.txt", "line": 1, "severity": "Warning",
                   "status": "new", "category": "Logic", "description": "wrong output",
                   "recommendation": "fix result", "failure_scenario": "null returns wrong result",
                   "verification_expectation": "assert null output", "criterion_ids": ["fix.ac-1"],
                   "verification_kind": "static"}
        review = self.review([finding])
        self.move("needs-fix", review_path=review)
        self.move("capped", reason="budget exhausted")
        previous = workflow.history(self.run / "api")[-1]
        grant = self.save("budget-grants/extra.json", self.artifact(
            workflow=self.c["workflow"], contract_sha256=workflow.sha(self.contract), additional_passes=1,
            purpose="review-fix", reason="One more attempt", approved_by="user",
            user_authorization="Grant one pass", approval_question="Grant one pass?",
            approved_at=datetime.now(timezone.utc).isoformat(), previous_state_path=str(previous[1]),
            previous_state_sha256=workflow.sha(previous[1]), review_path=review,
            review_sha256=workflow.sha(review), review_round=4, finding_ids=["api-1"], criterion_ids=[]))
        granted = self.move("needs-fix", budget_grant_path=grant)
        self.assertEqual((3, 4), (granted["review_fix_count"], granted["review_fix_limit"]))
        applied = self.save("extra-applied.json", self.artifact(round=4, applied=[],
            failed=[{"id": "api-1", "reason": "Repair did not resolve the finding"}], skipped=[]))
        self.source.write_text("attempted review repair\n")
        self.move("edited-unverified", action="review-fix", applied_path=applied)
        self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])
        new_finding = {**finding, "id": "api-2", "severity": "Critical", "status": "new",
                       "description": "second issue", "failure_scenario": "another input fails"}
        review = self.review([{**finding, "status": "recurring"}, new_finding])
        self.move("needs-fix", review_path=review)
        budget = workflow.review_budget_status(self.contract, "api")
        self.assertEqual((4, 4, True), (budget["used_passes"], budget["limit"], budget["exhausted"]))
        self.assertEqual({"Critical": 1, "Warning": 1, "Suggestion": 0}, budget["findings_by_severity"])
        self.move("capped", reason="additional pass exhausted")
        previous = workflow.history(self.run / "api")[-1]
        decision = self.save("extra-acceptance.json", self.artifact(
            workflow=self.c["workflow"], approved_by="user", approval_question="Accept remaining finding?",
            user_authorization="Accept", approved_at=datetime.now(timezone.utc).isoformat(),
            contract_sha256=workflow.sha(self.contract), previous_state_path=str(previous[1]),
            previous_state_sha256=workflow.sha(previous[1]), review_path=review,
            review_sha256=workflow.sha(review), review_round=5, finding_ids=["api-1", "api-2"],
            source_fingerprint=previous[2]["source_fingerprint"]))
        accepted = self.move("reviewed-with-findings", review_acceptance_path=decision)
        self.assertEqual((4, 4, 5), (accepted["review_fix_count"], accepted["review_fix_limit"],
                                     accepted["latest_review_round"]))
        self.assertEqual("valid", workflow.validate_history(self.contract, "api")["status"])
        self.move("gates-passed", gate_evidence_paths=[self.evidence("gate.tests")])
        assessment = self.save("extra-assessment.json", self.artifact(producer="checker", requirements_match=True,
            criteria=[{"criterion_id": "fix.ac-1", "implementation": "met", "verification": "verified",
                       "reason": "original scenario verified", "evidence_paths": [self.evidence("fix.ac-1")]}]))
        self.move("verified", assessment_path=assessment)
        workflow.complete(self.contract, str(self.run / "completion.json"))
        repo = state.load(self.run / "completion.json")["repos"]["api"]
        self.assertEqual((1, 4, 4), (repo["review_budget"]["granted_additional_passes"],
                                    repo["review_budget"]["used_passes"], repo["review_budget"]["effective_limit"]))
        self.assertEqual([("api-1", decision), ("api-2", decision)], [
            (item["id"], item["review_acceptance_path"]) for item in repo["open_findings"]])

    def test_ship_extra_review_pass_then_accept_remaining_findings(self):
        plan = self.root / "plan.md"
        plan.write_text("approved requirement")
        snapshot = self.run / "approved-plan.md"
        snapshot.write_bytes(plan.read_bytes())
        self.c.update(workflow="ship", plan_path=str(plan), approved_snapshot=str(snapshot),
                      plan_sha256=workflow.sha(plan))
        self.save("contract.json", self.c)
        self.test_extra_review_pass_then_accept_remaining_findings()

    def test_budget_grant_carries_validated_usage_from_prior_runs(self):
        from datetime import datetime, timezone
        self.ready()
        self.implement()
        finding = {"id": "api-1", "file": "source.txt", "line": 1, "severity": "Warning",
                   "status": "new", "category": "Logic", "description": "wrong output",
                   "recommendation": "fix result", "failure_scenario": "null returns wrong result",
                   "verification_expectation": "assert null output", "criterion_ids": ["fix.ac-1"],
                   "verification_kind": "static"}
        review_path = self.review([finding])
        self.move("needs-fix", review_path=review_path)
        previous = workflow.history(self.run / "api")[-1][1]
        prior_sources = []
        expected = {}
        for run_id, used in (("older-run", 1), ("successor-run", 2)):
            historical = self.run / "historical" / run_id
            historical.mkdir(parents=True, exist_ok=True)
            contract_path = historical / "contract.json"
            contract_path.write_text(json.dumps({"workflow": "fix", "run_id": run_id, "repos": ["api"]}))
            state_path = historical / "api" / "state-1.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"run_id": run_id, "review_fix_count": used}))
            source = {"run_id": run_id, "contract_path": str(contract_path),
                      "contract_sha256": workflow.sha(contract_path), "state_path": str(state_path),
                      "state_sha256": workflow.sha(state_path), "used_passes": used, "effective_limit": 3}
            prior_sources.append(source)
            expected[str(contract_path)] = {"run_id": run_id, "repo": "api", "state_path": str(state_path),
                                            "review_fix_count": used, "review_fix_limit": 3}
        grant_path = self.run / "budget-grants" / "carryover.json"
        grant_path.parent.mkdir(parents=True, exist_ok=True)
        grant_path.write_text(json.dumps(self.artifact(
            workflow="fix", contract_sha256=workflow.sha(self.contract), additional_passes=1,
            purpose="review-fix", reason="Carry historical review usage forward",
            approved_by="user", user_authorization="Grant one additional pass",
            approval_question="Grant one pass?", approved_at=datetime.now(timezone.utc).isoformat(),
            previous_state_path=str(previous), previous_state_sha256=workflow.sha(previous),
            review_path=review_path, review_sha256=workflow.sha(review_path), review_round=1,
            finding_ids=["api-1"], criterion_ids=[], historical_used_passes=3,
            historical_budget_sources=prior_sources)))
        with patch.object(workflow, "validate_history", side_effect=lambda path, repo, *args: expected[str(Path(path))]):
            approved = self.move("needs-fix", budget_grant_path=str(grant_path))
            self.assertEqual((0, 1), (approved["review_fix_count"], approved["review_fix_limit"]))
            status = workflow.review_budget_status(self.contract, "api")
            self.assertEqual((1, 3, 1, True), (status["limit"], status["historical_used_passes"],
                                               status["remaining_passes"], status["source_matches_state"]))
            self.assertEqual(3, workflow.review_budget(
                workflow.history(self.run / "api"), self.contract, self.c, "api")[1][0]["historical_used_passes"])
            self.source.write_text("first authorized repair\n")
            proof_path = self.evidence("fix.ac-1", kind="static")
            proof_record = state.load(proof_path)
            proof_record["claims"] = [{"id": "api-1", "assertion": "finding repair checked"}]
            Path(proof_path).write_text(json.dumps(proof_record))
            applied_path = self.save("carryover-applied.json", self.artifact(
                round=1, applied=[{"id": "api-1", "verified": "source inspected",
                "verification_kind": "static", "evidence_paths": [proof_path]}], failed=[], skipped=[]))
            self.move("edited-unverified", action="review-fix", applied_path=applied_path)
            self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])
            next_review = self.review([{**finding, "status": "recurring"}])
            self.move("needs-fix", review_path=next_review)
            next_previous = workflow.history(self.run / "api")[-1][1]
            next_grant = self.run / "budget-grants" / "carryover-second.json"
            next_grant.write_text(json.dumps(self.artifact(
                workflow="fix", contract_sha256=workflow.sha(self.contract), additional_passes=1,
                purpose="review-fix", reason="One more bounded repair after carryover is consumed",
                approved_by="user", user_authorization="Grant one further pass",
                approval_question="Grant one further pass?", approved_at=datetime.now(timezone.utc).isoformat(),
                previous_state_path=str(next_previous), previous_state_sha256=workflow.sha(next_previous),
                review_path=next_review, review_sha256=workflow.sha(next_review), review_round=2,
                finding_ids=["api-1"], criterion_ids=[])))
            with patch.object(workflow, "validate_history", side_effect=lambda path, repo, *args: expected[str(Path(path))]):
                second = self.move("needs-fix", budget_grant_path=str(next_grant))
                second_status = workflow.review_budget_status(self.contract, "api")
            self.assertEqual((1, 2), (second["review_fix_count"], second["review_fix_limit"]))
            self.assertEqual((2, 1, 4), (second_status["limit"], second_status["remaining_passes"],
                                       second_status["cumulative_used_passes"]))

    def test_historical_budget_source_rejects_changed_state_hash(self):
        hist = self.run / "historical" / "run-a"
        hist.mkdir(parents=True)
        contract_path = hist / "contract.json"
        contract_path.write_text(json.dumps({"workflow": "fix", "run_id": "run-a", "repos": ["api"]}))
        state_path = hist / "api" / "state-1.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text("{}")
        source = {"run_id": "run-a", "contract_path": str(contract_path),
                  "contract_sha256": workflow.sha(contract_path), "state_path": str(state_path),
                  "state_sha256": workflow.sha(state_path), "used_passes": 1, "effective_limit": 3}
        state_path.write_text("{\"changed\":true}")
        with patch.object(workflow, "validate_history", return_value={
                "run_id": "run-a", "state_path": str(state_path), "review_fix_count": 1,
                "review_fix_limit": 3}):
            with self.assertRaisesRegex(ValueError, "Historical budget state hash/path mismatch"):
                workflow.historical_budget_sources([source], "api", "fix", self.contract)

    def test_historical_history_rejects_forged_counters_and_cycles(self):
        api_dir = self.run / "api"
        api_dir.mkdir(parents=True, exist_ok=True)
        forged = {"run_id": "run-1", "repo": "api", "sequence": 1,
                  "status": "initialized", "review_fix_count": 3, "gate_fix_count": 0,
                  "latest_review_round": 0, "review_fix_limit": 3, "artifact_hashes": {}}
        (api_dir / "state-1.json").write_text(json.dumps(forged))
        with self.assertRaisesRegex(ValueError, "not derived from repair events"):
            workflow.validate_history(self.contract, "api")
        (api_dir / "state-1.json").unlink()
        with self.assertRaisesRegex(ValueError, "reference cycle/current-run ancestry"):
            workflow.validate_history(self.contract, "api", {Path(self.contract).resolve()})

    def test_plan_gap_repair_uses_user_extended_repo_budget(self):
        from datetime import datetime, timezone
        self.ready()
        self.implement()
        self.move("reviewed", review_path=self.review())
        for number in range(1, 4):
            self.source.write_text(f"plan-gap-{number}\n")
            event = self.move("edited-unverified", action="plan-gap")
            self.assertEqual(number, event["review_fix_count"])
            self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])
            self.move("reviewed", review_path=self.review())
        self.move("capped", reason="standard review-fix budget exhausted")
        previous = workflow.history(self.run / "api")[-1][1]
        review_path = workflow.history(self.run / "api")[-1][2]["review_path"]
        grant_path = self.run / "budget-grants" / "plan-gap.json"
        grant_path.parent.mkdir(parents=True, exist_ok=True)
        grant_path.write_text(json.dumps(self.artifact(
            workflow="fix", contract_sha256=workflow.sha(self.contract), additional_passes=1,
            purpose="plan-gap", reason="Close remaining acceptance gap", approved_by="user",
            user_authorization="Grant one additional pass", approval_question="Grant +1 or stop?",
            approved_at=datetime.now(timezone.utc).isoformat(), previous_state_path=str(previous),
            previous_state_sha256=workflow.sha(previous), review_path=review_path,
            review_sha256=workflow.sha(review_path), review_round=4, finding_ids=[],
            criterion_ids=["fix.ac-1"])))
        approved = self.move("reviewed", budget_grant_path=str(grant_path))
        self.assertEqual(4, approved["review_fix_limit"])
        self.assertEqual("valid", workflow.validate_history(self.contract, "api")["status"])
        original_grant = grant_path.read_bytes()
        grant_path.write_text(original_grant.decode() + " ")
        with self.assertRaisesRegex(ValueError, "grant seal mismatch"):
            workflow.review_budget(workflow.history(self.run / "api"), self.contract, self.c, "api")
        grant_path.write_bytes(original_grant)
        self.source.write_text("plan-gap-4\n")
        fourth = self.move("edited-unverified", action="plan-gap")
        self.assertEqual(4, fourth["review_fix_count"])
        self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])
        self.move("reviewed", review_path=self.review())
        previous = workflow.history(self.run / "api")[-1][1]
        review_path = workflow.history(self.run / "api")[-1][2]["review_path"]
        second_grant = self.run / "budget-grants" / "plan-gap-second.json"
        second_grant.write_text(json.dumps(self.artifact(
            workflow="fix", contract_sha256=workflow.sha(self.contract), additional_passes=1,
            purpose="plan-gap", reason="One more independently reviewed acceptance repair", approved_by="user",
            user_authorization="Grant one additional pass again", approval_question="Grant another +1?",
            approved_at=datetime.now(timezone.utc).isoformat(), previous_state_path=str(previous),
            previous_state_sha256=workflow.sha(previous), review_path=review_path,
            review_sha256=workflow.sha(review_path), review_round=5, finding_ids=[],
            criterion_ids=["fix.ac-1"])))
        approved_again = self.move("reviewed", budget_grant_path=str(second_grant))
        self.assertEqual(5, approved_again["review_fix_limit"])
        with self.assertRaisesRegex(ValueError, "bound to another state"):
            self.move("reviewed", budget_grant_path=str(grant_path))
        self.source.write_text("plan-gap-5\n")
        fifth = self.move("edited-unverified", action="plan-gap")
        self.assertEqual(5, fifth["review_fix_count"])
        self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])
        self.move("reviewed", review_path=self.review())
        accepted_source = self.source.read_text()
        self.source.write_text("plan-gap-6\n")
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            self.move("edited-unverified", action="plan-gap")
        self.source.write_text(accepted_source)
        self.move("gates-passed", gate_evidence_paths=[self.evidence("gate.tests")])
        assessment = self.save("grant-assessment.json", self.artifact(
            producer="orchestrator", requirements_match=True, criteria=[{
                "criterion_id": "fix.ac-1", "implementation": "met", "verification": "verified",
                "reason": "the acceptance assertion passed", "evidence_paths": [self.evidence("fix.ac-1")]}]))
        self.move("verified", assessment_path=assessment)
        workflow.complete(self.contract, str(self.run / "completion.json"))
        budget = state.load(self.run / "completion.json")["repos"]["api"]["review_budget"]
        self.assertEqual((5, 5, 2, 2), (budget["effective_limit"], budget["used_passes"],
                                       budget["granted_additional_passes"], budget["extra_passes_used"]))
        self.assertEqual(["Close remaining acceptance gap",
                          "One more independently reviewed acceptance repair"],
                         [grant["reason"] for grant in budget["grants"]])

    def test_gate_fix_budget_never_resets_after_review(self):
        self.ready()
        self.implement()
        for number in (1, 2):
            self.move("reviewed", review_path=self.review())
            failed = self.evidence("gate.tests", status="failed")
            self.source.write_text(f"gate fix {number}")
            event = self.move("edited-unverified", action="gate-fix", gate_failure_path=failed, gate_fix_count=0)
            self.assertEqual(number, event["gate_fix_count"])
            self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])
        self.move("reviewed", review_path=self.review())
        failed = self.evidence("gate.tests", status="failed")
        self.source.write_text("third fix")
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            self.move("edited-unverified", action="gate-fix", gate_failure_path=failed)

    def test_plan_gap_budget_and_cap_cannot_reset(self):
        self.ready()
        self.implement()
        for number in (1, 2, 3):
            self.move("reviewed", review_path=self.review())
            self.source.write_text(f"gap {number}")
            event = self.move("edited-unverified", action="plan-gap", review_fix_count=0)
            self.assertEqual(number, event["review_fix_count"])
            self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])
        self.move("reviewed", review_path=self.review())
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            self.move("edited-unverified", action="plan-gap")
        self.move("capped", reason="repair budget exhausted")
        with self.assertRaises(ValueError):
            self.move("ready", review_fix_count=0)

    def test_cross_repo_evidence_invalidated_by_producer_change(self):
        producer = self.root / "producer"
        producer.mkdir()
        source = producer / "producer.txt"
        source.write_text("api-v1")
        manifest = self.save("producer-source.json", state.capture(producer, ["producer.txt"]))
        self.c["repos"].append("sdk")
        self.c["repos"] = ["sdk", "api"]
        self.c["repo_paths"]["sdk"] = str(producer)
        self.c["criteria"][0]["repos"].append("sdk")
        self.c["criteria"][0]["assessment_repo"] = "api"
        p = self.evidence("fix.ac-1", source=self.initial)
        record = state.load(p)
        with self.assertRaisesRegex(ValueError, "Missing cross-repo source binding"):
            workflow.proof(self.c, "api", p, state.checked_manifest(self.initial), "fix.ac-1")
        record["dependency_manifests"] = {"sdk": manifest}
        self.save(Path(p).name, record)
        workflow.proof(self.c, "api", p, state.checked_manifest(self.initial), "fix.ac-1")
        source.write_text("api-v2")
        with self.assertRaisesRegex(ValueError, "Stale"):
            workflow.proof(self.c, "api", p, state.checked_manifest(self.initial), "fix.ac-1")

    def test_claimed_verified_matrix_requires_current_proof(self):
        self.ready()
        self.source.write_text("after")
        p = self.evidence("fix.ac-1", source=self.initial)
        m = self.save("matrix.json", self.artifact(criteria=[{
            "criterion_id": "fix.ac-1", "implementation": "source.txt", "assertion": "null result",
            "status": "verified", "reason": "claimed proof", "evidence_paths": [p]}]))
        with self.assertRaisesRegex(ValueError, "Stale"):
            self.move("implemented", matrix_path=m)

    def test_unverified_matrix_still_checks_supplied_evidence(self):
        evidence = self.evidence("fix.ac-1")
        record = state.load(evidence)
        record["data_scope"] = "wrong scope"
        self.save(Path(evidence).name, record)
        matrix_path = self.save("unverified-matrix.json", self.artifact(criteria=[{
            "criterion_id": "fix.ac-1", "implementation": "source.txt", "assertion": "result",
            "status": "unverified", "reason": "pending", "evidence_paths": [evidence]}]))
        with self.assertRaisesRegex(ValueError, "Wrong evidence data_scope"):
            workflow.matrix(self.c, "api", matrix_path, state.checked_manifest(self.initial))
        record["data_scope"] = "null"
        self.save(Path(evidence).name, record)
        workflow.matrix(self.c, "api", matrix_path, state.checked_manifest(self.initial))

    def test_deliverable_snapshot_detects_unpublished_or_changed_file(self):
        published = self.root / "report.txt"
        published.write_text("first")
        self.c["deliverables"] = {"api": [{"id": "report", "path": str(published),
                                              "criterion_ids": ["fix.ac-1"]}]}
        self.save("contract.json", self.c)
        manifest = workflow.snapshot_deliverables(self.contract, "api", str(self.run / "snapshots/1"))
        value = workflow.contract(self.contract)
        workflow.validate_deliverables(value, "api", manifest)
        published.write_text("second")
        with self.assertRaisesRegex(ValueError, "differs"):
            workflow.validate_deliverables(value, "api", manifest)
        second = workflow.snapshot_deliverables(self.contract, "api", str(self.run / "snapshots/2"))
        workflow.validate_deliverables(value, "api", second)
        self.assertTrue(Path(manifest).is_file())

    def test_check_input_snapshots_detect_change(self):
        input_file = self.root / "input.txt"
        input_file.write_text("original")
        paths = workflow.snapshot_inputs([str(input_file)], self.run / "input-snapshot", self.repo, [])
        workflow.check_inputs_unchanged(paths)
        input_file.write_text("changed")
        with self.assertRaisesRegex(ValueError, "changed during execution"):
            workflow.check_inputs_unchanged(paths)

    def test_execution_binding_rejects_wrong_producer(self):
        log = self.run / "execution.log"
        log.write_text("ok")
        execution = self.save("execution.json", {"producer": "implementer", "command": "check", "exit_code": 0,
                                                   "log": str(log)})
        record = {"execution_record": execution, "artifact_paths": [execution, str(log)],
                  "producer": "orchestrator", "command": "check", "exit_code": 0}
        with self.assertRaisesRegex(ValueError, "producer"):
            workflow.execution_binding(record)

    def test_submission_collects_multiple_bad_evidence_paths(self):
        matrix_path = self.save("bad-matrix.json", self.artifact(criteria=[{
            "criterion_id": "fix.ac-1", "implementation": "source.txt", "assertion": "result",
            "status": "unverified", "reason": "pending", "evidence_paths": ["relative", "also-relative"]}]))
        errors = workflow.submission_evidence_errors(self.c, "api", matrix_path,
                                                       state.checked_manifest(self.initial), "criteria")
        self.assertEqual(len(errors), 2)

    def test_from_review_requires_pre_review_and_author_handoff(self):
        self.c["from_review"] = True
        self.save("contract.json", self.c)
        self.ready()
        with self.assertRaisesRegex(ValueError, "Illegal transition"):
            self.move("reviewed", review_path=self.review())
        m = self.save("matrix.json", self.artifact(criteria=[{
            "criterion_id": "fix.ac-1", "implementation": "source.txt", "assertion": "original scenario",
            "status": "unverified", "reason": "author submitted code", "evidence_paths": []}]))
        self.move("pre-review-passed", matrix_path=m, pre_review_evidence_paths=[self.evidence("early.tests")])
        self.move("reviewed", review_path=self.review())
        with self.assertRaisesRegex(ValueError, "author handoff"):
            self.move("edited-unverified", action="plan-gap")
        self.move("handoff", reason="author must repair")
        # Handoff preserves review/counters and cannot fabricate completed verification.
        with self.assertRaises(ValueError):
            workflow.complete(self.contract, str(self.run / "completion.json"))

    def test_packet_and_record_evidence_cli(self):
        self.ready()
        self.source.write_text("after")
        self.save("scope.json", ["source.txt"])
        packet = self.run / "api/packet-1"
        command = ["python3", "-I", "-B", str(SCRIPT), "packet", "--contract", self.contract,
                   "--repo", "api", "--scope", self.scope, "--baseline", str(self.base), "--out", str(packet)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("source.txt", state.load(packet / "index.json")["changed_since_review"])
        self.assertTrue(state.load(packet / "delta.json")["changes"])
        evidence = self.evidence("fix.ac-1")
        output = self.run / "recorded-proof.json"
        command = ["python3", "-I", "-B", str(SCRIPT), "record-evidence", "--contract", self.contract,
                   "--repo", "api", "--file", evidence, "--out", str(output)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        record = state.load(output)
        self.assertEqual(2, record["schema_version"])
        self.assertTrue(record["artifact_hashes"])

    def enable_policy(self):
        self.c["telemetry_policy"] = {"repair_phase_required": True}
        self.save("contract.json", self.c)

    def repair_pair(self, identifier="repair-1", action="plan-gap", status="passed", start_changes=None,
                    end_changes=None, close=True):
        from datetime import datetime, timezone
        previous_path, previous = workflow.history(self.run / "api")[-1][1:]
        record = {"run_id": "run-1", "workflow": "fix", "repo": "api", "phase_id": identifier,
                  "phase": "gate-fix" if action == "gate-fix" else "review-fix", "action": action,
                  "agent_id": "original-1", "event": "phase-start",
                  "previous_state_path": str(previous_path), "previous_state_sha256": workflow.sha(previous_path),
                  "at": datetime.now(timezone.utc).isoformat()}
        if action == "gate-fix":
            record["attempt"] = previous["gate_fix_count"] + 1
        else:
            record["round"] = previous["latest_review_round"]
        record.update(start_changes or {})
        start = self.run / f"telemetry/{identifier}-start.json"
        record.update(schema_version=2, session_id="")
        state.write_once(start, record)
        if close:
            record.update(event="phase-end", status=status, at=datetime.now(timezone.utc).isoformat())
            record.update(end_changes or {})
            record["start_sha256"] = workflow.sha(start)
            state.write_once(self.run / f"telemetry/{identifier}-end.json", record)
        return identifier

    def policy_reviewed(self):
        self.enable_policy()
        self.ready()
        self.implement()
        self.move("reviewed", review_path=self.review())

    def test_repair_phase_rejects_bad_binding_before_dispatch(self):
        self.policy_reviewed()
        previous_path, previous = workflow.history(self.run / "api")[-1][1:]
        out = self.run / "telemetry/early-repair-start.json"
        fields = dict(phase_id="early-repair", phase="review-fix", event="phase-start", repo="api",
                      action="plan-gap", agent_id="original-1", round=previous["latest_review_round"],
                      previous_state_path=str(previous_path), previous_state_sha256=workflow.sha(previous_path))
        with self.assertRaisesRegex(ValueError, "previous state mismatch"):
            workflow.phase_from_flags(self.contract, str(out), **{**fields, "previous_state_sha256": "wrong"})
        self.assertFalse(out.exists())
        workflow.phase_from_flags(self.contract, str(out), **fields)
        end = self.run / "telemetry/early-repair-end.json"
        with self.assertRaisesRegex(ValueError, "end/start binding mismatch"):
            workflow.phase_from_flags(self.contract, str(end), **{**fields, "event": "phase-end", "status": "passed", "round": 999})
        self.assertFalse(end.exists())
        workflow.phase_from_flags(self.contract, str(end), **{**fields, "event": "phase-end", "status": "passed"})

    def test_fresh_repair_requires_bound_capsule_and_new_author(self):
        self.c["author_rotation"] = "capsule"
        self.save("contract.json", self.c)
        self.policy_reviewed()
        previous_path, previous = workflow.history(self.run / "api")[-1][1:]
        manifest = self.capture("fresh-capsule-manifest.json")
        capsule = self.save("fresh-capsule.json", self.artifact(written_by="original-1",
                            source_fingerprint=previous["source_fingerprint"], manifest_path=manifest))
        fields = dict(phase_id="fresh-repair", phase="review-fix", repo="api", action="plan-gap",
                      agent_id="original-1", round=previous["latest_review_round"],
                      previous_state_path=str(previous_path), previous_state_sha256=workflow.sha(previous_path))
        start = self.run / "telemetry/fresh-repair-start.json"
        with self.assertRaisesRegex(ValueError, "Missing absolute file"):
            workflow.phase_from_flags(self.contract, str(start), event="phase-start", **fields)
        workflow.phase_from_flags(self.contract, str(start), event="phase-start", capsule_path=capsule, **fields)
        end = self.run / "telemetry/fresh-repair-end.json"
        with self.assertRaisesRegex(ValueError, "new author"):
            workflow.phase_from_flags(self.contract, str(end), event="phase-end", status="passed",
                                      capsule_path=capsule, **fields)
        workflow.phase_from_flags(self.contract, str(end), event="phase-end", status="passed", capsule_path=capsule,
                                  **{**fields, "agent_id": "fresh-author"})

    def test_phase_binding_recovery_records_fresh_author(self):
        self.c["author_rotation"] = "capsule"
        self.save("contract.json", self.c)
        self.enable_policy()
        self.ready()
        self.implement()
        finding = {"id": "api-1", "file": "source.txt", "line": 1, "severity": "Warning", "status": "new",
                   "category": "Logic", "description": "wrong output", "recommendation": "fix result",
                   "failure_scenario": "null returns wrong result", "verification_expectation": "assert null output",
                   "criterion_ids": ["fix.ac-1"], "verification_kind": "empirical"}
        self.move("needs-fix", review_path=self.review([finding]))
        previous_path, previous = workflow.history(self.run / "api")[-1][1:]
        before = self.capture("recovery-before.json")
        capsule = self.save("recovery-before-capsule.json", self.artifact(written_by="original-1",
                            source_fingerprint=previous["source_fingerprint"], manifest_path=before))
        phase = self.repair_pair("arbitrary-repair", action="review-fix", end_changes={"agent_id": "fresh-author"})
        self.source.write_text("fixed by fresh author\n")
        self.move("blocked", reason="missing capsule telemetry binding")
        blocked_path, blocked = workflow.history(self.run / "api")[-1][1:]
        after = self.capture("recovery-after.json")
        result_capsule = self.save("recovery-after-capsule.json", self.artifact(written_by="fresh-author",
                                   source_fingerprint=blocked["source_fingerprint"], manifest_path=after))
        recovery = self.save("recovery.json", self.artifact(kind="ship-phase-binding-recovery-v1",
            reason="fresh-author handoff capsule_path was omitted from immutable phase events",
            user_authorization="User approved recovery", blocked_state_path=str(blocked_path),
            blocked_state_sha256=workflow.sha(blocked_path), preceding_state_path=str(previous_path),
            preceding_state_sha256=workflow.sha(previous_path), phase_id=phase, capsule_path=capsule,
            new_author_agent_id="fresh-author", result_capsule_path=result_capsule))
        for field, value, message in (("user_authorization", "", "Invalid phase-binding recovery"),
                                      ("phase_id", "../foreign", "recorded repair phase"),
                                      ("blocked_state_sha256", "bad", "state binding mismatch")):
            altered = dict(state.load(recovery), **{field: value})
            bad = self.save(f"recovery-bad-{field}.json", altered)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, message):
                workflow.phase_binding_recovery(self.contract, workflow.contract(self.contract), "api",
                                                {"phase_binding_recovery_path": bad},
                                                workflow.history(self.run / "api"), state.checked_manifest(after))
        proof_path = self.evidence("fix.ac-1")
        proof = state.load(proof_path)
        proof["claims"] = [{"id": "api-1", "assertion": "correct output"}]
        self.save(str(Path(proof_path).relative_to(self.run)), proof)
        applied = self.save("recovery-applied.json", self.artifact(round=1, applied=[{
            "id": "api-1", "verified": "output checked", "verification_kind": "empirical",
            "evidence_paths": [proof_path]}], failed=[], skipped=[]))
        event = self.move("edited-unverified", action="review-fix", repair_phase_id=phase,
                          phase_binding_recovery_path=recovery, applied_path=applied,
                          capsule_handoff={"capsule_path": capsule, "new_agent_id": "fresh-author"})
        self.assertEqual("fresh-author", event["current_author_agent_id"])
        self.assertEqual(1, event["review_fix_count"])

    def test_policy_types_and_legacy_absence(self):
        self.assertNotIn("telemetry_policy", workflow.contract(self.contract))
        for policy in (None, [], {}, {"repair_phase_required": 1}, {"repair_phase_required": "true"}):
            self.c["telemetry_policy"] = policy
            self.save("contract.json", self.c)
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, "telemetry_policy"):
                workflow.contract(self.contract)
        self.c["telemetry_policy"] = {"repair_phase_required": False}
        self.save("contract.json", self.c)
        workflow.contract(self.contract)

    def test_policy_current_request_missing_open_and_foreign_pair(self):
        self.policy_reviewed()
        self.source.write_text("repair")
        with self.assertRaisesRegex(ValueError, "current request repair_phase_id"):
            self.move("edited-unverified", action="plan-gap")
        with self.assertRaisesRegex(ValueError, "Missing absolute file"):
            self.move("edited-unverified", action="plan-gap", repair_phase_id="missing")
        self.repair_pair("open", close=False)
        with self.assertRaisesRegex(ValueError, "Missing absolute file"):
            self.move("edited-unverified", action="plan-gap", repair_phase_id="open")
        for index, changes in enumerate(({"agent_id": "foreign"}, {"action": "review-fix"},
                {"previous_state_sha256": "wrong"}, {"previous_state_path": self.initial},
                {"round": 99}, {"repo": None}, {"phase": "gate-fix"},
                {"at": "2020-01-01T00:00:00Z"})):
            identifier = self.repair_pair(f"foreign-{index}", start_changes=changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "binding mismatch|timestamp"):
                self.move("edited-unverified", action="plan-gap", repair_phase_id=identifier)

    def test_policy_seals_reuse_and_identity_override(self):
        self.policy_reviewed()
        identifier = self.repair_pair()
        self.source.write_text("repair")
        event = self.move("edited-unverified", action="plan-gap", repair_phase_id=identifier)
        for suffix in ("start", "end"):
            self.assertIn(str(self.run / f"telemetry/{identifier}-{suffix}.json"), event["artifact_hashes"])
        self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])
        self.move("reviewed", review_path=self.review())
        with self.assertRaisesRegex(ValueError, "already used"):
            self.move("edited-unverified", action="plan-gap", repair_phase_id=identifier)
        with self.assertRaisesRegex(ValueError, "current request repair_phase_id"):
            self.move("edited-unverified", action="plan-gap")
        with self.assertRaisesRegex(ValueError, "original implementer"):
            self.move("edited-unverified", action="plan-gap", original_agent_id="other")
        path = self.run / f"telemetry/{identifier}-end.json"
        path.write_text(path.read_text() + " ")
        with self.assertRaisesRegex(ValueError, "Immutable artifact changed"):
            self.move("edited-unverified", action="plan-gap", repair_phase_id="new")

    def test_policy_block_unblock_requires_repair_then_can_complete(self):
        self.policy_reviewed()
        self.move("blocked", reason="safe runtime temporarily unavailable")
        self.source.write_text("repair while blocked")
        with self.assertRaisesRegex(ValueError, "Source changed without recorded repair"):
            self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])
        with self.assertRaisesRegex(ValueError, "Repo not verified"):
            workflow.complete(self.contract, str(self.run / "completion.json"))
        identifier = self.repair_pair(status="blocked")
        self.move("edited-unverified", action="plan-gap", repair_phase_id=identifier)
        self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])
        self.verify()
        workflow.complete(self.contract, str(self.run / "completion.json"))

    def test_policy_blocked_snapshots_cannot_absorb_unrecorded_edits(self):
        self.policy_reviewed()
        self.source.write_text("partially repaired before blocked snapshot")
        self.move("blocked", reason="repair interrupted")
        self.move("blocked", reason="still awaiting safe runtime")
        self.move("handoff", reason="await original author")
        with self.assertRaisesRegex(ValueError, "Source changed without recorded repair"):
            self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])
        with self.assertRaisesRegex(ValueError, "Source changed without recorded repair"):
            self.move("reviewed", review_path=self.review())

    def test_policy_unchanged_block_unblock_remains_legal(self):
        self.policy_reviewed()
        self.move("blocked", reason="runtime temporarily unavailable, no source edits")
        self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])
        self.verify()
        workflow.complete(self.contract, str(self.run / "completion.json"))

    def test_legacy_policy_absent_block_unblock_keeps_existing_behavior(self):
        self.ready()
        self.implement()
        self.move("reviewed", review_path=self.review())
        self.source.write_text("legacy repair")
        self.move("blocked", reason="legacy interrupted edit")
        self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])
        self.verify()
        workflow.complete(self.contract, str(self.run / "completion.json"))

    def test_policy_from_review_handoff_edits_need_explicit_repair_record(self):
        self.enable_policy()
        self.test_from_review_requires_pre_review_and_author_handoff()
        self.source.write_text("author repair")
        with self.assertRaisesRegex(ValueError, "Source changed without recorded repair"):
            self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])
        self.move("blocked", reason="author repair awaiting recording")
        self.move("edited-unverified", action="plan-gap", author_handoff="original author repair result")
        self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])
        self.verify()
        workflow.complete(self.contract, str(self.run / "completion.json"))

    def test_policy_cannot_reset_source_anchor_by_restarting_implementation(self):
        self.enable_policy()
        self.test_from_review_requires_pre_review_and_author_handoff()
        preflight = self.save("later-blocked-preflight.json", self.artifact(
            status="blocked", reason="runtime unavailable after review"))
        self.move("preflight-blocked", preflight_path=preflight, reason="runtime unavailable after review")
        with self.assertRaisesRegex(ValueError, "cannot restart implementation"):
            self.move("implemented", matrix_path="missing")

    def test_policy_unused_pair_cannot_bind_an_older_state(self):
        self.policy_reviewed()
        identifier = self.repair_pair()
        self.move("gates-passed", gate_evidence_paths=[self.evidence("gate.tests")])
        with self.assertRaisesRegex(ValueError, "binding mismatch"):
            self.move("edited-unverified", action="plan-gap", repair_phase_id=identifier)

    def test_policy_original_identity_is_sealed_and_checked_against_origin(self):
        self.policy_reviewed()
        events = workflow.history(self.run / "api")
        original_path = next(path for _, path, event in events if event["status"] == "implemented")
        self.assertIn(str(original_path), events[-1][2]["artifact_hashes"])
        previous_path = events[-1][1]
        record = state.load(previous_path)
        record["original_agent_id"] = "other-agent"
        self.save(str(previous_path.relative_to(self.run)), record)
        identifier = self.repair_pair(start_changes={"agent_id": "other-agent"})
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.move("edited-unverified", action="plan-gap", repair_phase_id=identifier)
        original_path.write_text(original_path.read_text() + " ")
        with self.assertRaisesRegex(ValueError, "Immutable artifact changed"):
            self.move("edited-unverified", action="plan-gap", repair_phase_id=identifier)

    def test_policy_start_hash_and_end_timestamp_checked(self):
        self.policy_reviewed()
        identifier = self.repair_pair()
        path = self.run / f"telemetry/{identifier}-end.json"
        record = state.load(path)
        original = dict(record)
        for changes, message in (({"start_sha256": "bad"}, "start hash"),
                                 ({"at": "2099-01-01T00:00:00Z"}, "timestamp"),
                                 ({"at": "2020-01-01T00:00:00Z"}, "timestamp"),
                                 ({"run_id": "other"}, "binding mismatch"),
                                 ({"workflow": "ship"}, "binding mismatch"),
                                 ({"status": "unknown"}, "not closed")):
            self.save(str(path.relative_to(self.run)), {**original, **changes})
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                self.move("edited-unverified", action="plan-gap", repair_phase_id=identifier)

    def test_policy_failed_blocked_interrupted_closures_are_unverified(self):
        self.policy_reviewed()
        for index, status in enumerate(("failed", "blocked", "interrupted")):
            identifier = self.repair_pair(f"repair-{index}", status=status)
            self.source.write_text(f"repair {index}")
            event = self.move("edited-unverified", action="plan-gap", repair_phase_id=identifier)
            self.assertEqual("edited-unverified", event["status"])
            with self.assertRaisesRegex(ValueError, "Repo not verified"):
                workflow.complete(self.contract, str(self.run / "completion.json"))
            self.move("pre-review-passed", pre_review_evidence_paths=[self.evidence("early.tests")])
            self.move("reviewed", review_path=self.review())

    def test_policy_gate_fix_and_author_handoff_compatibility(self):
        self.policy_reviewed()
        false_failure = self.evidence("gate.tests", status="passed")
        identifier = self.repair_pair("false-gate", action="gate-fix")
        with self.assertRaisesRegex(ValueError, "Required evidence did not pass"):
            self.move("edited-unverified", action="gate-fix", repair_phase_id=identifier,
                      gate_failure_path=false_failure)
        failure = self.evidence("gate.tests", status="failed")
        identifier = self.repair_pair(action="gate-fix", status="failed")
        self.source.write_text("gate repair")
        event = self.move("edited-unverified", action="gate-fix", repair_phase_id=identifier,
                          gate_failure_path=failure)
        self.assertEqual(1, event["gate_fix_count"])
        with self.assertRaises(ValueError):
            self.move("verified", assessment_path="missing")

    def test_policy_capsule_handoff_moves_repair_to_fresh_author(self):
        self.c["author_rotation"] = "capsule"
        self.save("contract.json", self.c)
        self.policy_reviewed()
        manifest = self.save("capsule-manifest.json", state.capture(self.repo, ["source.txt"]))
        fingerprint = state.capture(self.repo, ["source.txt"])["fingerprint"]
        capsule = self.save("capsule-1.json", {
            "schema_version": 2, "run_id": "run-1", "repo": "api", "written_by": "other-agent",
            "source_fingerprint": fingerprint, "manifest_path": manifest})
        self.source.write_text("repair")
        # Each attempt needs its own phase: a refused attempt leaves its phase files behind.
        wrong = self.save("capsule-2.json", {
            "schema_version": 2, "run_id": "run-1", "repo": "api", "written_by": "other-agent",
            "source_fingerprint": fingerprint, "manifest_path": manifest})
        identifier = self.repair_pair("r-wrong", action="plan-gap",
                                      start_changes={"capsule_path": wrong},
                                      end_changes={"agent_id": "fresh-1", "capsule_path": wrong})
        with self.assertRaisesRegex(ValueError, "written by the current author"):
            self.move("edited-unverified", action="plan-gap", repair_phase_id=identifier,
                      capsule_handoff={"capsule_path": wrong, "new_agent_id": "fresh-1"})
        stale = self.save("capsule-3.json", {
            "schema_version": 2, "run_id": "run-1", "repo": "api", "written_by": "original-1",
            "source_fingerprint": "0" * 64, "manifest_path": manifest})
        identifier = self.repair_pair("r-stale", action="plan-gap",
                                      start_changes={"capsule_path": stale},
                                      end_changes={"agent_id": "fresh-1", "capsule_path": stale})
        with self.assertRaisesRegex(ValueError, "stale"):
            self.move("edited-unverified", action="plan-gap", repair_phase_id=identifier,
                      capsule_handoff={"capsule_path": stale, "new_agent_id": "fresh-1"})
        good = self.save("capsule-4.json", {
            "schema_version": 2, "run_id": "run-1", "repo": "api", "written_by": "original-1",
            "source_fingerprint": fingerprint, "manifest_path": manifest})
        # The start must name the predecessor and the capsule; naming the new author there is refused.
        identifier = self.repair_pair("r-badstart", action="plan-gap",
                                      start_changes={"agent_id": "fresh-1", "capsule_path": good},
                                      end_changes={"agent_id": "fresh-1", "capsule_path": good})
        with self.assertRaisesRegex(ValueError, "binding mismatch"):
            self.move("edited-unverified", action="plan-gap", repair_phase_id=identifier,
                      capsule_handoff={"capsule_path": good, "new_agent_id": "fresh-1"})
        identifier = self.repair_pair("r-good", action="plan-gap",
                                      start_changes={"capsule_path": good},
                                      end_changes={"agent_id": "fresh-1", "capsule_path": good})
        event = self.move("edited-unverified", action="plan-gap", repair_phase_id=identifier,
                          capsule_handoff={"capsule_path": good, "new_agent_id": "fresh-1"})
        self.assertEqual("fresh-1", event["current_author_agent_id"])
        self.assertEqual("original-1", event["original_agent_id"])

    def test_policy_from_review_has_no_fictitious_implementer(self):
        self.enable_policy()
        self.test_from_review_requires_pre_review_and_author_handoff()
        # Resume the same author-owned submitted source without a repair phase.
        self.move("reviewed", review_path=self.review())
        event = self.move("edited-unverified", action="plan-gap", author_handoff="author session result")
        self.assertEqual("edited-unverified", event["status"])
        self.assertNotIn("original_agent_id", event)

    def test_policy_review_fix_preserves_authoritative_finding_proof(self):
        self.enable_policy()
        self.test_review_fix_rounds_proof_and_counters()

    def test_policy_implementation_requires_original_identity(self):
        self.enable_policy()
        self.ready()
        with self.assertRaisesRegex(ValueError, "original implementer"):
            self.move("implemented", matrix_path="missing")

    def test_protected_certification_fields_cannot_be_overridden(self):
        self.ready()
        with self.assertRaisesRegex(ValueError, "certified lifecycle"):
            self.move("implemented", review_fingerprint="fake", matrix_path="missing")


class SliceLifecycleTests(unittest.TestCase):
    """Sequential slice lifecycle: own review budget, ownership, staleness, gate before the repo lifecycle."""

    save, capture, artifact, evidence = (LifecycleTests.save, LifecycleTests.capture,
                                         LifecycleTests.artifact, LifecycleTests.evidence)
    slice_item = LifecycleTests.slice_item
    build_schedule = LifecycleTests.schedule

    def setUp(self):
        LifecycleTests.setUp(self)
        for name in ("a.txt", "b.txt"):
            (self.repo / name).write_text(name[0] + "0\n")
            state.baseline(self.repo, self.base, name)
        self.c["workflow"] = "fix"
        self.edits = 0
        item = lambda i, scope, deps: {
            "id": i, "title": i, "contributions": [{"criterion_id": "fix.ac-1", "part": "part " + i}],
            "write_scope": [scope], "depends_on": deps, "interfaces": [], "targeted_checks": [],
            "resources": [], "parallel_allowed": False}
        data = {"schema_version": 2, "run_id": "run-1", "repo": "api", "approved": True, "approved_by": "user",
                "approved_at": "2026-10-16T00:00:00Z", "cross_slice_criteria": ["fix.ac-1"],
                "slices": [item("a", "a.txt", []), item("b", "b.txt", ["a"])]}
        self.schedule = self.save("slices-approved.json", data)
        self.c["slice_schedules"] = {"api": {"path": self.schedule, "sha256": workflow.sha(self.schedule)}}
        self.save("contract.json", self.c)
        self.repo_scope = self.save("repo-scope.json", ["a.txt", "b.txt"])
        self.initial_repo = self.cap(["a.txt", "b.txt"], "repo-initial.json")
        self.bases = {"a": self.cap(["a.txt"], "base-a.json"), "b": self.cap(["b.txt"], "base-b.json")}

    def cap(self, touched, name=None):
        self.counter += 1
        return self.save(name or f"cap-{self.counter}.json", state.capture(self.repo, touched))

    def move_slice(self, slice_id, status, touched, **kwargs):
        directory = self.run / "api" / "slices" / slice_id
        events = workflow.history(directory) if directory.is_dir() else []
        self.counter += 1
        request = self.save(f"slice-request-{self.counter}.json", {
            "status": status, "source_manifest": self.cap(touched), "base_manifest": self.bases[slice_id],
            "touched": touched, **kwargs})
        return workflow.slice_transition(self.contract, "api", slice_id, self.schedule, request,
                                         str(directory / f"state-{len(events) + 1}.json"))

    def slice_matrix(self, slice_id):
        return self.save(f"matrix-{slice_id}.json", self.artifact(rows=[{
            "criterion_id": "fix.ac-1", "part": "part " + slice_id, "assertion": "part asserted",
            "status": "unverified", "reason": "proved at whole-criterion acceptance", "evidence_paths": []}]))

    def slice_review(self, slice_id, touched, round_number, findings=None, dispositions=None, source=None):
        fingerprint = (state.load(source) if source else state.capture(self.repo, touched))["fingerprint"]
        result = self.artifact(round=round_number, source_fingerprint=fingerprint, accepted=True,
            coverage={"read": touched, "skipped": [], "mandatory_passes": [
                {"id": "logic", "status": "inspected", "reason": "checked", "sites": touched},
                {"id": "wiring", "status": "not-applicable", "reason": "no constructors", "sites": []}]},
            findings=findings or [], dispositions=dispositions or [])
        self.counter += 1
        return self.save(f"slice-review-{self.counter}.json", result)

    def accept(self, slice_id, filename, author="author-", **kwargs):
        touched = [filename]
        (self.repo / filename).write_text(filename[0] + "1\n")
        self.move_slice(slice_id, "implemented", touched, author_agent_id=author + slice_id,
                        matrix_path=self.slice_matrix(slice_id), **kwargs)
        self.move_slice(slice_id, "pre-review-passed", touched,
                        pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(touched))])
        self.move_slice(slice_id, "reviewed", touched, review_path=self.slice_review(slice_id, touched, 1))
        return self.move_slice(slice_id, "accepted", touched)

    def gate(self, name="gate.json"):
        out = self.run / name
        workflow.slice_gate(self.contract, "api", self.schedule, self.initial_repo,
                            self.cap(["a.txt", "b.txt"]), str(out))
        return str(out)

    def test_slices_accept_in_order_and_gate_covers_every_change(self):
        with self.assertRaisesRegex(ValueError, "predecessors are not accepted"):
            (self.repo / "b.txt").write_text("b1\n")
            self.move_slice("b", "implemented", ["b.txt"], author_agent_id="author-b",
                            matrix_path=self.slice_matrix("b"))
        (self.repo / "b.txt").write_text("b0\n")
        self.accept("a", "a.txt")
        accepted_b = self.accept("b", "b.txt")
        self.assertEqual({"a"}, set(accepted_b["dependency_states"]))
        with self.assertRaisesRegex(ValueError, "Illegal slice transition"):
            self.move_slice("a", "reviewed", ["a.txt"], review_path=self.slice_review("a", ["a.txt"], 2))
        record = state.load(self.gate())
        self.assertEqual({"a": "author-a", "b": "author-b"}, record["slice_authors"])
        self.assertEqual(["fix.ac-1"], record["cross_slice_criteria"])

    def test_slice_may_not_touch_a_file_owned_by_another_slice(self):
        (self.repo / "b.txt").write_text("b1\n")
        with self.assertRaisesRegex(ValueError, "outside slice write_scope"):
            self.move_slice("a", "implemented", ["a.txt", "b.txt"], author_agent_id="author-a",
                            matrix_path=self.slice_matrix("a"))
        # A non-git capture only sees declared paths, so an undeclared change needs a real git checkout.
        (self.repo / "b.txt").write_text("b0\n")
        for command in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"]):
            subprocess.run(["git", "-C", str(self.repo), *command], check=True, capture_output=True)
        self.bases["a"] = self.cap(["a.txt"])
        (self.repo / "b.txt").write_text("b1\n")
        with self.assertRaisesRegex(ValueError, "outside the slice's declared touched files"):
            self.move_slice("a", "implemented", ["a.txt"], author_agent_id="author-a",
                            matrix_path=self.slice_matrix("a"))

    def test_gate_rejects_unaccepted_unowned_and_stale_slices(self):
        self.accept("a", "a.txt")
        with self.assertRaisesRegex(ValueError, "Slice not accepted: b"):
            self.gate("g1.json")
        self.accept("b", "b.txt")
        (self.repo / "a.txt").write_text("sneaky\n")
        with self.assertRaisesRegex(ValueError, "Slice a is stale"):
            self.gate("g2.json")
        (self.repo / "a.txt").write_text("a1\n")
        self.gate("g3.json")

    def integration_fixture(self, edit_b=False):
        from datetime import datetime, timezone
        schedule = state.load(self.schedule)
        schedule["slices"].append(self.slice_item("c", ["c.txt"], depends_on=["b"],
            contributions=[{"criterion_id": "fix.ac-1", "part": "part c"}]))
        self.schedule = self.save("slices-approved.json", schedule)
        self.c["slice_schedules"]["api"]["sha256"] = workflow.sha(self.schedule)
        self.save("contract.json", self.c)
        for command in (["init", "-q"], ["add", "-A"],
                        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"]):
            subprocess.run(["git", "-C", str(self.repo), *command], check=True, capture_output=True)
        self.bases["a"] = self.cap(["a.txt"])
        self.accept("a", "a.txt")
        self.bases["b"] = self.cap(["b.txt"])
        a_state = workflow.history(self.run / "api/slices/a")[-1][1]
        self.accept("b", "b.txt", predecessor_states={"a": {"path": str(a_state), "sha256": workflow.sha(a_state)}})
        state.baseline(self.repo, self.base, "c.txt")
        (self.repo / "c.txt").write_text("partial c\n")
        start = self.cap(["a.txt", "b.txt", "c.txt"])
        starting = {}
        for identifier in ("a", "b"):
            event = workflow.history(self.run / "api/slices" / identifier)[-1]
            starting[identifier] = {"state_path": str(event[1]), "state_sha256": workflow.sha(event[1]),
                                    "author_agent_id": event[2]["author_agent_id"]}
        self.integration_auth = self.save("alignment/authorization.json", self.artifact(
            user_authorization="User approves limited alignment and dependency recertification",
            approved_at=datetime.now(timezone.utc).isoformat(), contract_sha256=workflow.sha(self.contract),
            schedule_sha256=workflow.sha(self.schedule), starting_slices=starting,
            start_manifest_path=start, start_manifest_sha256=workflow.sha(start),
            partial_slices={"c": ["c.txt"]},
            edit_paths={"a": ["a.txt"], **({"b": ["b.txt"]} if edit_b else {})}, recertify_slices=["b"]))

    def integration_request(self, identifier, mode="repair"):
        from datetime import datetime, timezone
        event = workflow.history(self.run / "api/slices" / identifier)[-1]
        start = self.cap(["a.txt", "b.txt", "c.txt"])
        bindings = {}
        for other in ("a", "b"):
            if other != identifier:
                latest = workflow.history(self.run / "api/slices" / other)[-1]
                bindings[other] = {"path": str(latest[1]), "sha256": workflow.sha(latest[1])}
        return self.artifact(slice_id=identifier, mode=mode, user_authorization="Approved alignment checkpoint",
            author_agent_id=event[2]["author_agent_id"], previous_state_path=str(event[1]),
            previous_state_sha256=workflow.sha(event[1]), contract_sha256=workflow.sha(self.contract),
            schedule_sha256=workflow.sha(self.schedule), authorization_path=self.integration_auth,
            authorization_sha256=workflow.sha(self.integration_auth), start_manifest=start,
            start_manifest_sha256=workflow.sha(start), at=datetime.now(timezone.utc).isoformat(),
            slice_states=bindings, partial_slices={"c": ["c.txt"]})

    def test_authorized_alignment_preserves_base_and_recertifies_unchanged_dependents(self):
        self.integration_fixture()
        original = workflow.history(self.run / "api/slices/a")[-1][2]
        old_b = workflow.history(self.run / "api/slices/b")[-1][1]
        old_b_hash = workflow.sha(old_b)
        checkpoint = self.save("alignment/a.json", self.integration_request("a"))
        (self.repo / "a.txt").write_text("a2\n")
        edited = self.move_slice("a", "edited-unverified", ["a.txt"], action="integration-fix",
                                 integration_path=checkpoint, author_agent_id="author-a")
        self.assertEqual(original["base_manifest"], edited["base_manifest"])
        self.assertEqual(original["review_fix_count"], edited["review_fix_count"])
        self.assertIn(checkpoint, edited["artifact_hashes"])
        self.move_slice("a", "pre-review-passed", ["a.txt"],
                        pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(["a.txt"]))])
        self.move_slice("a", "reviewed", ["a.txt"], review_path=self.slice_review("a", ["a.txt"], 2))
        self.move_slice("a", "accepted", ["a.txt"])
        self.assertEqual({"a"}, workflow.accepted_slices(self.contract, "api", {"a", "b"}))
        dependent = workflow.history(self.run / "api/slices/b")[-1][2]
        checkpoint_b = self.save("alignment/b.json", self.integration_request("b", "dependency-recheck"))
        self.move_slice("b", "pre-review-passed", ["b.txt"], action="dependency-recheck",
                        integration_path=checkpoint_b,
                        pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(["b.txt"]))])
        self.move_slice("b", "reviewed", ["b.txt"], review_path=self.slice_review("b", ["b.txt"], 2))
        accepted = self.move_slice("b", "accepted", ["b.txt"])
        a_state = workflow.history(self.run / "api/slices/a")[-1][1]
        self.assertEqual({"path": str(a_state), "sha256": workflow.sha(a_state)}, accepted["predecessor_states"]["a"])
        self.assertEqual(old_b_hash, workflow.sha(old_b))
        self.assertEqual(dependent["accepted_files"], accepted["accepted_files"])
        self.assertEqual(dependent["review_fix_count"], accepted["review_fix_count"])
        self.assertEqual(2, accepted["latest_review_round"])
        self.assertEqual({"a", "b"}, workflow.accepted_slices(self.contract, "api", {"a", "b"}))
        with self.assertRaisesRegex(ValueError, "Slice not accepted: c"):
            self.gate("still-partial.json")

    def test_dependent_integration_repair_rebinds_predecessor(self):
        self.integration_fixture(edit_b=True)
        old_b = workflow.history(self.run / "api/slices/b")[-1]
        old_a_path = workflow.history(self.run / "api/slices/a")[-1][1]
        checkpoint = self.save("alignment/a.json", self.integration_request("a"))
        (self.repo / "a.txt").write_text("a2\n")
        self.move_slice("a", "edited-unverified", ["a.txt"], action="integration-fix", integration_path=checkpoint)
        self.move_slice("a", "pre-review-passed", ["a.txt"],
                        pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(["a.txt"]))])
        self.move_slice("a", "reviewed", ["a.txt"], review_path=self.slice_review("a", ["a.txt"], 2))
        self.move_slice("a", "accepted", ["a.txt"])
        self.assertNotIn("b", workflow.accepted_slices(self.contract, "api", {"b"}))
        request = self.integration_request("b")
        checkpoint = self.save("alignment/b.json", request)
        stale = self.save("alignment/b-stale.json", {
            **request, "slice_states": {"a": {"path": str(old_a_path), "sha256": workflow.sha(old_a_path)}}})
        (self.repo / "b.txt").write_text("b2\n")
        with self.assertRaisesRegex(ValueError, "Predecessor state changed"):
            self.move_slice("b", "edited-unverified", ["b.txt"], action="integration-fix")
        with self.assertRaisesRegex(ValueError, "Integration sibling state changed"):
            self.move_slice("b", "edited-unverified", ["b.txt"], action="integration-fix", integration_path=stale)
        self.move_slice("b", "edited-unverified", ["b.txt"], action="integration-fix", integration_path=checkpoint)
        self.move_slice("b", "pre-review-passed", ["b.txt"],
                        pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(["b.txt"]))])
        self.move_slice("b", "reviewed", ["b.txt"], review_path=self.slice_review("b", ["b.txt"], 2))
        accepted = self.move_slice("b", "accepted", ["b.txt"])
        a_path = workflow.history(self.run / "api/slices/a")[-1][1]
        self.assertEqual(workflow.sha(a_path), accepted["predecessor_states"]["a"]["sha256"])
        self.assertEqual(old_b[2]["base_manifest"], accepted["base_manifest"])

    def test_alignment_rejects_foreign_bindings_scope_drift_and_reuse(self):
        self.integration_fixture()
        checkpoint = self.integration_request("a")
        (self.repo / "a.txt").write_text("a2\n")
        with self.assertRaisesRegex(ValueError, "outside the slice"):
            self.move_slice("a", "edited-unverified", ["a.txt"], action="integration-fix")
        variants = [{"repo": "foreign"}, {"slice_id": "b"}, {"author_agent_id": "other"},
                    {"previous_state_sha256": "bad"}, {"contract_sha256": "bad"},
                    {"schedule_sha256": "bad"}, {"authorization_sha256": "bad"},
                    {"start_manifest_sha256": "bad"},
                    {"slice_states": {"b": {"path": "wrong", "sha256": "bad"}}},
                    {"partial_slices": {"c": ["source.txt"]}}, {"mode": "dependency-recheck"}]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                bad = self.save("alignment/bad.json", {**checkpoint, **changes})
                self.move_slice("a", "edited-unverified", ["a.txt"], action="integration-fix", integration_path=bad)
        valid = self.save("alignment/a.json", checkpoint)
        for name in ("b.txt", "c.txt"):
            old = (self.repo / name).read_text()
            (self.repo / name).write_text("unauthorized change\n")
            with self.subTest(path=name), self.assertRaisesRegex(ValueError, "source changed"):
                self.move_slice("a", "edited-unverified", ["a.txt"], action="integration-fix", integration_path=valid)
            (self.repo / name).write_text(old)
        (self.repo / "unknown.txt").write_text("unowned\n")
        with self.assertRaisesRegex(ValueError, "foreign source changed"):
            self.move_slice("a", "edited-unverified", ["a.txt"], action="integration-fix", integration_path=valid)
        (self.repo / "unknown.txt").unlink()
        with self.assertRaisesRegex(ValueError, "original slice base"):
            self.move_slice("a", "edited-unverified", ["a.txt"], action="integration-fix",
                            integration_path=valid, base_manifest=self.cap(["a.txt"]))
        with self.assertRaisesRegex(ValueError, "certified slice fields"):
            self.move_slice("a", "edited-unverified", ["a.txt"], action="integration-fix",
                            integration_path=valid, review_fix_count=0)
        self.move_slice("a", "edited-unverified", ["a.txt"], action="integration-fix", integration_path=valid)
        self.move_slice("a", "pre-review-passed", ["a.txt"],
                        pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(["a.txt"]))])
        self.move_slice("a", "reviewed", ["a.txt"], review_path=self.slice_review("a", ["a.txt"], 2))
        self.move_slice("a", "accepted", ["a.txt"])
        with self.assertRaises(ValueError):
            self.move_slice("a", "edited-unverified", ["a.txt"], action="integration-fix", integration_path=valid)

    def test_dependency_recheck_rejects_source_edits_and_missing_authorization(self):
        self.integration_fixture()
        with self.assertRaisesRegex(ValueError, "Illegal slice transition"):
            self.move_slice("b", "pre-review-passed", ["b.txt"], action="dependency-recheck",
                            pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(["b.txt"]))])
        checkpoint = self.save("alignment/b.json", self.integration_request("b", "dependency-recheck"))
        (self.repo / "b.txt").write_text("b2\n")
        with self.assertRaisesRegex(ValueError, "authorized integration edits"):
            self.move_slice("b", "pre-review-passed", ["b.txt"], action="dependency-recheck",
                            integration_path=checkpoint,
                            pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(["b.txt"]))])
        (self.repo / "b.txt").write_text("b1\n")
        # Explicit authorization of a different slice cannot authorize editing this one.
        bad = self.integration_request("b", "repair")
        checkpoint = self.save("alignment/b-edit.json", bad)
        with self.assertRaisesRegex(ValueError, "no authorized integration edits"):
            self.move_slice("b", "edited-unverified", ["b.txt"], action="integration-fix", integration_path=checkpoint)

    def test_alignment_requires_all_foreign_changes_to_have_accepted_or_partial_provenance(self):
        self.integration_fixture()
        checkpoint = self.integration_request("a")
        checkpoint["slice_states"] = {}
        path = self.save("alignment/omitted-sibling.json", checkpoint)
        (self.repo / "a.txt").write_text("a2\n")
        with self.assertRaisesRegex(ValueError, "omits.*sibling"):
            self.move_slice("a", "edited-unverified", ["a.txt"], action="integration-fix", integration_path=path)
        checkpoint = self.integration_request("a")
        # A manifest captured after the target edit cannot replace the starting state.
        path = self.save("alignment/late-start.json", checkpoint)
        with self.assertRaisesRegex(ValueError, "start differs"):
            self.move_slice("a", "edited-unverified", ["a.txt"], action="integration-fix", integration_path=path)
        (self.repo / "a.txt").write_text("a1\n")
        checkpoint = self.integration_request("a")
        path = self.save("alignment/head-drift.json", checkpoint)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "--allow-empty", "-qm", "foreign HEAD"], check=True, capture_output=True)
        (self.repo / "a.txt").write_text("a2\n")
        with self.assertRaisesRegex(ValueError, "HEAD changed"):
            self.move_slice("a", "edited-unverified", ["a.txt"], action="integration-fix", integration_path=path)

    def test_alignment_cannot_hide_omitted_siblings_by_restoring_original_base(self):
        self.integration_fixture()
        checkpoint = self.integration_request("a")
        checkpoint["slice_states"] = {}
        path = self.save("alignment/hidden-revert.json", checkpoint)
        before = len(workflow.history(self.run / "api/slices/a"))
        (self.repo / "a.txt").write_text("a2\n")
        (self.repo / "b.txt").write_text("b0\n")
        with self.assertRaisesRegex(ValueError, "foreign source changed"):
            self.move_slice("a", "edited-unverified", ["a.txt"], action="integration-fix", integration_path=path)
        self.assertEqual(before, len(workflow.history(self.run / "api/slices/a")))
        (self.repo / "b.txt").write_text("b1\n")
        checkpoint["slice_states"] = self.integration_request("a")["slice_states"]
        checkpoint["partial_slices"] = {}
        path = self.save("alignment/hidden-partial-delete.json", checkpoint)
        (self.repo / "c.txt").unlink()
        with self.assertRaisesRegex(ValueError, "foreign source changed"):
            self.move_slice("a", "edited-unverified", ["a.txt"], action="integration-fix", integration_path=path)
        self.assertEqual(before, len(workflow.history(self.run / "api/slices/a")))

    def test_alignment_cannot_omit_source_changed_before_checkpoint_capture(self):
        self.integration_fixture()
        before = len(workflow.history(self.run / "api/slices/a"))
        # The unauthorized revert precedes capture, so a start/final comparison alone is insufficient.
        (self.repo / "b.txt").write_text("b0\n")
        checkpoint = self.integration_request("a")
        checkpoint["slice_states"] = {}
        path = self.save("alignment/pre-capture-revert.json", checkpoint)
        (self.repo / "a.txt").write_text("a2\n")
        with self.assertRaisesRegex(ValueError, "omits.*sibling"):
            self.move_slice("a", "edited-unverified", ["a.txt"], action="integration-fix", integration_path=path)
        self.assertEqual(before, len(workflow.history(self.run / "api/slices/a")))
        (self.repo / "a.txt").write_text("a1\n")
        (self.repo / "b.txt").write_text("b1\n")
        (self.repo / "c.txt").unlink()
        checkpoint = self.integration_request("a")
        checkpoint["partial_slices"] = {}
        path = self.save("alignment/pre-capture-delete.json", checkpoint)
        (self.repo / "a.txt").write_text("a2\n")
        with self.assertRaisesRegex(ValueError, "omits authorized partial"):
            self.move_slice("a", "edited-unverified", ["a.txt"], action="integration-fix", integration_path=path)
        self.assertEqual(before, len(workflow.history(self.run / "api/slices/a")))

    def test_alignment_rejects_head_change_before_checkpoint_for_both_modes(self):
        self.integration_fixture()
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "--allow-empty", "-qm", "HEAD before checkpoint"],
                       check=True, capture_output=True)
        for identifier, mode in (("a", "repair"), ("b", "dependency-recheck")):
            checkpoint = self.save("alignment/head-before-" + identifier + ".json",
                                   self.integration_request(identifier, mode))
            status = "edited-unverified" if mode == "repair" else "pre-review-passed"
            action = "integration-fix" if mode == "repair" else "dependency-recheck"
            before = len(workflow.history(self.run / "api/slices" / identifier))
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "HEAD changed"):
                self.move_slice(identifier, status, [identifier + ".txt"], action=action,
                                integration_path=checkpoint,
                                pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap([identifier + ".txt"]))])
            self.assertEqual(before, len(workflow.history(self.run / "api/slices" / identifier)))

    def integration_resume_fixture(self):
        from datetime import datetime, timezone
        self.integration_fixture()
        old = self.integration_request("a")
        old_path = self.save("alignment/a-original.json", old)
        (self.repo / "a.txt").write_text("a2\n")
        self.move_slice("a", "edited-unverified", ["a.txt"], action="integration-fix", integration_path=old_path)
        paused = workflow.history(self.run / "api/slices/a")[-1]
        resume = self.artifact(slice_id="a", author_agent_id="author-a", user_authorization="Fix partial c then resume a",
            paused_state_path=str(paused[1]), paused_state_sha256=workflow.sha(paused[1]),
            superseded_checkpoint_path=old_path, superseded_checkpoint_sha256=workflow.sha(old_path),
            before_manifest=paused[2]["source_manifest"], before_manifest_sha256=workflow.sha(paused[2]["source_manifest"]),
            partial_repair_paths={"c": ["c.txt"]}, approved_at=datetime.now(timezone.utc).isoformat())
        resume_path = self.save("alignment/resume-approval.json", resume)
        (self.repo / "c.txt").write_text("fixed partial c\n")
        start = self.cap(["a.txt", "b.txt", "c.txt"])
        authorization = {**state.load(self.integration_auth), "start_manifest_path": start,
            "start_manifest_sha256": workflow.sha(start), "resume_authorization_path": resume_path,
            "resume_authorization_sha256": workflow.sha(resume_path), "approved_at": datetime.now(timezone.utc).isoformat()}
        auth_path = self.save("alignment/resumed-authorization.json", authorization)
        checkpoint = {**old, "authorization_path": auth_path, "authorization_sha256": workflow.sha(auth_path),
                      "start_manifest": start, "start_manifest_sha256": workflow.sha(start),
                      "at": datetime.now(timezone.utc).isoformat()}
        return checkpoint, resume, authorization, paused

    def test_authorized_partial_repair_resumes_without_replacing_target_edit_or_history(self):
        checkpoint, _, _, paused = self.integration_resume_fixture()
        path = self.save("alignment/a-resumed.json", checkpoint)
        before = state.load(paused[1])
        resumed = self.move_slice("a", "pre-review-passed", ["a.txt"], action="integration-resume", integration_path=path,
            pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(["a.txt"]))])
        self.assertEqual(before["base_manifest"], resumed["base_manifest"])
        self.assertEqual(before["review_fix_count"], resumed["review_fix_count"])
        self.assertEqual(before["latest_review_round"], resumed["latest_review_round"])
        self.assertEqual(before, state.load(paused[1]))
        self.assertIn(str(paused[1]), resumed["artifact_hashes"])
        self.move_slice("a", "reviewed", ["a.txt"], review_path=self.slice_review("a", ["a.txt"], 2))
        accepted = self.move_slice("a", "accepted", ["a.txt"])
        self.assertEqual(2, accepted["latest_review_round"])
        self.assertEqual("a2\n", (self.repo / "a.txt").read_text())

    def test_partial_repair_resume_rejects_unbound_repair_and_target_drift(self):
        checkpoint, resume, authorization, paused = self.integration_resume_fixture()
        for changes in ({"paused_state_sha256": "bad"}, {"author_agent_id": "other"},
                        {"superseded_checkpoint_sha256": "bad"}, {"before_manifest_sha256": "bad"},
                        {"partial_repair_paths": {"b": ["b.txt"]}}, {"partial_repair_paths": {"c": ["source.txt"]}},
                        {"run_id": "foreign"}, {"slice_id": "b"}):
            approval = self.save("alignment/bad-resume.json", {**resume, **changes})
            auth = {**authorization, "resume_authorization_path": approval,
                    "resume_authorization_sha256": workflow.sha(approval)}
            auth_path = self.save("alignment/bad-resume-auth.json", auth)
            request = {**checkpoint, "authorization_path": auth_path, "authorization_sha256": workflow.sha(auth_path)}
            path = self.save("alignment/bad-resume-checkpoint.json", request)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.move_slice("a", "pre-review-passed", ["a.txt"], action="integration-resume", integration_path=path,
                    pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(["a.txt"]))])
        path = self.save("alignment/a-resumed.json", checkpoint)
        (self.repo / "a.txt").write_text("a3 unrecorded\n")
        with self.assertRaisesRegex(ValueError, "cannot add target source edits"):
            self.move_slice("a", "pre-review-passed", ["a.txt"], action="integration-resume", integration_path=path,
                pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(["a.txt"]))])
        (self.repo / "a.txt").write_text("a2\n")
        (self.repo / "b.txt").write_text("unauthorized sibling\n")
        with self.assertRaisesRegex(ValueError, "foreign source changed"):
            self.move_slice("a", "pre-review-passed", ["a.txt"], action="integration-resume", integration_path=path,
                pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(["a.txt"]))])
        self.assertEqual(str(paused[1]), str(workflow.history(self.run / "api/slices/a")[-1][1]))

    def test_reaccepting_a_predecessor_makes_dependents_stale(self):
        self.accept("a", "a.txt")
        self.accept("b", "b.txt")
        (self.repo / "a.txt").write_text("a2\n")
        self.move_slice("a", "edited-unverified", ["a.txt"], action="integration-fix", author_agent_id="author-a")
        self.move_slice("a", "pre-review-passed", ["a.txt"],
                        pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(["a.txt"]))],
                        matrix_path=self.slice_matrix("a"))
        self.move_slice("a", "reviewed", ["a.txt"], review_path=self.slice_review("a", ["a.txt"], 2))
        self.move_slice("a", "accepted", ["a.txt"])
        with self.assertRaisesRegex(ValueError, "predecessor a was re-accepted"):
            self.gate()

    def test_repair_goes_to_the_slice_author_and_review_budget_is_per_slice(self):
        self.accept("a", "a.txt")
        touched = ["a.txt"]
        finding = {"id": "api-1", "file": "a.txt", "line": 1, "severity": "Warning", "status": "new",
                   "category": "Logic", "description": "wrong", "recommendation": "fix",
                   "failure_scenario": "x", "verification_expectation": "assert", "criterion_ids": [],
                   "verification_kind": "static"}
        self.move_slice("a", "edited-unverified", touched, action="integration-fix", author_agent_id="author-a")
        self.move_slice("a", "pre-review-passed", touched,
                        pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(touched))])
        round_number = 2
        for cycle in range(3):
            finding["status"] = "new" if cycle == 0 else "recurring"
            self.move_slice("a", "needs-fix", touched,
                            review_path=self.slice_review("a", touched, round_number, [finding]))
            applied = self.save(f"applied-{cycle}.json", self.artifact(round=round_number, applied=[],
                failed=[{"id": "api-1", "reason": "Edited but unverified: pending"}], skipped=[]))
            with self.assertRaisesRegex(ValueError, "slice author"):
                self.move_slice("a", "edited-unverified", touched, action="review-fix", applied_path=applied,
                                author_agent_id="someone-else")
            event = self.move_slice("a", "edited-unverified", touched, action="review-fix", applied_path=applied)
            self.assertEqual(cycle + 1, event["review_fix_count"])
            self.move_slice("a", "pre-review-passed", touched,
                            pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(touched))])
            round_number += 1
        finding["status"] = "recurring"
        self.move_slice("a", "needs-fix", touched, review_path=self.slice_review("a", touched, round_number, [finding]))
        applied = self.save("applied-last.json", self.artifact(round=round_number, applied=[],
            failed=[{"id": "api-1", "reason": "Edited but unverified: pending"}], skipped=[]))
        with self.assertRaisesRegex(ValueError, "Slice review-fix budget exhausted"):
            self.move_slice("a", "edited-unverified", touched, action="review-fix", applied_path=applied)

    def test_continuity_limits_successor_slice_to_two_repairs(self):
        continuity = {"used_passes": 10, "effective_limit": 12}
        with patch.object(workflow, "budget_continuity", side_effect=lambda value, repo, path, slice_id=None:
                          continuity if slice_id == "a" else None):
            self.accept("a", "a.txt")
            touched = ["a.txt"]
            self.move_slice("a", "edited-unverified", touched, action="integration-fix", author_agent_id="author-a")
            self.move_slice("a", "pre-review-passed", touched,
                            pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(touched))])
            finding = {"id": "api-1", "file": "a.txt", "line": 1, "severity": "Warning", "status": "new",
                       "category": "Logic", "description": "wrong", "recommendation": "fix",
                       "failure_scenario": "x", "verification_expectation": "assert", "criterion_ids": [],
                       "verification_kind": "static"}
            for cycle in range(3):
                finding["status"] = "new" if cycle == 0 else "recurring"
                number = cycle + 2
                self.move_slice("a", "needs-fix", touched, review_path=self.slice_review("a", touched, number, [finding]))
                applied = self.save(f"continuity-applied-{cycle}.json", self.artifact(round=number, applied=[],
                    failed=[{"id": "api-1", "reason": "unverified"}], skipped=[]))
                if cycle == 2:
                    with self.assertRaisesRegex(ValueError, "Slice review-fix budget exhausted"):
                        self.move_slice("a", "edited-unverified", touched, action="review-fix", applied_path=applied)
                else:
                    result = self.move_slice("a", "edited-unverified", touched, action="review-fix", applied_path=applied)
                    self.assertEqual((cycle + 1, 2, 10), (result["review_fix_count"], result["review_fix_limit"],
                                                         result["historical_used_passes"]))
                    self.move_slice("a", "pre-review-passed", touched,
                                    pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(touched))])

    def test_slice_capsule_handoff_moves_repair_to_fresh_author_and_rejects_misuse(self):
        self.c["author_rotation"] = "capsule"
        self.save("contract.json", self.c)
        self.accept("a", "a.txt")
        touched = ["a.txt"]
        manifest = self.save("slice-capsule-manifest.json", state.capture(self.repo, touched))
        fingerprint = state.capture(self.repo, touched)["fingerprint"]

        def capsule(name, writer, digest=fingerprint):
            return self.save(name, {"schema_version": 2, "run_id": "run-1", "repo": "api", "slice_id": "a",
                                    "written_by": writer, "source_fingerprint": digest,
                                    "manifest_path": manifest})

        with self.assertRaisesRegex(ValueError, "written by the current author"):
            self.move_slice("a", "edited-unverified", touched, action="integration-fix",
                            capsule_handoff={"capsule_path": capsule("c-wrong.json", "someone"),
                                             "new_agent_id": "fresh-a"})
        with self.assertRaisesRegex(ValueError, "must change the author"):
            self.move_slice("a", "edited-unverified", touched, action="integration-fix",
                            capsule_handoff={"capsule_path": capsule("c-same.json", "author-a"),
                                             "new_agent_id": "author-a"})
        event = self.move_slice("a", "edited-unverified", touched, action="integration-fix",
                                capsule_handoff={"capsule_path": capsule("c-ok.json", "author-a"),
                                                 "new_agent_id": "fresh-a"})
        self.assertEqual("fresh-a", event["author_agent_id"])
        self.c.pop("author_rotation")
        self.save("contract.json", self.c)

    def test_author_rotation_value_is_validated(self):
        self.c["author_rotation"] = "sometimes"
        self.save("contract.json", self.c)
        with self.assertRaisesRegex(ValueError, "author_rotation"):
            workflow.contract(self.contract)
        self.c.pop("author_rotation")
        self.save("contract.json", self.c)
        workflow.contract(self.contract)

    def test_human_continuation_preserves_counters_and_grants_one_pass(self):
        from datetime import datetime, timezone
        self.test_repair_goes_to_the_slice_author_and_review_budget_is_per_slice()
        self.move_slice("a", "capped", ["a.txt"], reason="budget exhausted")
        events = workflow.history(self.run / "api/slices/a")
        path, capped = events[-1][1:]
        auth = self.artifact(slice_id="a", author_agent_id="author-a", user_authorization="User grants one pass",
            previous_state_path=str(path), previous_state_sha256=workflow.sha(path),
            approved_at=datetime.now(timezone.utc).isoformat(), additional_passes=1, finding_ids=["api-1"])
        authorization = self.save("continuations/a-1.json", auth)
        for changes in ({"additional_passes": 0}, {"additional_passes": True}, {"slice_id": "b"},
                        {"author_agent_id": "other"}, {"previous_state_sha256": "bad"},
                        {"finding_ids": []}, {"run_id": "foreign"}):
            bad = self.save("bad-continuation.json", {**auth, **changes})
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.move_slice("a", "needs-fix", ["a.txt"], continuation_path=bad)
        with self.assertRaises(ValueError):
            self.move_slice("a", "needs-fix", ["a.txt"])
        (self.repo / "a.txt").write_text("unauthorized drift")
        with self.assertRaisesRegex(ValueError, "source must be unchanged"):
            self.move_slice("a", "needs-fix", ["a.txt"], continuation_path=authorization)
        (self.repo / "a.txt").write_text("a1\n")
        resumed = self.move_slice("a", "needs-fix", ["a.txt"], continuation_path=authorization)
        self.assertEqual(3, resumed["review_fix_count"])
        self.assertEqual(capped["latest_review_round"], resumed["latest_review_round"])
        self.assertEqual(4, resumed["review_fix_limit"])
        self.assertIn(authorization, resumed["artifact_hashes"])
        applied = self.save("extra-applied.json", self.artifact(round=resumed["latest_review_round"], applied=[],
            failed=[{"id": "api-1", "reason": "Edited but unverified: fixture"}], skipped=[]))
        repaired = self.move_slice("a", "edited-unverified", ["a.txt"], action="review-fix", applied_path=applied)
        self.assertEqual(4, repaired["review_fix_count"])
        self.assertFalse(repaired["is_continuation"])
        self.assertEqual(capped, state.load(path))

    def test_accept_slice_findings_after_budget_exhaustion(self):
        from datetime import datetime, timezone
        self.test_repair_goes_to_the_slice_author_and_review_budget_is_per_slice()
        self.move_slice("a", "capped", ["a.txt"], reason="budget exhausted")
        events = workflow.history(self.run / "api/slices/a")
        path, capped = events[-1][1:]
        review_path = capped["review_path"]
        auth = self.artifact(workflow="fix", slice_id="a", approved_by="user",
            approval_question="Accept findings?", user_authorization="Accept",
            approved_at=datetime.now(timezone.utc).isoformat(), contract_sha256=workflow.sha(self.contract),
            previous_state_path=str(path), previous_state_sha256=workflow.sha(path),
            review_path=review_path, review_sha256=workflow.sha(review_path),
            review_round=capped["latest_review_round"], finding_ids=["api-1"],
            source_fingerprint=capped["source_fingerprint"])
        budget = workflow.review_budget_status(self.contract, "api", "a")
        self.assertEqual(["api-1"], budget["open_finding_ids"])
        self.assertEqual(1, budget["findings_by_severity"]["Warning"])
        approval = self.save("slice-acceptance.json", auth)
        result = self.move_slice("a", "reviewed-with-findings", ["a.txt"], review_acceptance_path=approval)
        self.assertEqual(approval, result["review_acceptance_path"])
        result = self.move_slice("a", "accepted", ["a.txt"])
        self.assertEqual("accepted", result["status"])

    def test_slice_extra_pass_acceptance_survives_repository_completion(self):
        from datetime import datetime, timezone

        def repo_move(status, **kwargs):
            events = workflow.history(self.run / "api")
            self.counter += 1
            request = self.save(f"whole-repo-request-{self.counter}.json", {
                "status": status, "source_manifest": self.cap(["a.txt", "b.txt"]),
                "scope_path": self.repo_scope, "baseline_dir": str(self.base), **kwargs})
            return workflow.transition(self.contract, "api", request,
                                       str(self.run / "api" / f"state-{len(events) + 1}.json"))

        repo_move("initialized")
        proof = self.evidence("env.runtime", "static", source=self.cap(["a.txt", "b.txt"]))
        preflight = self.save("whole-repo-preflight.json", self.artifact(status="passed", evidence_paths=[proof]))
        repo_move("ready", preflight_path=preflight)

        self.test_human_continuation_preserves_counters_and_grants_one_pass()
        self.move_slice("a", "pre-review-passed", ["a.txt"],
                        pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(["a.txt"]))])
        finding = {"id": "api-1", "file": "a.txt", "line": 1, "severity": "Warning", "status": "recurring",
                   "category": "Logic", "description": "wrong", "recommendation": "fix",
                   "failure_scenario": "x", "verification_expectation": "assert", "criterion_ids": [],
                   "verification_kind": "static"}
        latest = workflow.history(self.run / "api/slices/a")[-1][2]
        review = self.slice_review("a", ["a.txt"], latest["latest_review_round"] + 1, [finding])
        self.move_slice("a", "needs-fix", ["a.txt"], review_path=review)
        budget = workflow.review_budget_status(self.contract, "api", "a")
        self.assertEqual((4, 4, True), (budget["used_passes"], budget["limit"], budget["exhausted"]))
        self.move_slice("a", "capped", ["a.txt"], reason="additional pass exhausted")
        previous = workflow.history(self.run / "api/slices/a")[-1]
        decision = self.save("slice-extra-acceptance.json", self.artifact(
            workflow="fix", slice_id="a", approved_by="user", approval_question="Accept remaining finding?",
            user_authorization="Accept", approved_at=datetime.now(timezone.utc).isoformat(),
            contract_sha256=workflow.sha(self.contract), previous_state_path=str(previous[1]),
            previous_state_sha256=workflow.sha(previous[1]), review_path=review,
            review_sha256=workflow.sha(review), review_round=previous[2]["latest_review_round"],
            finding_ids=["api-1"], source_fingerprint=previous[2]["source_fingerprint"]))
        decision_record = state.load(decision)
        for index, changes in enumerate(({"slice_id": "b"}, {"finding_ids": []},
                                         {"approved_by": "reviewer"}, {"review_sha256": "wrong"})):
            bad = self.save(f"slice-extra-invalid-{index}.json", {**decision_record, **changes})
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.move_slice("a", "reviewed-with-findings", ["a.txt"], review_acceptance_path=bad)
        accepted = self.move_slice("a", "reviewed-with-findings", ["a.txt"], review_acceptance_path=decision)
        self.assertEqual((4, 4), (accepted["review_fix_count"], accepted["review_fix_limit"]))
        self.move_slice("a", "accepted", ["a.txt"])
        self.accept("b", "b.txt")
        gate = self.gate("whole-repo-slice-gate.json")
        matrix = self.save("whole-repo-matrix.json", self.artifact(criteria=[{
            "criterion_id": "fix.ac-1", "implementation": "slices a+b", "assertion": "whole criterion",
            "status": "unverified", "reason": "independent acceptance pending", "evidence_paths": []}]))
        repo_move("implemented", matrix_path=matrix, slice_gate_path=gate,
                  slice_authors={"a": "author-a", "b": "author-b"})
        repo_move("pre-review-passed", matrix_path=matrix, slice_gate_path=gate,
                  pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(["a.txt", "b.txt"]))])
        repo_move("reviewed", review_path=self.slice_review("a", ["a.txt", "b.txt"], 1))
        repo_move("gates-passed", gate_evidence_paths=[self.evidence("gate.tests", source=self.cap(["a.txt", "b.txt"]))])
        assessment = self.save("whole-repo-assessment.json", self.artifact(producer="orchestrator", requirements_match=True,
            criteria=[{"criterion_id": "fix.ac-1", "implementation": "met", "verification": "verified",
                       "reason": "original scenario checked", "evidence_paths": [
                           self.evidence("fix.ac-1", source=self.cap(["a.txt", "b.txt"]))]}]))
        repo_move("verified", assessment_path=assessment)
        workflow.complete(self.contract, str(self.run / "completion.json"))
        certificate = state.load(self.run / "completion.json")
        findings = certificate["repos"]["api"]["open_findings"]
        self.assertEqual([("slice/a", "api-1", decision)], [
            (item["scope"], item["id"], item["review_acceptance_path"]) for item in findings])
        self.assertIn(decision, certificate["artifact_hashes"])
        self.assertEqual("valid", workflow.validate_history(self.contract, "api")["status"])
        decision_bytes = Path(decision).read_bytes()
        Path(decision).write_bytes(decision_bytes + b" ")
        with self.assertRaisesRegex(ValueError, "Immutable artifact changed"):
            workflow.complete(self.contract, str(self.run / "completion.json"))
        Path(decision).write_bytes(decision_bytes)

    def test_slice_review_fix_allows_source_artifact_to_advance(self):
        touched = ["a.txt"]
        self.build_schedule(
            [self.slice_item("a", touched, contributions=[{"criterion_id": "fix.ac-1", "part": "part a"}])],
            cross=[]
        )

        def finding(identifier):
            return {"id": identifier, "file": "a.txt", "line": 1, "severity": "Warning", "status": "new",
                    "category": "Logic", "description": "source output incorrect", "recommendation": "repair source",
                    "failure_scenario": "source returns wrong value", "verification_expectation": "inspect corrected source",
                    "criterion_ids": ["fix.ac-1"], "verification_kind": "static"}

        def source_proof(identifier, name):
            manifest = self.cap(touched)
            path = self.repo / "a.txt"
            record = self.artifact(
                criterion_id="fix.ac-1", producer="implementer", kind="static", status="passed",
                assertion="corrected source inspected", environment="temporary repo fixture",
                data_scope="a.txt only", before_manifest=manifest, after_manifest=manifest,
                artifact_paths=[str(path)], claims=[{"id": identifier, "assertion": "source assertion checked"}],
                artifact_hashes=workflow.seal_files([manifest, str(path)])
            )
            return self.save(name + ".json", record)

        source = self.repo / "a.txt"
        source.write_text("slice implementation\n")
        self.move_slice("a", "implemented", touched, author_agent_id="author-a",
                        matrix_path=self.slice_matrix("a"))
        self.move_slice("a", "pre-review-passed", touched,
                        pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(touched))])
        self.move_slice("a", "needs-fix", touched,
                        review_path=self.slice_review("a", touched, 1, [finding("api-1")]))

        source.write_text("slice fix one\n")
        first_proof = source_proof("api-1", "slice-source-proof-1")
        first_applied = self.save("slice-applied-1.json", self.artifact(round=1, applied=[{
            "id": "api-1", "verified": "source inspected", "verification_kind": "static",
            "evidence_paths": [first_proof]}], failed=[], skipped=[]))
        self.move_slice("a", "edited-unverified", touched, action="review-fix", applied_path=first_applied)
        self.move_slice("a", "pre-review-passed", touched,
                        pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(touched))],
                        matrix_path=self.slice_matrix("a"))

        round_two_review = self.slice_review(
            "a", touched, 2, [finding("api-2")],
            [{"id": "api-1", "status": "resolved", "reason": "round-one source fix inspected",
              "evidence_paths": [first_proof]}]
        )
        self.move_slice("a", "needs-fix", touched, review_path=round_two_review)
        source.write_text("slice fix two\n")
        second_proof = source_proof("api-2", "slice-source-proof-2")
        second_applied = self.save("slice-applied-2.json", self.artifact(round=2, applied=[{
            "id": "api-2", "verified": "source inspected", "verification_kind": "static",
            "evidence_paths": [second_proof]}], failed=[], skipped=[]))
        repaired = self.move_slice("a", "edited-unverified", touched,
                                   action="review-fix", applied_path=second_applied)
        self.assertEqual("edited-unverified", repaired["status"])
        self.move_slice("a", "pre-review-passed", touched,
                        pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(touched))],
                        matrix_path=self.slice_matrix("a"))
        final_review = self.slice_review(
            "a", touched, 3, [],
            [{"id": "api-2", "status": "resolved", "reason": "round-two source fix inspected",
              "evidence_paths": [second_proof]}]
        )
        self.move_slice("a", "reviewed", touched, review_path=final_review)
        accepted = self.move_slice("a", "accepted", touched)
        self.assertEqual("accepted", accepted["status"])

    def test_repo_lifecycle_requires_a_current_slice_gate(self):
        def repo_move(status, **kwargs):
            events = workflow.history(self.run / "api")
            self.counter += 1
            request = self.save(f"repo-request-{self.counter}.json", {
                "status": status, "source_manifest": self.cap(["a.txt", "b.txt"]),
                "scope_path": self.repo_scope, "baseline_dir": str(self.base), **kwargs})
            return workflow.transition(self.contract, "api", request, str(self.run / "api" / f"state-{len(events) + 1}.json"))

        repo_move("initialized")
        proof = self.evidence("env.runtime", "static", source=self.cap(["a.txt", "b.txt"]))
        repo_move("ready", preflight_path=self.save("preflight.json", self.artifact(status="passed", evidence_paths=[proof])))
        self.accept("a", "a.txt")
        self.accept("b", "b.txt")
        matrix = self.save("repo-matrix.json", self.artifact(criteria=[{
            "criterion_id": "fix.ac-1", "implementation": "slices a+b", "assertion": "whole criterion pending",
            "status": "unverified", "reason": "independent acceptance pending", "evidence_paths": []}]))
        with self.assertRaises((ValueError, TypeError)):
            repo_move("implemented", matrix_path=matrix)  # no slice gate supplied
        gate = self.gate()
        with self.assertRaisesRegex(ValueError, "slice_authors must equal"):
            repo_move("implemented", matrix_path=matrix, slice_gate_path=gate, slice_authors={"a": "x"})
        event = repo_move("implemented", matrix_path=matrix, slice_gate_path=gate,
                          slice_authors={"a": "author-a", "b": "author-b"})
        self.assertEqual(gate, event["slice_gate_path"])
        (self.repo / "a.txt").write_text("late edit\n")
        with self.assertRaisesRegex(ValueError, "Slice gate is stale|stale|Source changed"):
            repo_move("pre-review-passed", matrix_path=matrix,
                      pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(["a.txt", "b.txt"]))])

    def test_slice_gate_binds_ignored_dependency_scope_and_rejects_drift(self):
        (self.repo / ".gitignore").write_text("vendor/\n")
        (self.repo / "vendor").mkdir()
        dep = self.repo / "vendor" / "library.js"
        dep.write_text("version one\n")
        for command in (["init", "-q"], ["add", "-A"],
                        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"]):
            subprocess.run(["git", "-C", str(self.repo), *command], check=True, capture_output=True)
        self.bases = {"a": self.cap(["a.txt"]), "b": self.cap(["b.txt"])}
        dependency_scope = self.save("dependency-scope.json", ["vendor/library.js"])
        scope = ["a.txt", "b.txt", "vendor/library.js"]
        self.initial_repo = self.cap(scope, "repo-initial-with-dep.json")

        def repo_move(status, **kwargs):
            events = workflow.history(self.run / "api")
            self.counter += 1
            request = self.save(f"repo-dep-request-{self.counter}.json", {
                "status": status, "source_manifest": self.cap(scope), "scope_path": self.repo_scope,
                "source_scope_path": dependency_scope, "baseline_dir": str(self.base), **kwargs})
            return workflow.transition(self.contract, "api", request,
                                       str(self.run / "api" / f"state-{len(events) + 1}.json"))

        repo_move("initialized")
        proof = self.evidence("env.runtime", "static", source=self.cap(scope))
        repo_move("ready", preflight_path=self.save("dep-preflight.json", self.artifact(status="passed", evidence_paths=[proof])))
        self.accept("a", "a.txt")
        self.bases["b"] = self.cap(["b.txt"])
        self.accept("b", "b.txt")
        full = self.cap(scope)
        gate_path = str(self.run / "dep-gate.json")
        workflow.slice_gate(self.contract, "api", self.schedule, self.initial_repo, full, gate_path)
        self.assertEqual(state.load(full)["fingerprint"], state.load(gate_path)["source_fingerprint"])
        matrix = self.save("dep-matrix.json", self.artifact(criteria=[{
            "criterion_id": "fix.ac-1", "implementation": "slices a+b", "assertion": "pending",
            "status": "unverified", "reason": "independent acceptance pending", "evidence_paths": []}]))
        repo_move("implemented", matrix_path=matrix, slice_gate_path=gate_path,
                  slice_authors={"a": "author-a", "b": "author-b"})
        dep.write_text("version two\n")
        with self.assertRaisesRegex(ValueError, "not owned by any accepted slice"):
            workflow.slice_gate(self.contract, "api", self.schedule, self.initial_repo,
                                self.cap(scope), str(self.run / "drift-gate.json"))

    def test_scope_amendment_preserves_history_and_rejects_unapproved_changes(self):
        """A reviewed sliced repo may add tracked paths only with a bound amendment."""
        from datetime import datetime, timezone

        (self.repo / "doc.txt").write_text("old documentation\n")
        (self.repo / "unused.txt").write_text("belongs to a slice\n")
        self.c["telemetry_policy"] = {"repair_phase_required": True}
        schedule = state.load(self.schedule)
        schedule["slices"][0]["write_scope"].append("unused.txt")
        self.save("slices-approved.json", schedule)
        self.c["slice_schedules"]["api"]["sha256"] = workflow.sha(self.schedule)
        self.save("contract.json", self.c)
        for command in (["init", "-q"], ["add", "-A"],
                        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"]):
            subprocess.run(["git", "-C", str(self.repo), *command], check=True, capture_output=True)
        self.initial_repo = self.cap(["a.txt", "b.txt"], "amend-initial.json")
        self.bases = {"a": self.cap(["a.txt"], "amend-base-a.json"),
                      "b": self.cap(["b.txt"], "amend-base-b.json")}

        def repo_move(status, scope=None, **kwargs):
            events = workflow.history(self.run / "api")
            paths = scope or self.repo_scope
            self.counter += 1
            request = self.save(f"amend-request-{self.counter}.json", {
                "status": status, "source_manifest": self.cap(state.scope_paths(paths)),
                "scope_path": paths, "baseline_dir": str(self.base), **kwargs})
            return workflow.transition(self.contract, "api", request,
                                       str(self.run / "api" / f"state-{len(events) + 1}.json"))

        repo_move("initialized")
        pre = self.save("amend-preflight.json", self.artifact(status="passed", evidence_paths=[
            self.evidence("env.runtime", "static", source=self.cap(["a.txt", "b.txt"]))]))
        repo_move("ready", preflight_path=pre)
        self.accept("a", "a.txt")
        self.bases["b"] = self.cap(["b.txt"])
        self.accept("b", "b.txt")
        gate = self.gate("amend-before-gate.json")
        matrix = self.save("amend-matrix.json", self.artifact(criteria=[{
            "criterion_id": "fix.ac-1", "implementation": "both slices", "assertion": "pending",
            "status": "unverified", "reason": "independent acceptance pending", "evidence_paths": []}]))
        repo_move("implemented", matrix_path=matrix, slice_gate_path=gate,
                  slice_authors={"a": "author-a", "b": "author-b"}, original_agent_id="original-1")
        repo_move("pre-review-passed", matrix_path=matrix, pre_review_evidence_paths=[
            self.evidence("early.tests", source=self.cap(["a.txt", "b.txt"]))])
        review = self.slice_review("a", ["a.txt", "b.txt"], 1)
        repo_move("reviewed", review_path=review)
        events = workflow.history(self.run / "api")
        anchor = events[-1][2]
        anchor_path = str(events[-1][1])
        state.baseline(self.repo, self.base, "doc.txt")
        authorization = self.artifact(approved_by="user", user_authorization="approve documentation",
            approved_at=datetime.now(timezone.utc).isoformat(), reason="repair confirmed documentation drift",
            contract_sha256=workflow.sha(self.contract), schedule_sha256=workflow.sha(self.schedule),
            previous_state_path=anchor_path, previous_state_sha256=workflow.sha(anchor_path),
            author_agent_id="original-1", before_manifest=anchor["source_manifest"],
            before_manifest_sha256=workflow.sha(anchor["source_manifest"]),
            slice_states=state.load(gate)["slices"], paths=["doc.txt"])
        approved = self.save("amendment.json", authorization)
        within_slice = self.save("amendment-within-slice.json", {**authorization, "paths": ["unused.txt"]})
        with self.assertRaisesRegex(ValueError, "belongs to an approved slice"):
            workflow.scope_amendment(self.contract, self.c, "api", within_slice,
                                     events, state.load(self.initial_repo))
        for label, change in (("contract", {"contract_sha256": "bad"}),
                              ("schedule", {"schedule_sha256": "bad"}),
                              ("state", {"previous_state_sha256": "bad"}),
                              ("slice", {"slice_states": {}}),
                              ("author", {"author_agent_id": "wrong"}),
                              ("approval", {"approved_by": "agent"}),
                              ("pattern", {"paths": ["*.txt"]}),
                              ("path", {"paths": ["other.txt"]})):
            bad = self.save(f"bad-amend-{label}.json", {**authorization, **change})
            with self.subTest(label=label), self.assertRaises((ValueError, TypeError)):
                workflow.scope_amendment(self.contract, self.c, "api", bad,
                                         events, state.load(self.initial_repo))
        backup = self.base / "doc.txt"
        backup.write_text("tampered baseline\n")
        with self.assertRaisesRegex(ValueError, "matching first-touch baseline"):
            workflow.scope_amendment(self.contract, self.c, "api", approved,
                                     events, state.load(self.initial_repo))
        backup.write_text("old documentation\n")
        (self.repo / "doc.txt").write_text("updated documentation\n")
        expanded = self.save("expanded-scope.json", ["a.txt", "b.txt", "doc.txt"])
        final = self.cap(state.scope_paths(expanded))
        amended_gate = str(self.run / "amended-gate.json")
        workflow.slice_gate(self.contract, "api", self.schedule, self.initial_repo, final, amended_gate, approved)
        self.assertEqual(["doc.txt"], state.load(amended_gate)["amended_paths"])
        pair = LifecycleTests.repair_pair(self, "amend-repair")
        with self.assertRaisesRegex(ValueError, "reviewed plan-gap repair"):
            repo_move("edited-unverified", scope=expanded, action="gate-fix", scope_amendment_path=approved,
                      slice_gate_path=amended_gate)
        with self.assertRaisesRegex(ValueError, "repair_phase_id"):
            repo_move("edited-unverified", scope=expanded, action="plan-gap", scope_amendment_path=approved,
                      slice_gate_path=amended_gate)
        event = repo_move("edited-unverified", scope=expanded, action="plan-gap", scope_amendment_path=approved,
                          slice_gate_path=amended_gate, repair_phase_id=pair)
        self.assertEqual(1, event["review_fix_count"])
        self.assertEqual(approved, event["scope_amendment_path"])
        self.assertEqual("valid", workflow.validate_history(self.contract, "api")["status"])
        with self.assertRaisesRegex(ValueError, "Cannot remove or replace scope amendment"):
            repo_move("pre-review-passed", scope=expanded, scope_amendment_path=None)
        early = self.evidence("early.tests", source=self.cap(state.scope_paths(expanded)))
        repo_move("pre-review-passed", scope=expanded, matrix_path=matrix,
                  slice_gate_path=amended_gate, pre_review_evidence_paths=[early])
        self.assertEqual("pre-review-passed", workflow.validate_history(self.contract, "api")["latest_status"])
        (self.repo / "source.txt").write_text("unapproved foreign change\n")
        with self.assertRaisesRegex(ValueError, "not owned|unapproved source"):
            workflow.slice_gate(self.contract, "api", self.schedule, self.initial_repo,
                                self.cap(state.scope_paths(expanded)), str(self.run / "foreign-gate.json"), approved)
        (self.repo / "source.txt").write_text("before\n")
        repo_review = self.slice_review("a", state.scope_paths(expanded), 2)
        repo_move("reviewed", scope=expanded, review_path=repo_review)
        repo_move("gates-passed", scope=expanded,
                  gate_evidence_paths=[self.evidence("gate.tests", source=self.cap(state.scope_paths(expanded)))])
        assessment = self.save("amended-assessment.json", self.artifact(
            producer="orchestrator", requirements_match=True, criteria=[{
                "criterion_id": "fix.ac-1", "implementation": "met", "verification": "verified",
                "reason": "fixture scenario checked", "evidence_paths": [
                    self.evidence("fix.ac-1", source=self.cap(state.scope_paths(expanded)))]}]))
        repo_move("verified", scope=expanded, assessment_path=assessment)
        workflow.complete(self.contract, str(self.run / "completion.json"))
        self.assertEqual(1, state.load(self.run / "completion.json")["repos"]["api"]["review_budget"]["used_passes"])
        original_amendment = Path(approved).read_bytes()
        Path(approved).write_text(Path(approved).read_text() + " ")
        with self.assertRaisesRegex(ValueError, "Immutable artifact changed"):
            workflow.validate_history(self.contract, "api")
        Path(approved).write_bytes(original_amendment)
        latest_slice = workflow.history(self.run / "api/slices/a")[-1][1]
        original_slice = latest_slice.read_bytes()
        latest_slice.write_text(latest_slice.read_text() + " ")
        with self.assertRaisesRegex(ValueError, "Immutable artifact changed"):
            workflow.validate_history(self.contract, "api")
        latest_slice.write_bytes(original_slice)
        slice_events = workflow.history(self.run / "api/slices/a")
        later_slice = self.run / "api/slices/a" / f"state-{len(slice_events) + 1}.json"
        later_slice.write_text(json.dumps({**slice_events[-1][2], "sequence": len(slice_events) + 1}))
        with self.assertRaisesRegex(ValueError, "accepted slices changed"):
            workflow.validate_history(self.contract, "api")
        with self.assertRaisesRegex(ValueError, "accepted slices changed"):
            workflow.complete(self.contract, str(self.run / "completion.json"))

    def start_repo_recovery(self, used_passes=0):
        (self.repo / ".gitignore").write_text("vendor/\n")
        (self.repo / "vendor").mkdir()
        self.dependency = self.repo / "vendor/library.js"
        self.dependency.write_text("dependency v1\n")
        (self.repo / "doc.txt").write_text("documentation before\n")
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"]):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)
        self.c["telemetry_policy"] = {"repair_phase_required": True}
        self.save("contract.json", self.c)
        self.dependency_scope = self.save("dependency-scope.json", ["vendor/library.js"])
        self.initial_repo = self.repo_capture()
        self.bases["a"] = self.cap(["a.txt"])
        self.repo_move("initialized", source_manifest=self.initial_repo, source_scope_path=self.dependency_scope)
        self.repo_move("ready", preflight_path=self.save("preflight.json", self.artifact(
            status="passed", evidence_paths=[self.evidence("env.runtime", "static", source=self.repo_capture())])))
        self.accept("a", "a.txt")
        self.bases["b"] = self.cap(["b.txt"])
        self.accept("b", "b.txt")
        self.old_gate_path = self.repo_gate("original-gate.json")[0]
        self.repo_matrix = self.save("repo-matrix.json", self.artifact(criteria=[{
            "criterion_id": "fix.ac-1", "implementation": "both slices", "assertion": "whole criterion",
            "status": "unverified", "reason": "independent acceptance pending", "evidence_paths": []}]))
        self.repo_move("implemented", matrix_path=self.repo_matrix, slice_gate_path=self.old_gate_path,
                       slice_authors={"a": "author-a", "b": "author-b"}, original_agent_id="original-1")
        self.findings = [{
            "id": f"api-{index}", "file": name + ".txt", "line": 1, "severity": "Warning",
            "status": "new", "category": "Logic", "description": "output is incorrect",
            "recommendation": "repair the output", "failure_scenario": "wrong output returned",
            "verification_expectation": "inspect the corrected output", "criterion_ids": ["fix.ac-1"],
            "verification_kind": "static",
        } for index, name in enumerate(("a", "b"), 1)]
        self.repo_findings(1)
        for index in range(used_passes):
            phase_id = LifecycleTests.repair_pair(self, f"prior-repair-{index}", action="review-fix")
            result = self.save(f"prior-applied-{index}.json", self.artifact(round=index + 1, applied=[], failed=[],
                skipped=[{"id": item["id"], "reason": "repair attempt left the finding open"} for item in self.findings]))
            self.repo_move("edited-unverified", action="review-fix", repair_phase_id=phase_id, applied_path=result)
            self.repo_findings(index + 2)
        self.repo_before_path, self.repo_before = workflow.history(self.run / "api")[-1][1:]
        self.original_slices = {identifier: self.slice_latest(identifier)[0] for identifier in ("a", "b")}
        self.checkpoints = {}

    def repo_capture(self, scope=None):
        return self.cap(state.scope_paths(scope or self.repo_scope) + ["vendor/library.js"])

    def repo_move(self, status, scope=None, **fields):
        request = {"status": status, "source_manifest": fields.pop("source_manifest", None) or self.repo_capture(scope),
                   **fields}
        if status == "initialized":
            request.update(scope_path=self.repo_scope, baseline_dir=str(self.base))
        if scope:
            request["scope_path"] = scope
        self.counter += 1
        request_path = self.save(f"repo-request-{self.counter}.json", request)
        sequence = len(workflow.history(self.run / "api")) + 1
        return workflow.transition(self.contract, "api", request_path,
                                   str(self.run / "api" / f"state-{sequence}.json"))

    def repo_findings(self, round_number):
        self.repo_move("pre-review-passed", pre_review_evidence_paths=[
            self.evidence("early.tests", source=self.repo_capture())])
        findings = [{**item, "status": "recurring"} for item in self.findings] if round_number > 1 else self.findings
        review_path = self.repo_review(round_number, findings=findings)
        self.repo_move("needs-fix", review_path=review_path)

    def repo_review(self, round_number, findings=None, dispositions=None, scope=None):
        return self.slice_review("whole", state.scope_paths(scope or self.repo_scope),
                                 round_number, findings, dispositions, source=self.repo_capture(scope))

    def repo_gate(self, name, scope=None, amendment=None):
        source = self.repo_capture(scope)
        path = str(self.run / name)
        workflow.slice_gate(self.contract, "api", self.schedule, self.initial_repo, source, path, amendment)
        return path, source

    def slice_latest(self, identifier):
        return workflow.history(self.run / "api" / "slices" / identifier)[-1][1:]

    @staticmethod
    def reference(path):
        return {"path": str(path), "sha256": workflow.sha(path)}

    def authorize_repo_slice_repairs(self, edit_paths=None, recertify=()):
        start = self.repo_capture()
        starting = {}
        for identifier in ("a", "b"):
            path, event = self.slice_latest(identifier)
            starting[identifier] = {"state_path": str(path), "state_sha256": workflow.sha(path),
                                    "author_agent_id": event["author_agent_id"]}
        self.repo_integration_auth = self.save(f"authorization-{self.counter}.json", self.artifact(
            user_authorization="User approves repairs to the findings of the repo review",
            approved_at=datetime.now(timezone.utc).isoformat(), contract_sha256=workflow.sha(self.contract),
            schedule_sha256=workflow.sha(self.schedule), starting_slices=starting,
            start_manifest_path=start, start_manifest_sha256=workflow.sha(start),
            edit_paths=edit_paths if edit_paths is not None else {"a": ["a.txt"], "b": ["b.txt"]},
            recertify_slices=list(recertify)))

    def repo_slice_checkpoint(self, identifier, mode):
        start = self.cap(["a.txt", "b.txt"])
        anchor, event = self.slice_latest(identifier)
        sibling = "a" if identifier == "b" else "b"
        return self.save(f"checkpoint-{identifier}-{self.counter}.json", self.artifact(
            slice_id=identifier, mode=mode, user_authorization="Approved repo integration repair",
            author_agent_id=event["author_agent_id"], previous_state_path=str(anchor),
            previous_state_sha256=workflow.sha(anchor), contract_sha256=workflow.sha(self.contract),
            schedule_sha256=workflow.sha(self.schedule), authorization_path=self.repo_integration_auth,
            authorization_sha256=workflow.sha(self.repo_integration_auth), start_manifest=start,
            start_manifest_sha256=workflow.sha(start), at=datetime.now(timezone.utc).isoformat(),
            slice_states={sibling: self.reference(self.slice_latest(sibling)[0])}))

    def repair_repo_slice(self, identifier, *, mode="repair", finish=True, checkpointed=True):
        previous = self.slice_latest(identifier)[1]
        touched = [identifier + ".txt"]
        fields = {}
        if checkpointed:
            self.checkpoints[identifier] = self.repo_slice_checkpoint(identifier, mode)
            fields["integration_path"] = self.checkpoints[identifier]
        if mode == "repair":
            (self.repo / touched[0]).write_text(identifier + "2\n")
            self.move_slice(identifier, "edited-unverified", touched, action="integration-fix", **fields)
            fields = {}
        else:
            fields["action"] = "dependency-recheck"
        self.move_slice(identifier, "pre-review-passed", touched, **fields, pre_review_evidence_paths=[
            self.evidence("early.tests", source=self.cap(touched))])
        if finish:
            self.move_slice(identifier, "reviewed", touched,
                            review_path=self.slice_review(identifier, touched, previous["latest_review_round"] + 1))
            self.move_slice(identifier, "accepted", touched)

    def prepare_repo_recovery(self, edited=("a", "b"), cite_source=False):
        gate, self.repaired_source = self.repo_gate("recovery-gate.json")
        applied = []
        skipped = []
        for item in self.findings:
            if item["file"][0] in edited:
                proof = self.evidence(item["id"], "static", source=self.repaired_source)
                if cite_source:
                    record = state.load(proof)
                    source_file = str(self.repo / item["file"])
                    record["artifact_paths"] = [source_file]
                    record["artifact_hashes"] = workflow.seal_files([self.repaired_source, source_file])
                    proof = self.save(f"source-proof-{item['id']}.json", record)
                applied.append({"id": item["id"], "verification_kind": "static", "verified": "inspected repair",
                                "evidence_paths": [proof]})
            else:
                skipped.append({"id": item["id"], "reason": "no repair attempted"})
        self.applied = self.save("recovery-applied.json", self.artifact(
            round=self.repo_before["latest_review_round"], applied=applied, failed=[], skipped=skipped))
        self.recovery = self.artifact(
            previous_state=self.reference(self.repo_before_path), review=self.reference(self.repo_before["review_path"]),
            slice_gate=self.reference(gate), applied=self.reference(self.applied),
            slices={identifier: {"previous_state": self.reference(self.original_slices[identifier]),
                                 "accepted_state": self.reference(self.slice_latest(identifier)[0]),
                                 "checkpoint": self.reference(checkpoint)}
                    for identifier, checkpoint in self.checkpoints.items()},
            finding_slices={item["id"]: [item["file"][0]] for item in self.findings if item["file"][0] in edited})
        self.recovery_path = self.save("repo-recovery.json", self.recovery)

    def recover_repo(self, **fields):
        return self.repo_move("edited-unverified", **{
            "source_manifest": self.repaired_source, "action": "review-fix",
            "slice_repair_recovery_path": self.recovery_path, **fields})

    def recovery_grant(self, **fields):
        return self.save(f"recovery-grant-{len(fields)}.json", self.artifact(**{
            "workflow": "fix", "purpose": "review-fix", "additional_passes": 1, "approved_by": "user",
            "approved_at": datetime.now(timezone.utc).isoformat(), "reason": "Complete accepted slice repairs",
            "approval_question": "Approve one repo repair pass?", "user_authorization": "Approve one pass",
            "contract_sha256": workflow.sha(self.contract), "previous_state_path": str(self.repo_before_path),
            "previous_state_sha256": workflow.sha(self.repo_before_path), "review_path": self.repo_before["review_path"],
            "review_sha256": workflow.sha(self.repo_before["review_path"]),
            "review_round": self.repo_before["latest_review_round"], "finding_ids": ["api-1", "api-2"],
            "slice_repair_recovery_sha256": workflow.sha(self.recovery_path), **fields}))

    def close_recovered_review(self):
        self.repo_move("pre-review-passed", pre_review_evidence_paths=[
            self.evidence("early.tests", source=self.repo_capture())])
        packet = self.run / "recovery-packet"
        workflow.packet(self.contract, "api", self.repo_scope, str(self.base), str(packet))
        index = state.load(packet / "index.json")
        self.assertEqual((self.applied, self.recovery_path), (index["applied_path"], index["slice_repair_recovery_path"]))
        dispositions = [{"id": item["id"], "status": "resolved", "reason": "rechecked repo repair",
                         "evidence_paths": [self.evidence(item["id"], "static", source=self.repo_capture(),
                                                          producer="reviewer")]}
                        for item in self.findings]
        next_round = self.repo_before["latest_review_round"] + 1
        slice_review = self.slice_latest("b")[1]["review_path"]
        with self.assertRaisesRegex(ValueError, "fresh independent repo review"):
            self.repo_move("reviewed", review_path=slice_review)
        review_copy = self.run / "slice-review-copy.json"
        review_copy.write_bytes(Path(slice_review).read_bytes())
        with self.assertRaisesRegex(ValueError, "fresh independent repo review"):
            self.repo_move("reviewed", review_path=str(review_copy))
        with self.assertRaisesRegex(ValueError, "closed-finding adjudication"):
            self.repo_move("reviewed", review_path=self.repo_review(next_round))
        return self.repo_move("reviewed", review_path=self.repo_review(next_round, dispositions=dispositions))

    def test_repo_recovery_after_both_slice_integration_fixes(self):
        self.start_repo_recovery()
        self.assertEqual("valid", workflow.validate_history(self.contract, "api")["status"])
        self.authorize_repo_slice_repairs()
        self.repair_repo_slice("a")
        with self.assertRaisesRegex(ValueError, "stale: predecessor a was re-accepted"):
            self.repo_gate("dependency-stale.json")
        self.repair_repo_slice("b")
        self.prepare_repo_recovery(cite_source=True)
        budget = workflow.review_budget_status(self.contract, "api")
        self.assertEqual(("needs-fix", False, ["api-1", "api-2"]),
                         (budget["latest_status"], budget["source_matches_state"], budget["open_finding_ids"]))
        self.assertIn("slice_repair_recovery_path", budget["next_step"])
        with self.assertRaisesRegex(ValueError, "use slice repair recovery"):
            self.repo_move("reviewed", review_path=self.repo_review(2))
        originals = {path: path.read_bytes() for path in self.run.rglob("*") if path.is_file()}
        recovered = self.recover_repo()
        self.assertEqual(("edited-unverified", 1, 0, 1), (recovered["status"], recovered["review_fix_count"],
                         recovered["gate_fix_count"], recovered["latest_review_round"]))
        self.assertEqual(self.repo_before["current_author_agent_id"], recovered["current_author_agent_id"])
        self.assertEqual(self.repo_before["slice_authors"], recovered["slice_authors"])
        self.assertNotIn("repair_phase_id", recovered)
        post_recovery = workflow.review_budget_status(self.contract, "api")
        self.assertTrue(post_recovery["source_matches_state"])
        self.assertEqual(["api-1", "api-2"], post_recovery["open_finding_ids"])
        with self.assertRaisesRegex(ValueError, "Illegal transition"):
            self.repo_move("reviewed", review_path=self.repo_review(2))
        with self.assertRaisesRegex(ValueError, "fresh repo pre-review"):
            self.repo_move("pre-review-passed",
                           pre_review_evidence_paths=self.slice_latest("b")[1]["pre_review_evidence_paths"])
        reviewed = self.close_recovered_review()
        self.assertEqual(2, reviewed["latest_review_round"])
        state.baseline(self.repo, self.base, "doc.txt")
        reviewed_path = workflow.history(self.run / "api")[-1][1]
        approved = self.save("scope-amendment.json", self.artifact(
            approved_by="user", user_authorization="approve additional documentation",
            approved_at=datetime.now(timezone.utc).isoformat(), reason="correct documentation for the repaired behavior",
            contract_sha256=workflow.sha(self.contract), schedule_sha256=workflow.sha(self.schedule),
            previous_state_path=str(reviewed_path), previous_state_sha256=workflow.sha(reviewed_path),
            author_agent_id=reviewed["current_author_agent_id"], before_manifest=reviewed["source_manifest"],
            before_manifest_sha256=workflow.sha(reviewed["source_manifest"]),
            slice_states=state.load(reviewed["slice_gate_path"])["slices"], paths=["doc.txt"]))
        phase = LifecycleTests.repair_pair(self, "approved-plan-gap")
        (self.repo / "doc.txt").write_text("documentation after\n")
        expanded = self.save("updated-scope.json", ["a.txt", "b.txt", "doc.txt"])
        gate, source = self.repo_gate("amended-recovery-gate.json", expanded, approved)
        self.repo_move("edited-unverified", scope=expanded, source_manifest=source, action="plan-gap",
                       scope_amendment_path=approved, slice_gate_path=gate, repair_phase_id=phase)
        self.repo_move("pre-review-passed", scope=expanded, pre_review_evidence_paths=[
            self.evidence("early.tests", source=self.repo_capture(expanded))])
        self.repo_move("reviewed", scope=expanded, review_path=self.repo_review(3, scope=expanded))
        self.repo_move("gates-passed", scope=expanded, gate_evidence_paths=[
            self.evidence("gate.tests", source=self.repo_capture(expanded))])
        assessment = self.save("recovery-assessment.json", self.artifact(
            producer="orchestrator", requirements_match=True,
            criteria=[{"criterion_id": "fix.ac-1", "implementation": "met", "verification": "verified",
                       "reason": "original scenario checked",
                       "evidence_paths": [self.evidence("fix.ac-1", source=self.repo_capture(expanded))]}]))
        self.repo_move("verified", scope=expanded, assessment_path=assessment)
        certificate = self.run / "completion.json"
        workflow.complete(self.contract, certificate)
        self.assertEqual(2, state.load(certificate)["repos"]["api"]["review_budget"]["used_passes"])
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)
        self.dependency.write_text("dependency v2 after run\n")
        (self.repo / "a.txt").unlink()
        history = workflow.validate_history(self.contract, "api")
        self.assertEqual(("verified", 2), (history["latest_status"], history["review_fix_count"]))

    def test_recovery_refuses_substituted_or_stale_checkpoint_gate_and_budget_fields(self):
        self.start_repo_recovery()
        self.authorize_repo_slice_repairs()
        self.repair_repo_slice("a")
        self.repair_repo_slice("b")
        self.prepare_repo_recovery()
        clone = self.save("unconsumed-checkpoint.json", state.load(self.checkpoints["a"]))
        changed_checkpoint = copy.deepcopy(self.recovery)
        changed_checkpoint["slices"]["a"]["checkpoint"] = self.reference(clone)
        missing = copy.deepcopy(self.recovery)
        del missing["finding_slices"]["api-2"]
        gate = state.load(self.recovery["slice_gate"]["path"])
        bad_gate = self.save("substituted-gate.json", {**gate, "slice_authors": {"a": "fake", "b": "author-b"}})
        gate_bad_hash = {**self.recovery["slice_gate"], "sha256": "wrong"}
        for label, body, message in (
                ("checkpoint", changed_checkpoint, "does not seal the supplied checkpoint"),
                ("stale-checkpoint", {**self.recovery, "slices": {
                    **self.recovery["slices"], "a": {**self.recovery["slices"]["a"],
                                                     "checkpoint": self.reference(self.checkpoints["b"])}}},
                 "checkpoint is stale"),
                ("gate-hash", {**self.recovery, "slice_gate": gate_bad_hash}, "slice_gate hash mismatch"),
                ("gate", {**self.recovery, "slice_gate": self.reference(bad_gate)}, "differs from current acceptance"),
                ("old-gate", {**self.recovery, "slice_gate": self.reference(self.old_gate_path)}, "Slice gate is stale"),
                ("findings", missing, "bind each applied finding")):
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, message):
                self.recover_repo(slice_repair_recovery_path=self.save(f"invalid-recovery-{label}.json", body))
        for key in ("review_fix_count", "review_fix_limit", "gate_fix_count", "latest_review_round"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "certified lifecycle|override lifecycle"):
                self.recover_repo(**{key: 0})
        self.authorize_repo_slice_repairs(edit_paths={}, recertify=("b",))
        self.repair_repo_slice("b", mode="dependency-recheck", finish=False)
        with self.assertRaisesRegex(ValueError, "Slice not accepted: b"):
            self.recover_repo()
        self.move_slice("b", "reviewed", ["b.txt"], review_path=self.slice_review("b", ["b.txt"], 3))
        self.move_slice("b", "accepted", ["b.txt"])
        with self.assertRaisesRegex(ValueError, "differs from current acceptance"):
            self.recover_repo()
        self.assertEqual(self.repo_before_path, workflow.history(self.run / "api")[-1][1])

    def test_recovery_refuses_unconfirmed_and_foreign_source_and_changed_dependency(self):
        self.start_repo_recovery()
        self.authorize_repo_slice_repairs()
        self.repair_repo_slice("b", checkpointed=False)
        self.prepare_repo_recovery(edited=("b",))
        with self.assertRaisesRegex(ValueError, "bind every reaccepted slice"):
            self.recover_repo()
        candidate_checkpoint = self.save("unrecorded-checkpoint.json", self.artifact(
            previous_state_path=str(self.original_slices["b"]), authorization_path=self.repo_integration_auth))
        unconfirmed = self.save("unconfirmed-recovery.json", {**self.recovery, "slices": {"b": {
            "previous_state": self.reference(self.original_slices["b"]), "checkpoint": self.reference(candidate_checkpoint),
            "accepted_state": self.reference(self.slice_latest("b")[0])}}})
        with self.assertRaisesRegex(ValueError, "does not seal the supplied checkpoint"):
            self.recover_repo(slice_repair_recovery_path=unconfirmed)
        for path, message in ((self.repo / "source.txt", "outside repo scope"),
                              (self.dependency, "frozen dependencies"),
                              (self.repo / "b.txt", "stale: b.txt changed after acceptance")):
            original = path.read_bytes()
            with self.subTest(path=path):
                path.write_text("unaccepted changes\n")
                source = self.repo_capture()
                gate = state.load(self.recovery["slice_gate"]["path"])
                gate["source_fingerprint"] = state.load(source)["fingerprint"]
                gate["artifact_hashes"].update(workflow.seal_files([source]))
                candidate = self.save(f"gate-{path.name}.json", gate)
                recovery = self.save(f"recovery-{path.name}.json",
                                     {**self.recovery, "slice_gate": self.reference(candidate)})
                with self.assertRaisesRegex(ValueError, message):
                    self.recover_repo(source_manifest=source, slice_repair_recovery_path=recovery)
                path.write_bytes(original)
        self.assertEqual(self.repo_before_path, workflow.history(self.run / "api")[-1][1])

    def test_recovery_at_cap_requires_a_bound_grant_and_counts_one_pass(self):
        self.start_repo_recovery(used_passes=3)
        self.authorize_repo_slice_repairs()
        self.repair_repo_slice("a")
        self.repair_repo_slice("b")
        self.prepare_repo_recovery()
        self.assertIn("user-approved budget grant", workflow.review_budget_status(self.contract, "api")["next_step"])
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            self.recover_repo()
        original = self.repo_before_path.read_bytes()
        self.repo_before_path.write_text(json.dumps({**self.repo_before, "review_fix_count": 0}))
        with self.assertRaisesRegex(ValueError, "counter is not derived"):
            self.recover_repo()
        self.repo_before_path.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "grant must bind the recovery"):
            self.recover_repo(budget_grant_path=self.recovery_grant(slice_repair_recovery_sha256="wrong"))
        grant = self.recovery_grant()
        recovered = self.recover_repo(budget_grant_path=grant)
        self.assertEqual((4, 4, 4), (recovered["review_fix_count"], recovered["review_fix_limit"],
                                     recovered["latest_review_round"]))
        with self.assertRaisesRegex(ValueError, "requires needs-fix"):
            self.recover_repo(budget_grant_path=grant)
        history = workflow.validate_history(self.contract, "api")
        self.assertEqual((4, 4, 1), (history["review_fix_count"], history["review_fix_limit"],
                                     history["granted_additional_passes"]))
        self.close_recovered_review()
        self.authorize_repo_slice_repairs(edit_paths={}, recertify=("b",))
        self.repair_repo_slice("b", mode="dependency-recheck")
        self.assertEqual("valid", workflow.validate_history(self.contract, "api")["status"])

    def test_recovery_includes_read_only_reacceptance_and_keeps_the_remaining_finding_open(self):
        self.start_repo_recovery()
        self.authorize_repo_slice_repairs(edit_paths={"a": ["a.txt"]}, recertify=("b",))
        self.repair_repo_slice("a")
        self.repair_repo_slice("b", mode="dependency-recheck")
        self.prepare_repo_recovery(edited=("a",))
        recovered = self.recover_repo()
        result = state.load(recovered["applied_path"])
        self.assertEqual(["api-1"], [item["id"] for item in result["applied"]])
        self.assertEqual(["api-2"], [item["id"] for item in result["skipped"]])
        self.repo_move("pre-review-passed", pre_review_evidence_paths=[
            self.evidence("early.tests", source=self.repo_capture())])
        dispositions = [{"id": "api-1", "status": "resolved", "reason": "rechecked against whole-source behavior",
                         "evidence_paths": [self.evidence("api-1", "static", source=self.repo_capture(),
                                                          producer="reviewer")]}]
        self.repo_move("needs-fix", review_path=self.repo_review(
            2, findings=[{**self.findings[1], "status": "recurring"}], dispositions=dispositions))
        budget = workflow.review_budget_status(self.contract, "api")
        self.assertEqual((1, ["api-2"]), (budget["used_passes"], budget["open_finding_ids"]))
        self.assertEqual("valid", workflow.validate_history(self.contract, "api")["status"])


class ParallelBatchTests(unittest.TestCase):
    """Shared-checkout write windows: stable start, attributed barrier, violations stop the line."""

    save, capture, artifact, evidence = (LifecycleTests.save, LifecycleTests.capture,
                                         LifecycleTests.artifact, LifecycleTests.evidence)
    cap, move_slice = SliceLifecycleTests.cap, SliceLifecycleTests.move_slice
    slice_matrix, slice_review = SliceLifecycleTests.slice_matrix, SliceLifecycleTests.slice_review

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)

    def setUp(self):
        LifecycleTests.setUp(self)
        for name in ("a.txt", "b.txt", "c.txt"):
            (self.repo / name).write_text(name[0] + "0\n")
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.c["workflow"] = "fix"
        item = lambda i, scope, deps, par: {
            "id": i, "title": i, "contributions": [{"criterion_id": "fix.ac-1", "part": "part " + i}],
            "write_scope": [scope], "depends_on": deps, "interfaces": [], "targeted_checks": [],
            "resources": [], "parallel_allowed": par}
        data = {"schema_version": 2, "run_id": "run-1", "repo": "api", "approved": True, "approved_by": "user",
                "approved_at": "2026-10-16T00:00:00Z", "max_parallel": 2,
                "cross_slice_criteria": ["fix.ac-1"],
                "slices": [item("a", "a.txt", [], True), item("b", "b.txt", [], True),
                           item("c", "c.txt", ["a", "b"], True)]}
        self.schedule = self.save("slices-approved.json", data)
        self.c["slice_schedules"] = {"api": {"path": self.schedule, "sha256": workflow.sha(self.schedule)}}
        self.save("contract.json", self.c)
        self.baselines = self.run / "batch-baselines"
        self.baselines.mkdir()
        self.bases = {k: self.cap([]) for k in "abc"}

    def start(self, members=("a", "b")):
        base = self.cap([])
        self.base_path = base
        self.bases = {k: base for k in "abc"}
        directory = self.run / "api" / "batches"
        number = len(list(directory.glob("batch-*-start.json"))) + 1 if directory.is_dir() else 1
        out = directory / f"batch-{number}-start.json"
        workflow.batch_start(self.contract, "api", self.schedule, list(members), base, str(self.baselines), [], str(out))
        return str(out)

    def barrier(self, start, statuses=None, members=("a", "b")):
        results = self.save("results.json", [{"slice_id": m, "agent_id": "agent-" + m,
                                               "status": (statuses or {}).get(m, "passed")} for m in members])
        number = state.load(start)["batch"]
        out = self.run / "api" / "batches" / f"batch-{number}-barrier.json"
        workflow.batch_barrier(self.contract, "api", self.schedule, start, self.cap([]), results, str(out))
        return str(out)

    def write(self, name, text):
        state.baseline(self.repo, self.baselines, name)
        (self.repo / name).write_text(text)

    def test_batch_refuses_two_owners_using_alternate_spellings(self):
        if not (self.repo / "A.TXT").exists():
            self.skipTest("filesystem distinguishes case")
        schedule = state.load(self.schedule)
        schedule["slices"][1]["write_scope"] = ["A.TXT"]
        self.save("slices-approved.json", schedule)
        self.c["slice_schedules"]["api"]["sha256"] = workflow.sha(self.schedule)
        self.save("contract.json", self.c)
        start = self.start()
        self.write("A.TXT", "a1\n")
        with self.assertRaisesRegex(ValueError, "several members own it"):
            self.barrier(start)

    def test_two_writers_one_window_then_independent_slice_lifecycles(self):
        start = self.start()
        self.write("a.txt", "a1\n")
        self.write("b.txt", "b1\n")
        barrier = self.barrier(start)
        record = state.load(barrier)
        self.assertEqual({"a": ["a.txt"], "b": ["b.txt"]}, record["owned"])
        self.assertEqual("closed", record["status"])
        for name in ("a", "b"):
            touched = [name + ".txt"]
            self.move_slice(name, "implemented", touched, author_agent_id="agent-" + name,
                            matrix_path=self.slice_matrix(name), batch_barrier_path=barrier)
        for name in ("a", "b"):
            touched = [name + ".txt"]
            self.move_slice(name, "pre-review-passed", touched,
                            pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(touched))])
            self.move_slice(name, "reviewed", touched, review_path=self.slice_review(name, touched, 1))
            self.assertEqual("accepted", self.move_slice(name, "accepted", touched)["status"])
        # the dependent group is a separate, single-member batch-free group
        self.write("c.txt", "c1\n")
        self.bases_c = self.cap([])

    def test_unowned_change_is_a_recorded_violation_that_blocks_the_next_batch(self):
        start = self.start()
        self.write("a.txt", "a1\n")
        self.write("c.txt", "sneaky\n")
        with self.assertRaisesRegex(ValueError, "c.txt: no member owns it"):
            self.barrier(start)
        record = state.load(self.run / "api" / "batches" / "batch-1-barrier.json")
        self.assertEqual("violation", record["status"])
        with self.assertRaisesRegex(ValueError, "previous batch ended in a violation"):
            self.start()

    def test_head_movement_missing_baseline_and_incomplete_results_are_caught(self):
        start = self.start()
        (self.repo / "a.txt").write_text("a1\n")  # edited without a first-touch baseline
        with self.assertRaisesRegex(ValueError, "Missing first-touch record"):
            self.barrier(start)
        self.setUp()
        start = self.start()
        self.write("a.txt", "a1\n")
        self.git("commit", "-qam", "moved head")
        with self.assertRaisesRegex(ValueError, "git HEAD moved"):
            self.barrier(start)
        self.setUp()
        start = self.start()
        results = self.save("results.json", [{"slice_id": "a", "agent_id": "x", "status": "passed"}])
        with self.assertRaisesRegex(ValueError, "exactly every batch member"):
            workflow.batch_barrier(self.contract, "api", self.schedule, start, self.cap([]), results,
                                   str(self.run / "api" / "batches" / "batch-1-barrier.json"))

    def test_start_rules_group_membership_open_batch_and_predecessors(self):
        with self.assertRaisesRegex(ValueError, "exactly one computed schedule group"):
            self.start(("a", "c"))
        with self.assertRaisesRegex(ValueError, "exactly one computed schedule group"):
            self.start(("a",))
        start = self.start()
        directory = self.run / "api" / "batches"
        with self.assertRaisesRegex(ValueError, "still open"):
            workflow.batch_start(self.contract, "api", self.schedule, ["a", "b"], self.base_path,
                                 str(self.baselines), [], str(directory / "batch-2-start.json"))
        self.write("a.txt", "a1\n")
        self.barrier(start, statuses={"b": "interrupted"})
        self.assertEqual("closed-incomplete", state.load(directory / "batch-1-barrier.json")["status"])

    def test_parallel_group_needs_a_closed_barrier_and_exact_touched_files(self):
        with self.assertRaises((ValueError, TypeError)):
            self.write("a.txt", "a1\n")
            self.move_slice("a", "implemented", ["a.txt"], author_agent_id="agent-a",
                            matrix_path=self.slice_matrix("a"))  # no barrier supplied
        self.setUp()
        start = self.start()
        self.write("a.txt", "a1\n")
        self.write("b.txt", "b1\n")
        barrier = self.barrier(start, statuses={"b": "failed"})
        with self.assertRaisesRegex(ValueError, "closed barrier for exactly this group"):
            self.move_slice("a", "implemented", ["a.txt"], author_agent_id="agent-a",
                            matrix_path=self.slice_matrix("a"), batch_barrier_path=barrier)


class PredecessorProvenanceTests(unittest.TestCase):
    """First implementation may carry bytes accepted by a dependency, pinned to that dependency's state."""

    save, capture, artifact, evidence = (LifecycleTests.save, LifecycleTests.capture,
                                         LifecycleTests.artifact, LifecycleTests.evidence)
    cap, move_slice = SliceLifecycleTests.cap, SliceLifecycleTests.move_slice
    slice_matrix, slice_review = SliceLifecycleTests.slice_matrix, SliceLifecycleTests.slice_review

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)

    def setUp(self):
        LifecycleTests.setUp(self)
        for name in ("a.txt", "b.txt"):
            (self.repo / name).write_text(name[0] + "0\n")
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.c["workflow"] = "fix"
        item = lambda i, scope, deps: {
            "id": i, "title": i, "contributions": [{"criterion_id": "fix.ac-1", "part": "part " + i}],
            "write_scope": [scope], "depends_on": deps, "interfaces": [], "targeted_checks": [],
            "resources": [], "parallel_allowed": False}
        data = {"schema_version": 2, "run_id": "run-1", "repo": "api", "approved": True, "approved_by": "user",
                "approved_at": "2026-10-16T00:00:00Z", "cross_slice_criteria": ["fix.ac-1"],
                "slices": [item("a", "a.txt", []), item("b", "b.txt", ["a"])]}
        self.schedule = self.save("slices-approved.json", data)
        self.c["slice_schedules"] = {"api": {"path": self.schedule, "sha256": workflow.sha(self.schedule)}}
        self.save("contract.json", self.c)
        # Base captured before any acceptance: b's implementation must see a.txt's accepted bytes as foreign.
        self.bases = {k: self.cap([]) for k in "ab"}

    def accept_a(self):
        (self.repo / "a.txt").write_text("a1\n")
        self.move_slice("a", "implemented", ["a.txt"], author_agent_id="author-a",
                        matrix_path=self.slice_matrix("a"))
        self.move_slice("a", "pre-review-passed", ["a.txt"],
                        pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(["a.txt"]))])
        self.move_slice("a", "reviewed", ["a.txt"], review_path=self.slice_review("a", ["a.txt"], 1))
        self.move_slice("a", "accepted", ["a.txt"])

    def binding(self, slice_id):
        path = workflow.history(self.run / "api" / "slices" / slice_id)[-1][1]
        return {"path": str(path), "sha256": workflow.sha(path)}

    def implement_b(self, **kwargs):
        (self.repo / "b.txt").write_text("b1\n")
        return self.move_slice("b", "implemented", ["b.txt"], author_agent_id="author-b",
                               matrix_path=self.slice_matrix("b"), **kwargs)

    def test_accepted_dependency_bytes_are_admitted_only_when_pinned(self):
        self.accept_a()
        data = self.implement_b(predecessor_states={"a": self.binding("a")})
        self.assertEqual(data["status"], "implemented")
        self.assertIn(str(Path(self.binding("a")["path"]).resolve()), data["artifact_hashes"])

    def test_unpinned_foreign_change_is_still_rejected(self):
        self.accept_a()
        with self.assertRaisesRegex(ValueError, "Changes outside"):
            self.implement_b()

    def test_predecessor_binding_must_match_current_state_bytes(self):
        self.accept_a()
        stale = {"path": self.binding("a")["path"], "sha256": "0" * 64}
        with self.assertRaisesRegex(ValueError, "Predecessor state changed"):
            self.implement_b(predecessor_states={"a": stale})

    def test_predecessor_binding_must_cover_every_dependency_exactly(self):
        self.accept_a()
        with self.assertRaisesRegex(ValueError, "needs predecessor_states"):
            self.implement_b(predecessor_states={})
        with self.assertRaisesRegex(ValueError, "bind every dependency exactly"):
            self.implement_b(predecessor_states={"a": self.binding("a"), "x": self.binding("a")})

    def test_out_of_band_edit_after_acceptance_is_not_admitted(self):
        self.accept_a()
        (self.repo / "a.txt").write_text("a2\n")
        with self.assertRaisesRegex(ValueError, "Changes outside.*a.txt"):
            self.implement_b(predecessor_states={"a": self.binding("a")})

    def test_bound_provenance_carries_forward_to_later_transitions_without_rebinding(self):
        self.accept_a()
        self.implement_b(predecessor_states={"a": self.binding("a")})
        data = self.move_slice("b", "pre-review-passed", ["b.txt"],
                               pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(["b.txt"]))])
        self.assertEqual(data["status"], "pre-review-passed")
        self.assertEqual(data["predecessor_states"], {"a": self.binding("a")})

    def test_provenance_only_accompanies_the_first_implementation(self):
        self.accept_a()
        self.implement_b(predecessor_states={"a": self.binding("a")})
        with self.assertRaisesRegex(ValueError, "only accompanies the first implementation"):
            self.move_slice("b", "pre-review-passed", ["b.txt"],
                            pre_review_evidence_paths=[self.evidence("early.tests", source=self.cap(["b.txt"]))],
                            predecessor_states={"a": self.binding("a")})


if __name__ == "__main__":
    unittest.main()
