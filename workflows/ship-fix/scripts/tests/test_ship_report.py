"""Canonical accounting and historical certification must survive presentation."""

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve()
INSTALLED = HERE.parents[4].name == ".agents"
WORKSPACE = HERE.parents[5] if INSTALLED else HERE.parents[4]
SKILLS = WORKSPACE / (".agents/skills" if INSTALLED else "skills")
DIRECTORY = SKILLS / "ship-analyze"


def load(name):
    spec = importlib.util.spec_from_file_location("test_" + name, DIRECTORY / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report = load("report")
analyzer = load("analyze")


class ReportTests(unittest.TestCase):
    def test_completed_report_includes_open_findings(self):
        data = self.sample()
        data["telemetry"]["run_completed"] = True
        data["insights"] = {"open_findings": [{"repo": "api", "scope": "slice/a", "id": "api-1",
            "severity": "Warning", "category": "Logic", "file": "a.py", "line": 12,
            "description": "Wrong output", "failure_scenario": "Empty input fails", "accepted_by_user": True}]}
        text = report.compact(data)
        self.assertIn("завершено с открытыми замечаниями", text)
        self.assertIn("Warning: 1", text)
        self.assertIn("slice/a / api-1", text)
        self.assertIn("Empty input fails", text)

    def test_completed_report_displays_user_deferral_reason(self):
        data = self.sample()
        data["telemetry"]["run_completed"] = True
        data["insights"] = {"open_findings": [{"repo": "api", "scope": "repo", "id": "api-1",
            "severity": "Suggestion", "category": "Logging", "file": "api.py", "line": 12,
            "description": "Add a diagnostic log", "failure_scenario": "Diagnosis takes longer",
            "accepted_by_user": False, "deferral": {"reason": "Later task", "review_round": 1}}]}
        rendered = report.compact(data)
        self.assertIn("завершено с открытыми замечаниями", rendered)
        self.assertIn("Отложено пользователем, раунд 1", rendered)
        self.assertIn("Причина: Later task", rendered)

    def test_exact_run_name_precedes_ambiguous_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'run').mkdir()
            (root / 'run-other').mkdir()
            with patch.object(analyzer, 'RUNS_DIR', root):
                self.assertEqual('run', analyzer.resolve_run('run')[0].name)
                with self.assertRaises(SystemExit):
                    analyzer.resolve_run('ru')

    def test_explicit_workspace_reports_missing_run(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-I", "-B", str(DIRECTORY / "analyze.py"),
                                     "--workspace", directory, "--run", "missing"],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no run directory matching", result.stderr)
            self.assertIn(directory, result.stderr)

    def sample(self):
        return {"run_id": "fixture", "schema_version": 2, "telemetry": {
            "run_completed": False, "recorded_completion": {"status": "completed", "at": "2026-10-14T00:00:00Z"},
            "accounting_complete": True, "run_accounting_complete": False,
            "accounting_scope": "recorded phases only",
            "repo_status": {"api": {"recorded_status": "verified", "artifacts_intact": False, "goals_verified": False}},
            "completeness": {"usage": {"complete": True, "reasons": []},
                             "coverage": {"complete": False, "reasons": ["uncovered requests"]}},
            "integrity": {"completion": {"hash_mismatch": [{"path": "/fixture/handoff.md",
                "expected_sha256": "before", "actual_sha256": "after"}]}},
            "timing": {"elapsed_seconds": 120, "agent_service_seconds": 60, "unclassified_seconds": 60},
            "phases": [{"phase_id": "i", "phase": "implement", "repo": "api", "status": "passed",
                "usage_source": "ocx exact requestId join", "models": ["actual-model"], "usage_complete": True,
                "duration_seconds": 60, "usage": {"input_tokens": 10, "output_tokens": 55226,
                "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0},
                "ocx": {"vs_transcript": {"output_tokens_equal": False, "transcript_output_tokens": 22429}}}],
            "ocx": {"agents": {"author": {"outside_recorded_phases": {"requests": 73, "complete": True}}}},
            "warnings": []}}

    def test_canonical_usage_actual_model_and_source(self):
        text = report.compact(self.sample())
        self.assertIn("55,226", text)
        self.assertIn("actual-model", text)
        self.assertIn("ocx exact requestId join", text)
        self.assertIn("22,429", text)
        self.assertEqual(1, text.count("| implement / api |"))

    def test_independent_integrity_and_usage_with_specific_mismatch(self):
        text = report.compact(self.sample())
        self.assertIn("completion: **нет**", text)
        self.assertIn("outcome: completed", text)
        self.assertIn("usage: да", text)
        self.assertIn("/fixture/handoff.md", text)
        self.assertIn("expected=before; actual=after", text)

    def test_unknown_is_not_zero_and_real_zero_survives(self):
        data = self.sample()
        data["telemetry"]["phases"][0]["usage"]["input_tokens"] = None
        text = report.compact(data)
        self.assertIn("| неизвестно | 55,226 | 100 | 0 |", text)

    def test_scope_gaps_envelopes_and_unavailable_usd(self):
        text = report.compact(self.sample())
        for value in ("whole-run accounting: нет", "73 requests", "Не доказанная fix-only работа",
                      "не чистое время модели", "Unclassified gaps: 60", "recorded phases only", "USD недоступен"):
            self.assertIn(value, text)

    def test_phase_notes_do_not_interrupt_table(self):
        data = self.sample()
        phase = dict(data["telemetry"]["phases"][0], phase_id="r", phase="review", usage_reasons=["fallback"])
        data["telemetry"]["phases"].append(phase)
        text = report.compact(data)
        self.assertLess(text.index("| review / api |"), text.index("источники расходятся"))

    def test_non_llm_phase_has_not_applicable_usage(self):
        data = self.sample()
        data["telemetry"]["phases"] = [{"phase": "gates", "status": "passed", "usage": {},
            "duration_seconds": 20, "usage_complete": False, "accounting_complete": True}]
        text = report.compact(data)
        self.assertIn("не LLM-фаза | н/п | н/п | н/п | н/п | 20 | н/п | да", text)

    def test_uncovered_orchestrator_and_excluded_requests_are_explained(self):
        data = self.sample()
        data["telemetry"]["coverage"] = {"in_run_outside_phases": {"requests": 119}}
        data["telemetry"]["ocx"].update(orchestrator={"requests": 46, "complete": False, "reasons": ["unreported usage"]},
            other_sessions_in_window={"other": {"requests": 11}}, unattributed_in_window={"requests": 160},
            uncorrelated_agents_in_window={"session/unrelated": {"requests": 7}})
        text = report.compact(data)
        for value in ("119 requests", "Оркестратор в окне запуска: 46", "unreported usage",
                      "Другие доказанные сессии: 11", "Неатрибутированные запросы в окне: 160", "исключены",
                      "Агенты той же сессии без связи с запуском: 7"):
            self.assertIn(value, text)

    def test_unfinished_window_is_not_elapsed_run_time(self):
        data = self.sample()
        data["telemetry"]["timing"].update(elapsed_seconds=None, observation_seconds=120)
        data["telemetry"]["window"] = {"kind": "bounded-observation", "start": "start", "end": "last event"}
        text = report.compact(data)
        self.assertIn("Elapsed wall: неизвестно", text)
        self.assertIn("не длительность завершённого запуска", text)
        self.assertIn("bounded-observation", text)

    def test_legacy_scope_not_recertified(self):
        text = report.compact({"session_id": "old", "accuracy": {"api": {"converged": True}}})
        self.assertIn("Legacy / uncertified", text)
        self.assertIn("не сертификат", text)
        self.assertIn("USD запуска недоступен", text)

    def test_recorded_accuracy_preserved_but_current_flags_fail(self):
        accuracy = {"api": {"review_certified": True, "goals_verified": True, "converged": True}}
        result = report.reconcile_accuracy(accuracy, self.sample()["telemetry"])["api"]
        self.assertTrue(result["recorded_goals_verified"])
        self.assertTrue(result["recorded_review_certified"])
        self.assertFalse(result["goals_verified"])
        self.assertFalse(result["review_certified"])
        self.assertTrue(accuracy["api"]["goals_verified"])

    def test_json_emit_preserves_machine_readable_data(self):
        data = self.sample()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            analyzer.emit_report(data, "json")
        self.assertEqual(data, json.loads(output.getvalue()))

    def test_compact_emit(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            analyzer.emit_report(self.sample(), "compact")
        self.assertTrue(output.getvalue().startswith("# Анализ fixture"))

    def test_cli_formats_are_mutually_exclusive(self):
        with patch("sys.argv", ["analyze.py", "--json", "--format", "compact"]), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                analyzer.main()
        self.assertEqual(2, error.exception.code)


if __name__ == "__main__":
    unittest.main()
