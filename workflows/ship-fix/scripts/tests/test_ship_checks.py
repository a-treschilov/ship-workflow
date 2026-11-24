"""Isolated opt-in runner fixtures; never use product resources or live runs."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "ship_checks.py"
spec = importlib.util.spec_from_file_location("ship_checks", SCRIPT)
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)
state = checks.state
workflow = checks.workflow

RUNNER_SCRIPT = Path(__file__).resolve().parents[1] / "ship_run_checks.py"
runner_spec = importlib.util.spec_from_file_location("ship_run_checks", RUNNER_SCRIPT)
runner = importlib.util.module_from_spec(runner_spec)
runner_spec.loader.exec_module(runner)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ["SHIP_TEST_TMPDIR"], prefix="check-runner-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "source"
        self.repo.mkdir()
        (self.repo / "source.txt").write_text("before\n")
        self.run_dir = self.root / "run-1"
        self.run_dir.mkdir()
        self.scope = self.save("scope.json", ["source.txt"])
        self.path = self.run_dir / "contract.json"
        self.contract = {
            "schema_version": 2, "workflow": "fix", "run_id": "run-1", "repos": ["api"],
            "repo_paths": {"api": str(self.repo)},
            "criteria": [{"id": "fix.ac-1", "kind": "empirical", "mandatory": True,
                          "repos": ["api"], "requirement": "correct output", "source": "fixture",
                          "verification": "assert observable output", "environment": "fixture", "data_scope": "synthetic"}],
            "required_gates": {"api": [self.entry("gate.tests")]},
            "pre_review_checks": {"api": [self.entry("early.tests")]},
            "preflight_checks": {"api": [self.entry("env.tests")]},
            "required_passes": {"api": ["logic"]}, "prerequisites": [], "cross_repo_contracts": [],
            "acceptance": {"original_scenario": "fixture", "expected_behavior": "correct output",
                           "non_goals": "no side effects", "mode": "improvement", "criterion_ids": ["fix.ac-1"]},
            "telemetry_policy": {"repair_phase_required": True, "end_reason_required": True},
        }
        self.persist()

    def entry(self, identifier, code="print('ok')"):
        return {"id": identifier, "kind": "empirical", "argv": [sys.executable, "-I", "-B", "-c", code],
                "verification": "fixture command exits zero", "execution": {"cwd": ".", "timeout_seconds": 5}}

    def save(self, name, data):
        path = self.run_dir / name
        path.write_text(json.dumps(data))
        return str(path)

    def persist(self):
        self.path.write_text(json.dumps(self.contract))

    def setup_code(self, code):
        self.contract["required_gates"]["api"][0] = self.entry("gate.tests", code)
        self.persist()

    def describe(self, **kwargs):
        values = dict(contract_path=str(self.path), repo="api", group="required_gates", check_id="gate.tests",
                      scope_path=self.scope, attempt="attempt-1", environment="fixture", data_scope="synthetic")
        values.update(kwargs)
        return checks.describe(**values)

    def run_check(self, **kwargs):
        spec = self.describe(**kwargs)
        return spec, checks.run(spec, spec["spec_digest"])

    def load(self, spec, name):
        return state.load(Path(spec["output_dir"]) / name)

    def test_success_sealed_evidence_and_telemetry(self):
        spec, (code, result) = self.run_check(output_policy="redact")
        self.assertEqual(code, 0)
        self.assertEqual(result["child_returncode"], 0)
        evidence = self.load(spec, "evidence.json")
        self.assertEqual(evidence["producer"], "orchestrator")
        self.assertEqual([c["id"] for c in evidence["claims"]], ["gate.tests"])
        self.assertIn("ok", self.load(spec, "stdout.json")["text"])
        phase = self.run_dir / "telemetry" / (spec["phase_id"] + "-end.json")
        self.assertEqual(state.load(phase)["status"], "passed")
        self.assertFalse((self.run_dir / "api").exists())
        workflow.proof(self.contract, "api", result["evidence_path"], self.load(spec, "after.json"),
                       "gate.tests", "orchestrator")
        (Path(spec["output_dir"]) / "stdout.json").write_text("tamper")
        with self.assertRaises(ValueError):
            workflow.proof(self.contract, "api", result["evidence_path"], self.load(spec, "after.json"), "gate.tests")

    def test_read_exclusions_are_bound_and_apply_to_both_captures(self):
        self.contract["read_excludes"] = {"api": ["secret"]}
        self.persist()
        (self.repo / "secret").mkdir()
        (self.repo / "secret" / "private.txt").write_text("private")
        spec, (code, _) = self.run_check()
        self.assertEqual(code, 0)
        self.assertEqual(spec["read_excludes"], ["secret"])
        for name in ("before.json", "after.json"):
            self.assertNotIn("secret/private.txt", self.load(spec, name)["files"])

    def declare_external_check(self, code):
        root = self.root / "store"
        root.mkdir(exist_ok=True)
        info = root.stat()
        path = root / "input.bin"
        path.write_bytes(b"declared bytes")
        small = self.root / "small.txt"
        small.write_text("local input")
        self.contract["external_stores"] = {"data": {"path": str(root), "device": info.st_dev, "inode": info.st_ino}}
        self.contract["required_gates"]["api"] = [{**self.entry("gate.tests", code(path)),
            "input_paths": [str(path), str(small)], "external_input_stores": {str(path): "data"}}]
        self.persist()
        return path

    def test_unknown_check_does_not_mark_work_started(self):
        with self.assertRaisesRegex(SystemExit, "Unknown or non-argv"):
            runner.run_group(str(self.path), self.contract, "api", self.scope, self.run_dir / "unknown",
                             "required_gates", {"missing"}, False)
        self.assertFalse((self.run_dir / "activity-started.json").exists())

    def test_runner_refuses_output_inside_another_run(self):
        other = self.root / "plans/.fix-runs/other"
        other.mkdir(parents=True)
        (other / "setup-replacement.json").write_text("{}")
        with self.assertRaisesRegex(SystemExit, "different run"):
            runner.run_group(str(self.path), self.contract, "api", self.scope, other / "injected",
                             "preflight_checks", set(), False)
        self.assertFalse((other / "injected").exists())

    def test_runner_refuses_output_directory_swapped_to_another_run(self):
        other = self.root / "plans/.fix-runs/other"
        other.mkdir(parents=True)
        (other / "setup-replacement.json").write_text("{}")
        output = self.run_dir / "safe"
        output.mkdir()
        original_mark = workflow.mark_work_started

        def swap(contract_path, repo):
            original_mark(contract_path, repo)
            output.rename(self.run_dir / "moved")
            output.symlink_to(other, target_is_directory=True)

        with mock.patch.object(runner, "workflow_module", return_value=workflow), \
             mock.patch.object(workflow, "mark_work_started", side_effect=swap):
            with self.assertRaises((ValueError, OSError)):
                runner.run_group(str(self.path), self.contract, "api", self.scope, output,
                                 "required_gates", set(), False)
        self.assertFalse((other / "capture-scope.json").exists())

    def test_evidence_reader_refuses_swapped_excluded_output(self):
        private = self.repo / "private"
        private.mkdir()
        (private / "gate.tests-input.json").write_text("{")
        self.contract["read_excludes"] = {"api": ["private"]}
        self.persist()
        output = self.run_dir / "group"
        original_run = runner.run

        def swap(argv, **kwargs):
            if "record-evidence" in argv:
                output.rename(self.run_dir / "moved")
                output.symlink_to(private, target_is_directory=True)
            return original_run(argv, **kwargs)

        with mock.patch.object(runner, "run", side_effect=swap):
            with self.assertRaisesRegex(SystemExit, "read_excludes"):
                runner.run_group(str(self.path), self.contract, "api", self.scope, output,
                                 "required_gates", set(), False)
        self.assertFalse((private / "gate.tests-record.json").exists())

    def test_external_input_evidence_is_sealed_by_both_orchestrator_runners(self):
        path = self.declare_external_check(lambda file: f"assert open({str(file)!r}, 'rb').read() == b'declared bytes'")
        spec, (code, result) = self.run_check()
        self.assertEqual(0, code)
        out = self.run_dir / "group"
        results = runner.run_group(str(self.path), self.contract, "api", self.scope, out, "required_gates", set(), False)
        self.assertEqual(0, results["gate.tests"][0])
        value = workflow.contract(self.path)
        for evidence_path in (result["evidence_path"], str(out / "gate.tests-record.json")):
            with self.subTest(evidence=evidence_path):
                record = state.load(evidence_path)
                binding = record["input_bindings"]
                before, after = [state.load(binding[key]["path"]) for key in ("before", "after")]
                self.assertEqual(before["inputs"], after["inputs"])
                self.assertEqual(workflow.sha(path), before["inputs"][0]["sha256"])
                self.assertTrue(all(ref["path"] in record["artifact_hashes"] for ref in binding.values()))
                self.assertEqual(1, len(list(Path(binding["before"]["path"]).parent.glob("*.input"))))
                final = state.load(record["after_manifest"])
                workflow.proof(value, "api", evidence_path, final, "gate.tests", "orchestrator")
                with self.assertRaisesRegex(ValueError, "input bindings"):
                    workflow.prepare_evidence_record(self.path, "api", {k: v for k, v in record.items()
                                                                        if k != "input_bindings"})
                forged = copy.deepcopy(record)
                forged["input_bindings"]["after"] = binding["before"]
                with self.assertRaisesRegex(ValueError, "bindings differ"):
                    workflow.prepare_evidence_record(self.path, "api", forged)
        self.assertEqual(workflow.sha(path), self.load(spec, "inputs/external-before.json")["inputs"][0]["sha256"])
        path.write_bytes(b"updated bytes")
        record = state.load(result["evidence_path"])
        with self.assertRaisesRegex(ValueError, "input is stale"):
            workflow.proof(value, "api", result["evidence_path"], state.load(record["after_manifest"]), "gate.tests")
        workflow.proof(value, "api", result["evidence_path"], state.load(record["after_manifest"]), "gate.tests",
                       historical_seals=record["artifact_hashes"])

    def test_external_input_mutation_keeps_diagnostics_and_refuses_evidence(self):
        self.declare_external_check(lambda file: f"open({str(file)!r}, 'wb').write(b'changed bytes')")
        spec, (code, result) = self.run_check()
        self.assertEqual((3, 0, None), (code, result["child_returncode"], result["evidence_path"]))
        self.assertNotEqual(self.load(spec, "inputs/external-before.json")["inputs"],
                            self.load(spec, "inputs/external-after.json")["inputs"])
        out = self.run_dir / "group"
        self.declare_external_check(lambda file: f"open({str(file)!r}, 'wb').write(b'changed bytes')")
        with self.assertRaisesRegex(ValueError, "input changed during execution"):
            runner.run_group(str(self.path), self.contract, "api", self.scope, out, "required_gates", set(), False)
        self.assertEqual("0\n", (out / "gate.tests.exit").read_text())
        self.assertFalse((out / "gate.tests-record.json").exists())

    def test_failed_child_is_not_cli_exit_code(self):
        self.setup_code("import sys; sys.exit(17)")
        spec, (code, result) = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(result["child_returncode"], 17)
        self.assertEqual(self.load(spec, "evidence.json")["exit_code"], 17)
        end = state.load(self.run_dir / "telemetry" / (spec["phase_id"] + "-end.json"))
        self.assertEqual((end["status"], end["end_reason"]), ("failed", "done"))

    def test_signal_returncode(self):
        self.setup_code("import os,signal; os.kill(os.getpid(),signal.SIGTERM)")
        spec, (code, result) = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(result["child_returncode"], -signal.SIGTERM)
        self.assertEqual(self.load(spec, "evidence.json")["exit_code"], -signal.SIGTERM)

    def test_all_groups(self):
        for group, check_id in (("preflight_checks", "env.tests"), ("pre_review_checks", "early.tests")):
            with self.subTest(group=group):
                spec, (code, _) = self.run_check(group=group, check_id=check_id, attempt=group)
                self.assertEqual(code, 0)
                end = state.load(self.run_dir / "telemetry" / (spec["phase_id"] + "-end.json"))
                self.assertEqual(end["phase"], checks.GROUPS[group])

    def test_gates_writes_phase_telemetry_and_reports_failure(self):
        self.contract["required_gates"]["api"] = [
            self.entry("gate.pass", "print('ok')"),
            self.entry("gate.fail", "import sys; sys.exit(23)"),
        ]
        self.persist()
        out_dir = self.run_dir / "gates-output"
        results, status = runner.gates(str(self.path), "api", self.scope, str(out_dir), "gates-1")
        self.assertEqual("failed", status)
        self.assertEqual(0, results["gate.pass"][0])
        self.assertEqual(23, results["gate.fail"][0])
        start = state.load(self.run_dir / "telemetry/gates-1-start.json")
        end = state.load(self.run_dir / "telemetry/gates-1-end.json")
        self.assertEqual(("phase-start", "gates", "api"), (start["event"], start["phase"], start["repo"]))
        self.assertEqual(("phase-end", "failed", "done"), (end["event"], end["status"], end["end_reason"]))
        self.assertEqual(workflow.sha(str(self.run_dir / "telemetry/gates-1-start.json")),
                         end["start_sha256"])

    def add_gates_repo(self, name, code):
        other = self.root / name
        other.mkdir()
        (other / "source.txt").write_text("before\n")
        self.contract["repos"].append(name)
        self.contract["repo_paths"][name] = str(other)
        for group in ("required_gates", "pre_review_checks", "preflight_checks"):
            self.contract[group][name] = [self.entry(f"{group}-{name}", code)]
        self.contract["required_passes"][name] = ["logic"]
        self.persist()

    def write_scope_files(self, directory):
        directory.mkdir(exist_ok=True)
        for repo in self.contract["repos"]:
            (directory / f"{repo}-scope.json").write_text(json.dumps(["source.txt"]))

    def test_gates_all_repos_records_both_repos_and_failure(self):
        self.add_gates_repo("web", "import sys; sys.exit(23)")
        inputs = self.run_dir / "inputs"
        self.write_scope_files(inputs)
        out_dir = self.run_dir / "gates-all-output"
        results, status = runner.gates_all_repos(str(self.path), None, str(inputs), str(out_dir))
        self.assertEqual("failed", status)
        self.assertEqual({"api": "passed", "web": "failed"}, results)
        for repo in ("api", "web"):
            start = state.load(self.run_dir / f"telemetry/{repo}-gates-1-start.json")
            end = state.load(self.run_dir / f"telemetry/{repo}-gates-1-end.json")
            self.assertEqual((repo, "phase-start"), (start["repo"], start["event"]))
            self.assertEqual((repo, "phase-end"), (end["repo"], end["event"]))
            self.assertEqual(workflow.sha(str(self.run_dir / f"telemetry/{repo}-gates-1-start.json")),
                             end["start_sha256"])

    def test_gates_repos_cli_exit_code(self):
        self.add_gates_repo("web", "import sys; sys.exit(23)")
        inputs = self.run_dir / "inputs"
        self.write_scope_files(inputs)
        command = ["python3", "-I", "-B", str(RUNNER_SCRIPT), "gates", "--contract", str(self.path),
                   "--all-repos", "--scope-dir", str(inputs),
                   "--out-dir", str(self.run_dir / "cli-all-out")]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(1, result.returncode)
        self.assertIn("gates web: failed", result.stdout)
        self.assertIn("gates api: passed", result.stdout)

    def test_bad_selection(self):
        for kwargs in ({"repo": "unknown"}, {"group": "criteria"}, {"check_id": "unknown"}, {"attempt": "../escape"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.describe(**kwargs)

    def test_legacy_and_static_rejected_but_contract_stays_valid(self):
        original = copy.deepcopy(self.contract)
        for replacement in ({"id": "gate.tests", "kind": "static", "verification": "inspection"},
                            {"id": "gate.tests", "kind": "empirical", "command": "true", "verification": "test"}):
            self.contract = copy.deepcopy(original)
            self.contract["required_gates"]["api"][0] = replacement
            self.persist()
            workflow.contract(self.path)
            with self.assertRaises(ValueError):
                self.describe()
        self.contract = original
        del self.contract["required_gates"]["api"][0]["execution"]
        self.persist()
        workflow.contract(self.path)
        with self.assertRaises(ValueError):
            self.describe()

    def test_timeout_validation(self):
        for value in (True, False, 0, -1, 1.5, "5", 86401):
            with self.subTest(value=value):
                self.contract["required_gates"]["api"][0]["execution"]["timeout_seconds"] = value
                self.persist()
                with self.assertRaises(ValueError):
                    self.describe()

    def test_cwd_escape_and_missing(self):
        (self.repo / "outside").symlink_to(self.root, target_is_directory=True)
        for value in ("../", str(self.root), "outside", "missing"):
            self.contract["required_gates"]["api"][0]["execution"]["cwd"] = value
            self.persist()
            with self.subTest(value=value), self.assertRaises((ValueError, FileNotFoundError)):
                self.describe()

    def test_nul_rejected(self):
        self.contract["required_gates"]["api"][0]["argv"].append("a\0b")
        self.persist()
        with self.assertRaises(ValueError):
            self.describe()

    def test_argv_is_literal(self):
        arguments = ["two words", "'quotes'", "$HOME", "*.txt", "|", ";"]
        self.setup_code("import json,sys; print(json.dumps(sys.argv[1:]))")
        self.contract["required_gates"]["api"][0]["argv"].extend(arguments)
        self.persist()
        spec, (code, _) = self.run_check(output_policy="redact")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.load(spec, "stdout.json")["text"]), arguments)

    def test_digest_and_contract_drift_no_spawn(self):
        spec = self.describe()
        with mock.patch.object(checks, "execute") as execute:
            with self.assertRaises(ValueError):
                checks.run(spec, "wrong")
            self.contract["required_gates"]["api"][0]["execution"]["timeout_seconds"] = 6
            self.persist()
            with self.assertRaises(ValueError):
                checks.run(spec, spec["spec_digest"])
            execute.assert_not_called()
        self.assertFalse(Path(spec["output_dir"]).exists())

    def test_collision_before_spawn(self):
        spec = self.describe()
        Path(spec["output_dir"]).mkdir(parents=True)
        with mock.patch.object(checks, "execute") as execute, self.assertRaises(FileExistsError):
            checks.run(spec, spec["spec_digest"])
        execute.assert_not_called()

    def test_mutation_never_produces_evidence(self):
        for exit_code in (0, 7):
            self.setup_code(f"from pathlib import Path; Path('source.txt').write_text('changed-{exit_code}'); raise SystemExit({exit_code})")
            spec, (code, result) = self.run_check(attempt=f"mutate-{exit_code}")
            self.assertEqual(code, 3)
            self.assertEqual(result["outcome"], "source-changed")
            self.assertEqual(result["child_returncode"], exit_code)
            self.assertFalse((Path(spec["output_dir"]) / "evidence.json").exists())
            self.assertEqual((self.repo / "source.txt").read_text(), f"changed-{exit_code}")

    def test_capture_failure_no_spawn(self):
        spec = self.describe()
        with mock.patch.object(state, "capture", side_effect=ValueError("secret")), mock.patch.object(checks, "execute") as execute:
            code, result = checks.run(spec, spec["spec_digest"])
        self.assertEqual(code, 3)
        self.assertEqual(result["outcome"], "capture-error")
        execute.assert_not_called()
        self.assertNotIn("secret", json.dumps(result))

    def test_publication_error_is_not_passed(self):
        spec = self.describe()
        with mock.patch.object(workflow, "record_evidence", side_effect=OSError("secret")):
            code, result = checks.run(spec, spec["spec_digest"])
        self.assertEqual(code, 3)
        self.assertEqual(result["runner_exit_code"], 3)
        end = state.load(self.run_dir / "telemetry" / (spec["phase_id"] + "-end.json"))
        self.assertEqual(end["status"], "blocked")

    def test_suppression_default(self):
        self.setup_code("print('arbitrary-sensitive-value')")
        spec, (code, _) = self.run_check()
        self.assertEqual(code, 0)
        stream = self.load(spec, "stdout.json")
        self.assertTrue(stream["suppressed"])
        self.assertGreater(stream["bytes_seen"], 0)
        self.assertEqual(stream["text"], "")
        self.assertFalse(list(Path(spec["output_dir"]).glob("*.log")))

    def test_flood_both_streams(self):
        self.setup_code("import os; [(os.write(1,b'x'*8192),os.write(2,b'y'*8192)) for _ in range(64)]")
        spec, (code, _) = self.run_check(output_policy="redact")
        self.assertEqual(code, 0)
        for name in ("stdout", "stderr"):
            stream = self.load(spec, name + ".json")
            self.assertTrue(stream["truncated"])
            self.assertEqual(stream["bytes_seen"], 524288)
            self.assertLessEqual(len(stream["text"]), checks.LIMIT)

    def test_redaction_controls_and_binary(self):
        raw = b'\x1b[31mpassword=abc token=def Bearer ghi mysql://user:pwd@host\x00\xff\n'
        text = checks.sanitize(raw)
        for secret in ("abc", "def", "ghi", "user:pwd", "\x1b", "\x00"):
            self.assertNotIn(secret, text)
        self.assertIn("REDACTED", text)
        self.assertEqual(checks.sanitize(b"secret-cut-line", truncated=True), "")

    def test_timeout_kills_term_resistant_child(self):
        self.setup_code("import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)")
        self.contract["required_gates"]["api"][0]["execution"]["timeout_seconds"] = 1
        self.persist()
        spec, (code, result) = self.run_check()
        self.assertEqual(code, 3)
        self.assertEqual(result["outcome"], "timeout")
        self.assertEqual(result["child_returncode"], -signal.SIGKILL)
        self.assertFalse((Path(spec["output_dir"]) / "evidence.json").exists())
        end = state.load(self.run_dir / "telemetry" / (spec["phase_id"] + "-end.json"))
        self.assertEqual((end["status"], end["end_reason"]), ("interrupted", "timeout"))

    def test_cooperative_timeout_and_grandchild_pipes(self):
        self.setup_code("import subprocess,sys; subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(30)'])")
        self.contract["required_gates"]["api"][0]["execution"]["timeout_seconds"] = 1
        self.persist()
        spec, (code, result) = self.run_check()
        self.assertEqual(code, 3)
        self.assertIn(result["outcome"], ("timeout", "cleanup-uncertain"), result)
        self.assertFalse((Path(spec["output_dir"]) / "evidence.json").exists())

    def test_cleanup_uncertain_no_evidence(self):
        spec = self.describe()
        fake = {"outcome": "cleanup-uncertain", "child_returncode": 0, "cleanup": "uncertain"}
        with mock.patch.object(checks, "execute", return_value=(fake, {})):
            code, result = checks.run(spec, spec["spec_digest"])
        self.assertEqual(code, 3)
        self.assertIsNone(result["evidence_path"])

    def test_stale_source_and_spoofed_claims_rejected(self):
        spec, (_, result) = self.run_check()
        after = self.load(spec, "after.json")
        for key, replacement in (("command", "other"), ("producer", "implementer"), ("claims", [{"id": "wrong", "assertion": "fake"}])):
            evidence = self.load(spec, "evidence.json")
            evidence[key] = replacement
            path = self.save("spoof-" + key + ".json", evidence)
            with self.subTest(key=key), self.assertRaises(ValueError):
                workflow.proof(self.contract, "api", path, after, "gate.tests", "orchestrator")
        (self.repo / "source.txt").write_text("later")
        with self.assertRaises(ValueError):
            workflow.proof(self.contract, "api", result["evidence_path"], state.capture(self.repo, ["source.txt"]), "gate.tests")

    def test_cli_describe_run(self):
        base = [sys.executable, "-I", "-B", str(SCRIPT)]
        flags = ["--contract", str(self.path), "--repo", "api", "--group", "required_gates",
                 "--check", "gate.tests", "--scope", self.scope, "--attempt", "cli",
                 "--environment", "fixture", "--data-scope", "synthetic"]
        described = subprocess.run([*base, "describe", *flags], check=True, capture_output=True, text=True)
        spec = json.loads(described.stdout)
        completed = subprocess.run([*base, "run", *flags, "--expected-digest", spec["spec_digest"]], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["outcome"], "completed")
        self.assertEqual(self.load(spec, "finalization-result.json")["runner_exit_code"], 0)
        self.assertEqual(self.load(spec, "execution-result.json")["child_returncode"], 0)

    def test_runner_handles_sigint_and_sigterm(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.setup_code(f"import os,signal,time; os.kill(os.getppid(),{int(sig)}); time.sleep(30)")
            spec, (code, result) = self.run_check(attempt=f"interrupt-{int(sig)}")
            self.assertEqual(code, 3)
            self.assertEqual(result["outcome"], "interrupted", result)
            self.assertEqual(result["interruption_signal"], int(sig))
            self.assertEqual(result["child_returncode"], -signal.SIGTERM)
            self.assertFalse((Path(spec["output_dir"]) / "evidence.json").exists())

    def test_process_group_permission_error_is_uncertain_not_crash(self):
        with mock.patch.object(checks.os, "killpg", side_effect=PermissionError):
            self.assertTrue(checks.group_alive(123))
            self.assertFalse(checks.signal_group(123, signal.SIGTERM))

    def test_execution_result_is_hash_sealed(self):
        spec, (_, result) = self.run_check()
        path = Path(spec["output_dir"]) / "execution-result.json"
        actual = state.load(path)
        actual["child_returncode"] = 99
        path.write_text(json.dumps(actual))
        with self.assertRaises(ValueError):
            workflow.proof(self.contract, "api", result["evidence_path"], self.load(spec, "after.json"), "gate.tests")

    def test_spawn_failure_is_unknown_not_synthetic_exit(self):
        spec = self.describe()
        with mock.patch.object(checks.subprocess, "Popen", side_effect=FileNotFoundError("secret")):
            code, result = checks.run(spec, spec["spec_digest"])
        self.assertEqual(code, 3)
        self.assertEqual(result["outcome"], "spawn-error")
        self.assertIsNone(result["child_returncode"])
        self.assertIsNone(result["evidence_path"])

    def test_after_capture_failure_preserves_child_result(self):
        spec = self.describe()
        capture = state.capture
        calls = []

        def broken_after(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise ValueError("capture failed")
            return capture(*args, **kwargs)

        with mock.patch.object(state, "capture", side_effect=broken_after):
            code, result = checks.run(spec, spec["spec_digest"])
        self.assertEqual(code, 3)
        self.assertEqual(result["outcome"], "capture-error")
        self.assertEqual(result["child_returncode"], 0)
        self.assertIsNone(result["evidence_path"])

    def test_telemetry_failure_cannot_return_success(self):
        spec = self.describe()
        phase = workflow.phase

        def broken_end(contract, request, output):
            if state.load(request)["event"] == "phase-end":
                raise OSError("disk error")
            return phase(contract, request, output)

        with mock.patch.object(workflow, "phase", side_effect=broken_end):
            code, result = checks.run(spec, spec["spec_digest"])
        self.assertEqual(code, 3)
        self.assertEqual(result["runner_exit_code"], 3)
        self.assertFalse((self.run_dir / "telemetry" / (spec["phase_id"] + "-end.json")).exists())
        self.assertEqual(self.load(spec, "finalization-result.json")["runner_exit_code"], 3)

    def test_varying_environment_and_metadata_rejected(self):
        self.contract["required_gates"]["api"][0]["environment"] = "declared-runtime"
        self.persist()
        with self.assertRaises(ValueError):
            self.describe(environment="other")
        spec = self.describe(environment="declared-runtime")
        self.assertEqual(spec["environment"], "declared-runtime")

    def test_executable_symlink_invocation_is_preserved(self):
        executable = self.root / "python-link"
        executable.symlink_to(sys.executable)
        self.contract["required_gates"]["api"][0]["argv"][0] = str(executable)
        self.persist()
        spec = self.describe()
        self.assertEqual(spec["resolved_argv"][0], str(executable))
        self.assertNotEqual(spec["resolved_argv"][0], str(executable.resolve()))

    def test_contract_change_during_execution_blocks_proof(self):
        self.setup_code(f"from pathlib import Path; p=Path({str(self.path)!r}); p.write_text(p.read_text()+' ')")
        spec, (code, result) = self.run_check()
        self.assertEqual(code, 3)
        self.assertEqual(result["child_returncode"], 0)
        self.assertFalse((Path(spec["output_dir"]) / "evidence.json").exists())

    def test_incomplete_attempt_is_never_reused(self):
        spec = self.describe()
        with mock.patch.object(checks, "execute", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                checks.run(spec, spec["spec_digest"])
        self.assertTrue((self.run_dir / "telemetry" / (spec["phase_id"] + "-start.json")).exists())
        self.assertFalse((self.run_dir / "telemetry" / (spec["phase_id"] + "-end.json")).exists())
        with mock.patch.object(checks, "execute") as execute, self.assertRaises(FileExistsError):
            checks.run(spec, spec["spec_digest"])
        execute.assert_not_called()

    def test_existing_pre_review_transition_consumes_runner_evidence(self):
        initial = self.save("initial.json", state.capture(self.repo, ["source.txt"]))
        sequence = []

        def transition(status, **kwargs):
            request = self.save(f"request-{len(sequence)}.json", {
                "status": status, "source_manifest": initial, "scope_path": self.scope, **kwargs})
            output = self.run_dir / "api" / f"state-{len(sequence) + 1}.json"
            sequence.append(output)
            return workflow.transition(str(self.path), "api", request, str(output))

        transition("initialized")
        spec, (code, result) = self.run_check(group="preflight_checks", check_id="env.tests", attempt="preflight")
        self.assertEqual(code, 0)
        preflight = self.save("preflight.json", {"schema_version": 2, "run_id": "run-1", "repo": "api",
                              "status": "passed", "evidence_paths": [result["evidence_path"]]})
        transition("ready", preflight_path=preflight)
        spec, (code, result) = self.run_check(group="pre_review_checks", check_id="early.tests", attempt="early")
        self.assertEqual(code, 0)
        matrix = self.save("matrix.json", {"schema_version": 2, "run_id": "run-1", "repo": "api",
                           "criteria": [{"criterion_id": "fix.ac-1", "implementation": "fixture",
                                         "assertion": "acceptance not exercised here", "status": "unverified",
                                         "reason": "only test runner mechanics", "evidence_paths": []}]})
        transition("implemented", matrix_path=matrix, original_agent_id="fixture-author")
        event = transition("pre-review-passed", matrix_path=matrix, pre_review_evidence_paths=[result["evidence_path"]])
        self.assertEqual(event["status"], "pre-review-passed")


if __name__ == "__main__":
    unittest.main()
