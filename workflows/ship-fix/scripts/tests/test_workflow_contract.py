"""Instruction integration and historical compatibility; behavior lives in lifecycle tests."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest

HERE = Path(__file__).resolve()
INSTALLED = HERE.parents[4].name == ".agents"
ROOT = HERE.parents[5] if INSTALLED else HERE.parents[4]
SHARED = ROOT / (".agents/workflows/ship-fix" if INSTALLED else "workflows/ship-fix")
SKILLS = ROOT / (".agents/skills" if INSTALLED else "skills")
ROLES = ROOT / (".agents/roles" if INSTALLED else "roles")
spec = importlib.util.spec_from_file_location("ship_analyze", SKILLS / "ship-analyze/analyze.py")
analyzer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analyzer)


class InstructionContractTests(unittest.TestCase):
    def setUp(self):
        self.ship = (SKILLS / "ship/SKILL.md").read_text()
        self.fix = (SKILLS / "fix/SKILL.md").read_text()
        self.protocol = (SHARED / "PROTOCOL.md").read_text()
        self.host_adapters = (SHARED / "HOST-ADAPTERS.md").read_text()
        self.agents = {name: (ROLES / f"{name}.md").read_text()
                       for name in ("implementer", "reviewer", "plan-checker", "slicer")}

    def test_session_capability_scenarios_and_setup_order(self):
        scenarios = (
            ("`wait_agent(targets)` delivers final result", "Proceed using targeted wait."),
            ("final message with sender ID", "Proceed using the identified final message."),
            ("no named role selector", "Stop: named role selection is unavailable."),
            ("fresh context is not guaranteed", "Stop: fresh context is unverified."),
            ("only status or acknowledgement is available", "Stop: identified result is unavailable."),
        )
        for interface, decision in scenarios:
            with self.subTest(interface=interface):
                self.assertTrue(any(interface in line and decision in line
                                    for line in self.host_adapters.splitlines()))
        for skill in (self.ship, self.fix):
            self.assertLess(skill.index("session capability check"), skill.index("init-run"))
            self.assertIn("Repeat it on resume", skill)
        self.assertIn("cannot see session tools", self.host_adapters)
        self.assertIn("Do not launch a paid agent", self.host_adapters)
        self.assertIn("An update from\n  another agent does not complete", self.host_adapters)
        self.assertIn("Match the sender against\n  the recorded dispatch ID", self.host_adapters)

    def test_adapters_use_portable_paths_and_absolute_handoffs(self):
        for command in (self.ship, self.fix):
            self.assertIn(".agents/workflows/ship-fix/PROTOCOL.md", command)
            self.assertIn("absolute PROTOCOL_PATH", command)
            self.assertIn("absolute HELPER_PATH", command)
            self.assertIn("python3 -I -B .agents/workflows/ship-fix/scripts/<script>.py", command)
            self.assertIn("project root", command)
            self.assertNotIn(str(ROOT.parent), command)
            self.assertIn("independent review", command)
            self.assertIn("preflight", command)
        for agent in self.agents.values():
            self.assertIn("PROTOCOL_PATH", agent)
            self.assertIn("CONTRACT_PATH", agent)
        for path in ("PROTOCOL.md", "scripts/ship_state.py", "scripts/ship_workflow.py"):
            self.assertTrue((SHARED / path).is_file())

    @unittest.skipUnless(INSTALLED, "project installation only")
    def test_platform_links_point_to_one_shared_source(self):
        for host_path, shared_path in (
            (ROOT / ".claude/skills/ship", SKILLS / "ship"),
            (ROOT / ".claude/skills/fix", SKILLS / "fix"),
            (ROOT / ".claude/skills/ship-analyze", SKILLS / "ship-analyze"),
            (ROOT / ".claude/scripts", SHARED / "scripts"),
            (ROOT / ".codex/scripts", SHARED / "scripts"),
            (ROOT / ".claude/hooks", SHARED / "hooks"),
            (ROOT / ".codex/hooks", SHARED / "hooks"),
            (ROOT / ".claude/references/ship-protocol.md", SHARED / "PROTOCOL.md"),
        ):
            with self.subTest(host_path=host_path):
                self.assertTrue(host_path.is_symlink())
                self.assertEqual(shared_path.resolve(), host_path.resolve())
        self.assertTrue((SKILLS / "ship/SKILL.md").is_file())
        self.assertTrue((SKILLS / "fix/SKILL.md").is_file())
        self.assertIn("Claude Code's agent tool", self.host_adapters)
        self.assertIn("Codex's custom subagent dispatch", self.host_adapters)

    def test_workflow_helpers_are_standalone_from_project_root(self):
        documents = {"protocol": self.protocol, "ship": self.ship, "fix": self.fix, **self.agents}
        for name, text in documents.items():
            lower = text.lower()
            with self.subTest(document=name):
                self.assertIn("python3 -i -b", lower)
                self.assertIn(".agents/workflows/ship-fix/scripts/", lower)
                self.assertIn("heredoc", lower)
                self.assertIn("pipeline", lower)
                self.assertIn("redirection", lower)
                self.assertIn("unrelated", lower)
        for name, text in {"ship": self.ship, "fix": self.fix}.items():
            with self.subTest(adapter=name):
                self.assertIn("Repeat this exact constraint in every agent dispatch and resume prompt", text)
                self.assertIn("file-writing capability for JSON inputs", text)
        self.assertIn("in a separate\nshell invocation", self.protocol)
        self.assertIn("Do not retrieve RUN_DIR/TASK_PATH via scratchpad pointer files", self.protocol)
        self.assertIn("RUN_DIR", self.ship)
        self.assertIn("TASK_PATH", self.protocol)
        self.assertIn("historical_budget_sources", self.protocol)
        self.assertIn("Historical runs are read-only", self.protocol)
        for command in (self.ship, self.fix):
            self.assertIn("user-input capability", command)
            self.assertIn("budget_grant_path", command)
            self.assertIn("grants accumulate", command)
            self.assertIn("Gate-fix", command)
            self.assertIn("review-budget", command)
            self.assertIn("init-task", command)
        self.assertIn("Check a repair budget", self.host_adapters)

    def test_documented_helper_commands_exist(self):
        result = subprocess.run(["python3", "-I", "-B", str(SHARED / "scripts/ship_state.py"), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        for command in ("validate-contract", "validate-history", "review-budget", "transition", "record-evidence", "packet", "phase", "complete"):
            self.assertIn(command, result.stdout)
            self.assertIn(command, self.protocol)

    def test_contract_example_is_valid_json_with_shared_requirements(self):
        example = self.protocol.split("```json\n", 1)[1].split("```", 1)[0]
        value = json.loads(example)
        self.assertEqual(2, value["schema_version"])
        self.assertTrue(value["criteria"])
        for group in ("required_gates", "pre_review_checks", "preflight_checks", "required_passes"):
            self.assertEqual(set(value["repos"]), set(value[group]))

    def test_prospective_repair_policy_and_recording_contract(self):
        example = json.loads(self.protocol.split("```json\n", 1)[1].split("```", 1)[0])
        self.assertEqual({"repair_phase_required": True}, example["telemetry_policy"])
        for command in (self.ship,):
            for required in ("telemetry_policy", "repair_phase_required", "original_agent_id",
                             "phase-start before the host's continuation capability", "specific actual result", "phase-end",
                             "delivery acknowledgement is not completion", "previous_state_path",
                             "previous_state_sha256", "repair_phase_id", "outside telemetry/",
                             "action=plan-gap", "author_handoff", "immutable", "snapshot"):
                self.assertIn(required, command.replace("delivery\nacknowledgement", "delivery acknowledgement"))
        for required in ("old v2 contracts without policy", "does not mechanically prove host's continuation capability order",
                         "Failed, blocked", "interrupted", "start_sha256", "never fabricate an end",
                         "CLI inputs", "No new dispatch wrapper or hook"):
            self.assertIn(required, self.protocol)

    def test_role_separation_and_scope_precedence(self):
        reviewer = self.agents["reviewer"]
        self.assertIn("not its standalone HEAD-diff", reviewer)
        self.assertIn("Hash equality is not impact analysis", reviewer)
        self.assertIn("Unrelated pre-existing Boy Scout", reviewer)
        self.assertIn("producer reviewer", reviewer)
        self.assertIn("read-only", self.agents["plan-checker"])
        self.assertIn("not another general code review", self.protocol)
        self.assertIn("not a convenient proxy green test", self.agents["plan-checker"])

    @unittest.skipUnless(INSTALLED, "project installation only")
    def test_models_are_centralized_and_checked_before_dispatch(self):
        config = (ROOT / ".agents/agent-models.yaml").read_text()
        model_spec = importlib.util.spec_from_file_location("contract_agent_models", SHARED / "scripts/agent_models.py")
        model_config = importlib.util.module_from_spec(model_spec)
        model_spec.loader.exec_module(model_config)
        models, _available, aliases = model_config.parse_config()
        for role in ("implementer", "reviewer", "plan-checker", "slicer"):
            self.assertTrue(models.get(role), f"Missing model assignment: {role}")
        self.assertEqual([], model_config.check(models, aliases))
        for role, model in models.items():
            codex = tomllib.loads((ROOT / ".codex/agents" / f"{role}.toml").read_text())
            claude = (ROOT / ".claude/agents" / f"{role}.md").read_text()
            self.assertEqual(model, codex["model"])
            self.assertIn(f"model: {aliases[model]}\n", claude)
        self.assertFalse((ROOT / ".claude/agent-models.yaml").exists())
        self.assertFalse((ROOT / ".claude/agent-models.yaml").is_symlink())
        self.assertIn("available_models:", config)
        for command in (self.ship,):
            self.assertIn("agent_models.py check", command)
            self.assertIn("stop;", command)
        self.assertIn("agent_models.py sync", self.protocol)
        self.assertIn("agents share the source", self.protocol)
        self.assertIn("--from-review", self.ship)
        self.assertIn("no implementer to resume", self.ship)

    def test_no_automatic_checker_for_small_fix_or_premature_archive(self):
        self.assertIn("no automatic checker call on every tiny fix", self.fix)
        self.assertIn("leave master", self.ship)
        self.assertIn("Only after valid completion.json", self.ship)
        self.assertIn("never verified/completed/archive", self.protocol)

    @unittest.skipIf(INSTALLED, "package documentation only")
    def test_ai_entry_points_and_documentation_rule(self):
        guidance = (ROOT / "AGENTS.md").read_text()
        self.assertIn("Documentation is part of the change", guidance)
        self.assertIn("make test", guidance)
        self.assertTrue((ROOT / "CLAUDE.md").is_symlink())
        self.assertEqual((ROOT / "AGENTS.md").resolve(), (ROOT / "CLAUDE.md").resolve())
        for name in ("README.md", "ARCHITECTURE.md", "INSTALLATION.md", "DEVELOPMENT.md", "AI-CONTEXT.md"):
            with self.subTest(document=name):
                self.assertTrue((ROOT / "docs" / name).is_file())


class AnalyzerCompatibilityTests(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get("SHIP_TEST_TMPDIR")
        if not scratch:
            self.fail("Set SHIP_TEST_TMPDIR to the session scratchpad")
        self.temp = tempfile.TemporaryDirectory(dir=scratch, prefix="ship-history-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "api"
        self.repo.mkdir()

    def save(self, name, data):
        (self.repo / name).write_text(json.dumps(data))

    def clean(self, round_number=1, fingerprint=None):
        data = {"repo": "api", "round": round_number, "findings": []}
        if fingerprint:
            data["source_fingerprint"] = fingerprint
        self.save(f"round-{round_number}-findings.json", data)

    def test_legacy_clean_history_remains_supported_but_uncertified(self):
        self.clean()
        ladder = analyzer.scan_run_history(self.root)["api"]
        self.assertTrue(ladder["converged"])
        self.assertFalse(ladder["review_certified"])
        self.assertFalse(ladder["goals_verified"])
        self.assertEqual("legacy/uncertified", ladder["certification"])

    def test_rejected_clean_review_is_not_convergence(self):
        self.clean(fingerprint="before")
        self.save("round-1-meta.json", {"accepted": False})
        self.assertFalse(analyzer.scan_run_history(self.root)["api"]["converged"])

    def test_gate_fix_after_clean_review_invalidates_convergence(self):
        self.clean(fingerprint="before")
        self.save("state-1.json", {"status": "edited-unverified", "source_fingerprint": "after",
                                   "latest_review_round": 1})
        self.assertFalse(analyzer.scan_run_history(self.root)["api"]["converged"])

    def test_final_verification_round_is_recognized(self):
        for number in (1, 2, 3):
            self.save(f"round-{number}-findings.json", {"repo": "api", "round": number,
                      "findings": [{"id": "api-1", "severity": "Suggestion", "status": "recurring"}]})
        self.clean(4, "final")
        self.save("round-4-meta.json", {"accepted": True})
        self.save("state-2.json", {"status": "verified", "source_fingerprint": "final", "latest_review_round": 4})
        ladder = analyzer.scan_run_history(self.root)["api"]
        self.assertTrue(ladder["converged"])
        self.assertTrue(ladder["goals_verified"])
        self.assertEqual(4, ladder["rounds_used"])
        suggestions = analyzer.build_suggestions([], {"api": ladder}, {}, [])
        self.assertFalse(any("Consider ending the loop" in text for text in suggestions))

    def test_numeric_state_order_and_stale_fingerprint(self):
        self.clean(fingerprint="source")
        self.save("state-2.json", {"status": "verified", "source_fingerprint": "source"})
        self.save("state-10.json", {"status": "blocked", "source_fingerprint": "different"})
        ladder = analyzer.scan_run_history(self.root)["api"]
        self.assertEqual("blocked", ladder["final_state"])
        self.assertFalse(ladder["converged"])

    def test_missing_or_malformed_findings_is_not_clean(self):
        self.save("round-1-applied.json", {"applied": [], "failed": [], "skipped": []})
        self.assertFalse(analyzer.scan_run_history(self.root)["api"]["converged"])
        self.clean()
        self.save("round-2-findings.json", {"repo": "api"})
        self.assertFalse(analyzer.scan_run_history(self.root)["api"]["converged"])

    def test_new_frontend_gates_are_recognized(self):
        self.assertIn("npm run test:ci", analyzer.GATE_COMMANDS)
        self.assertIn("npm run build", analyzer.GATE_COMMANDS)


if __name__ == "__main__":
    unittest.main()
