"""Exercise the installed first-run path with deterministic test roles."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request

PACKAGE = Path(__file__).resolve().parents[4]


class FirstRunTests(unittest.TestCase):
    def test_pinned_install_fix_and_view(self):
        with tempfile.TemporaryDirectory(dir=os.environ["SHIP_TEST_TMPDIR"], prefix="first-run-") as temp:
            root = Path(temp).resolve()
            seed = root / "source-seed"
            source = root / "ship-workflow"
            shutil.copytree(PACKAGE, seed, ignore=shutil.ignore_patterns(".git", "__pycache__", ".DS_Store"))
            def run(command, cwd, **kwargs):
                result = subprocess.run([str(x) for x in command], cwd=cwd, capture_output=True, text=True, **kwargs)
                self.assertEqual(0, result.returncode, f"{command}\n{result.stdout}\n{result.stderr}")
                return result
            run(["git", "init", "-q"], seed)
            run(["git", "add", "."], seed)
            run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "Snapshot"], seed)
            run(["git", "clone", "-q", seed, source], root)
            sha = run(["git", "rev-parse", "--verify", "HEAD"], source).stdout.strip()
            project = root / "project"
            project.mkdir()
            run(["git", "init", "-q"], project)
            (project / ".gitignore").write_text(".agents/\n.claude/\n.codex/\nplans/\nship-view\n")
            (project / "count_lines.py").write_text("import sys\nprint(len(open(sys.argv[1]).read().split('\\n')))\n")
            (project / "empty.txt").write_text("")
            (project / "nonempty.txt").write_text("one\ntwo")
            run(["git", "add", "."], project)
            run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "Initial project"], project)
            (project / ".agents").mkdir()
            run([source / "install/install.sh", sha, "--host", "both"], project,
                env=dict(os.environ, SHIP_SRC=str(source)))
            marker = json.loads((project / ".agents/.ship-version").read_text())
            self.assertEqual((sha, sha), (marker["ref"], marker["commit"]))
            launcher = project / "ship-view"
            self.assertTrue(os.access(launcher, os.X_OK))
            helper = ".agents/workflows/ship-fix/scripts/ship_state.py"
            def cli(*args, expect_success=True):
                result = subprocess.run(["python3", "-I", "-B", helper, *map(str, args)],
                                        cwd=project, capture_output=True, text=True)
                if expect_success:
                    self.assertEqual(0, result.returncode, f"{args}\n{result.stdout}\n{result.stderr}")
                else:
                    self.assertNotEqual(0, result.returncode)
                return result
            rubric = json.loads(cli("review-rubric", "--repo-path", project).stdout)
            self.assertEqual(["logic", "security", "coverage"], rubric["ids"])
            run_dir = project / "plans/.fix-runs/first-fix"
            cli("init-run", "--run-dir", run_dir, "--workflow", "fix", "--repo", "app",
                "--description", "Count lines in an empty file")
            def save(name, value):
                path = run_dir / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value))
                return path
            scope = save("scope.json", ["count_lines.py", "test_count_lines.py"])
            base = run_dir / "app/baselines"
            for path in ("count_lines.py", "test_count_lines.py"):
                cli("baseline", "--repo", project, "--baseline", base, "--path", path)
            def capture(name):
                path = run_dir / name
                cli("capture", "--repo", project, "--scope", scope, "--out", path)
                return path
            initial = capture("initial.json")
            before_path = run_dir / "before-evidence.json"
            command = [sys.executable, "-I", "-B", "test_count_lines.py"]
            check = lambda name: {"id": name, "kind": "empirical", "argv": command, "verification": "regression assertions pass"}
            contract = {"schema_version": 2, "workflow": "fix", "run_id": "first-fix", "repos": ["app"],
                "repo_paths": {"app": str(project)}, "telemetry_policy": {"repair_phase_required": True},
                "author_rotation": "capsule", "budget_work_items": {"app": "empty-line-count"},
                "criteria": [{"id": "fix.ac-1", "kind": "empirical", "mandatory": True, "repos": ["app"],
                    "requirement": "Count zero lines for empty input and preserve nonempty counts", "source": "brief",
                    "verification": "assert exact outputs", "environment": "temporary project", "data_scope": "fixtures"}],
                "required_gates": {"app": [check("gate.tests")]},
                "pre_review_checks": {"app": [check("early.tests")]},
                "preflight_checks": {"app": [{"id": "env.runtime", "kind": "static", "verification": "inspect project and interpreter"}]},
                "required_passes": {"app": rubric["ids"]}, "prerequisites": [], "cross_repo_contracts": [],
                "acceptance": {"original_scenario": "empty file", "expected_behavior": "zero lines", "non_goals": "preserve nonempty counts",
                    "mode": "reproduced-bug", "criterion_ids": ["fix.ac-1"], "before_evidence": {"app": str(before_path)}}}
            contract_path = save("contract.json", contract)
            cli("validate-run", "--contract", contract_path, "--attempt-id", "first")
            def artifact(**kw):
                return {"schema_version": 2, "run_id": "first-fix", "repo": "app", **kw}
            def evidence(name, identifier, manifest, *, status, output, command_text, kind="empirical", scenario=None, after=None):
                log = run_dir / (name + ".log")
                log.write_text(output)
                data = artifact(criterion_id=identifier, claims=[{"id": identifier, "assertion": "Observed fixture result"}],
                    kind=kind, producer="orchestrator", status=status, command=command_text,
                    assertion="Observed fixture result", exit_code=0 if status == "passed" else 1,
                    environment="temporary project", data_scope="fixtures", before_manifest=str(manifest),
                    after_manifest=str(after or manifest), artifact_paths=[str(log)], reason="fixture failed" if status == "failed" else "")
                if scenario:
                    data["scenario"] = scenario
                input_path = save(name + "-input.json", data)
                out = run_dir / (name + "-evidence.json")
                cli("record-evidence", "--contract", contract_path, "--repo", "app", "--file", input_path, "--out", out)
                return out
            before_command = [sys.executable, "-I", "-B", "-c",
                "import subprocess,sys; result=subprocess.check_output([sys.executable, 'count_lines.py', 'empty.txt'], text=True).strip(); print(result); assert result == '0'"]
            before = subprocess.run(before_command, cwd=project, capture_output=True, text=True)
            self.assertEqual(1, before.returncode)
            self.assertEqual("1", before.stdout.strip())
            after_before = capture("after-before.json")
            evidence("before", "fix.ac-1", initial, status="failed", output=before.stdout + before.stderr,
                     command_text=__import__("shlex").join(before_command), scenario="empty file", after=after_before)
            counter = 0
            def move(status, **kwargs):
                nonlocal counter
                counter += 1
                manifest = capture(f"manifest-{counter}.json")
                request = save(f"request-{counter}.json", {"status": status, "source_manifest": str(manifest),
                    "scope_path": str(scope), "baseline_dir": str(base), **kwargs})
                cli("transition", "--contract", contract_path, "--repo", "app", "--file", request,
                    "--out", run_dir / f"app/state-{counter}.json")
                return manifest
            def phase(name, event, agent_id=None):
                args = ["phase", "--contract", contract_path, "--repo", "app", "--phase-id", name,
                        "--phase", name, "--event", "phase-" + event,
                        "--out", run_dir / f"telemetry/{name}-{event}.json"]
                if event == "end":
                    args.extend(["--status", "passed", "--end-reason", "done"])
                if agent_id:
                    args.extend(["--agent-id", agent_id])
                cli(*args)
            move("initialized")
            phase("preflight", "start")
            pre_evidence = evidence("preflight", "env.runtime", initial, status="passed", output=str(project),
                                    command_text="inspect runtime", kind="static")
            preflight = save("preflight.json", artifact(status="passed", evidence_paths=[str(pre_evidence)]))
            phase("preflight", "end")
            move("ready", preflight_path=str(preflight))
            artifacts = ".agents/workflows/ship-fix/scripts/ship_artifacts.py"
            task = run_dir / "task.json"
            run(["python3", "-I", "-B", artifacts, "init-task", "--contract", contract_path,
                 "--repo", "app", "--touched", scope, "--baseline", base, "--scope-file", scope,
                 "--evidence-dir", run_dir / "app/evidence", "--out", task], project)
            phase("implement", "start")
            run(["python3", "-I", "-B", artifacts, "--task", task, "begin-edit",
                 "count_lines.py", "test_count_lines.py"], project)
            # Deterministic implementer role changes the source and supplies executable assertions.
            (project / "count_lines.py").write_text("import sys\nprint(len(open(sys.argv[1]).read().splitlines()))\n")
            (project / "test_count_lines.py").write_text(
                "import subprocess, sys\n"
                "def count(path):\n    return subprocess.check_output([sys.executable, 'count_lines.py', path], text=True).strip()\n"
                "assert count('empty.txt') == '0'\nassert count('nonempty.txt') == '2'\nprint('fixture assertions passed')\n")
            matrix = save("matrix.json", artifact(criteria=[{"criterion_id": "fix.ac-1", "implementation": "count_lines.py",
                "assertion": "exact empty and nonempty outputs", "status": "unverified", "reason": "independent acceptance pending", "evidence_paths": []}]))
            capsule = save("capsule-draft.json", {"task": "Fix the empty file count", "decisions": ["Use splitlines"],
                "invariants": ["Keep nonempty counts"], "files": ["count_lines.py", "test_count_lines.py"],
                "pitfalls": [], "verify": ["Run fixture assertions"], "do_not_touch": [], "open_items": []})
            run(["python3", "-I", "-B", artifacts, "capsule-check", capsule], project)
            phase("implement", "end", "test-implementer")
            run(["python3", "-I", "-B", artifacts, "--task", task, "capsule", capsule,
                 "--agent-id", "test-implementer"], project)
            implemented = move("implemented", matrix_path=str(matrix), original_agent_id="test-implementer")
            cli("validate-submission", "--contract", contract_path, "--repo", "app", "--matrix", matrix, "--manifest", implemented)
            def test_proof(name, identifier):
                before = capture(name + "-before.json")
                result = run(command, project)
                after = capture(name + "-after.json")
                return evidence(name, identifier, before, status="passed", output=result.stdout,
                                command_text=__import__("shlex").join(command), scenario="empty file", after=after)
            phase("pre-review", "start")
            early = test_proof("early", "early.tests")
            phase("pre-review", "end")
            move("pre-review-passed", pre_review_evidence_paths=[str(early)])
            packet_dir = run_dir / "packet"
            cli("packet", "--contract", contract_path, "--repo", "app", "--scope", scope,
                "--baseline", base, "--out", packet_dir)
            packet = json.loads((packet_dir / "index.json").read_text())
            self.assertEqual(rubric["ids"], packet["required_passes"])
            selected = json.loads(cli("review-rubric", "--contract", contract_path, "--repo", "app").stdout)
            self.assertEqual(rubric["ids"], selected["ids"])
            manifest = capture("review-manifest.json")
            fingerprint = json.loads(manifest.read_text())["fingerprint"]
            phase("review", "start")
            # Deterministic reviewer inspects both changed files and all selected passes.
            coverage = [{"id": item, "status": "inspected", "reason": "inspected fixture code and assertions",
                         "sites": ["count_lines.py", "test_count_lines.py"]} for item in rubric["ids"]]
            review = save("review.json", artifact(round=1, source_fingerprint=fingerprint, accepted=True,
                coverage={"read": ["count_lines.py", "test_count_lines.py"], "skipped": [], "mandatory_passes": coverage},
                findings=[], dispositions=[]))
            incomplete = save("incomplete-review.json", artifact(round=1, source_fingerprint=fingerprint, accepted=True,
                coverage={"read": ["count_lines.py", "test_count_lines.py"], "skipped": [], "mandatory_passes": coverage[:-1]},
                findings=[], dispositions=[]))
            bad_request = save("incomplete-request.json", {"status": "reviewed", "source_manifest": str(manifest),
                "scope_path": str(scope), "baseline_dir": str(base), "review_path": str(incomplete)})
            rejected = cli("transition", "--contract", contract_path, "--repo", "app", "--file", bad_request,
                           "--out", run_dir / f"app/state-{counter + 1}.json", expect_success=False)
            self.assertIn("Missing rubric pass", rejected.stderr)
            phase("review", "end", "test-reviewer")
            move("reviewed", review_path=str(review))
            phase("gates", "start")
            gate = test_proof("gate", "gate.tests")
            phase("gates", "end")
            move("gates-passed", gate_evidence_paths=[str(gate)])
            proof = test_proof("acceptance", "fix.ac-1")
            assessment = save("assessment.json", artifact(producer="orchestrator", requirements_match=True,
                criteria=[{"criterion_id": "fix.ac-1", "implementation": "met", "verification": "verified",
                    "reason": "exact fixture outputs asserted", "evidence_paths": [str(proof)]}]))
            move("verified", assessment_path=str(assessment))
            completion = run_dir / "completion.json"
            cli("complete", "--contract", contract_path, "--out", completion)
            self.assertEqual("completed", json.loads(completion.read_text())["status"])
            target = root / "separate-target"
            project_rubric = target / ".agents/skills/review/SKILL.md"
            project_rubric.parent.mkdir(parents=True)
            project_rubric.write_text("# Project rubric\n\n## project-check\nCheck the project invariant.\n")
            separate_run = project / "plans/.fix-runs/separate"
            cli("init-run", "--run-dir", separate_run, "--workflow", "fix", "--repo", "app",
                "--description", "Check separate target")
            separate_contract = separate_run / "contract.json"
            separate_contract.write_text(json.dumps({**contract, "run_id": "separate", "repo_paths": {"app": str(target)}}))
            override = json.loads(cli("review-rubric", "--contract", separate_contract, "--repo", "app").stdout)
            self.assertEqual("project", override["source"])
            self.assertEqual(project_rubric.read_text(), override["content"])
            viewer = subprocess.Popen([str(launcher), "--no-open", "--port", "0"], cwd=project,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                line = viewer.stdout.readline()
                url = line.split()[2]
                with urllib.request.urlopen(url + "api/runs", timeout=10) as response:
                    runs = json.load(response)
                self.assertTrue(any(item["id"] == "first-fix" for item in runs))
            finally:
                viewer.terminate()
                viewer.communicate(timeout=10)
