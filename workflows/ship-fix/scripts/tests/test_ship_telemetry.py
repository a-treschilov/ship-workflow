"""Synthetic transcript/run accounting fixtures, no real agent calls."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve()
INSTALLED = (HERE.parents[4] / ".agents/skills/ship-analyze").is_dir()
WORKSPACE = HERE.parents[5] if INSTALLED else HERE.parents[4]
SKILLS = WORKSPACE / (".agents/skills" if INSTALLED else "skills")
spec = importlib.util.spec_from_file_location("telemetry", SKILLS / "ship-analyze/telemetry.py")
telemetry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(telemetry)


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ["SHIP_TEST_TMPDIR"], prefix="telemetry-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        ocx_patch = patch.object(telemetry, "OCX_LOG", self.root / "usage.jsonl")
        ocx_patch.start()
        self.addCleanup(ocx_patch.stop)
        self.project = self.root / "project"
        self.project.mkdir()
        self.run = self.root / "run-1"
        self.run.mkdir()
        self.save(self.run / "contract.json", {"schema_version": 2, "workflow": "fix", "repos": ["api"]})
        self.save(self.run / "run.json", {"run_id": "run-1", "workflow": "fix", "session_id": "session-1",
                                          "started_at": "2026-10-12T00:00:00Z"})
        self.transcript = self.project / "session-1/subagents/agent-author.jsonl"

    def save(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def phase(self, identifier, phase, start, end=None, agent="author", **kwargs):
        data = {"schema_version": 2, "run_id": self.run.name, "workflow": "fix", "phase_id": identifier,
                "phase": phase, "repo": "api", "event": "phase-start", "at": start, "agent_id": agent, **kwargs}
        path = self.save(self.run / f"telemetry/{identifier}-start.json", data)
        if end:
            self.save(self.run / f"telemetry/{identifier}-end.json", {
                **data, "event": "phase-end", "at": end, "status": "passed",
                "start_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})

    def message(self, identifier, moment, usage=None, model="model-a"):
        return {"type": "assistant", "timestamp": moment,
                "message": {"id": identifier, "model": model, "usage": usage if usage is not None else {
                    "input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 3}}}

    def write_messages(self, messages):
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.write_text("\n".join(json.dumps(x) for x in messages))

    def analyze(self):
        return telemetry.analyze(self.run, self.project)

    def test_resumed_fixes_and_other_run_not_counted(self):
        self.phase("implement-1", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z")
        self.phase("fix-1", "review-fix", "2026-10-12T00:02:00Z", "2026-10-12T00:03:00Z")
        self.phase("gate-fix-1", "gate-fix", "2026-10-12T00:04:00Z", "2026-10-12T00:05:00Z")
        self.write_messages([self.message("i", "2026-10-12T00:00:30Z"),
                             self.message("f", "2026-10-12T00:02:30Z"),
                             self.message("g", "2026-10-12T00:04:30Z"),
                             self.message("other-run", "2026-10-12T00:06:30Z")])
        result = self.analyze()
        self.assertTrue(result["accounting_complete"])
        self.assertEqual(30, result["usage"]["input_tokens"])
        self.assertEqual([1, 1, 1], [p["requests"] for p in result["phases"]])
        self.assertEqual(180, result["timing"]["agent_service_seconds"])
        self.assertIsNone(result["timing"]["elapsed_seconds"])
        self.assertEqual(300, result["timing"]["observation_seconds"])
        self.assertEqual("bounded-observation", result["window"]["kind"])
        self.assertEqual(120, result["timing"]["unclassified_seconds"])

    def test_streaming_replay_dedup_not_cumulative_totals(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z")
        first = self.message("same", "2026-10-12T00:00:20Z", {"input_tokens": 10, "output_tokens": 1})
        final = self.message("same", "2026-10-12T00:00:30Z")
        self.write_messages([first, final, final, {"type": "user", "toolUseResult": {"totalTokens": 999999}}])
        result = self.analyze()
        self.assertEqual(1, result["phases"][0]["requests"])
        self.assertEqual(2, result["usage"]["output_tokens"])
        self.assertEqual(10, result["usage"]["input_tokens"])

    def test_models_are_instance_phase_specific(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z")
        self.phase("f", "review-fix", "2026-10-12T00:02:00Z", "2026-10-12T00:03:00Z")
        self.write_messages([self.message("i", "2026-10-12T00:00:30Z", model="model-a"),
                             self.message("f1", "2026-10-12T00:02:10Z", model="model-b"),
                             self.message("f2", "2026-10-12T00:02:30Z", model="model-c")])
        result = self.analyze()
        self.assertEqual(["model-a"], result["phases"][0]["models"])
        self.assertEqual(["model-b", "model-c"], result["phases"][1]["models"])

    def test_missing_usage_is_null_not_zero(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z")
        self.write_messages([self.message("i", "2026-10-12T00:00:30Z", {"input_tokens": 10})])
        result = self.analyze()
        self.assertFalse(result["accounting_complete"])
        self.assertIsNone(result["usage"]["output_tokens"])
        self.assertIsNone(result["usage"]["cache_read_input_tokens"])

    def test_incomplete_or_missing_telemetry(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z")
        result = self.analyze()
        self.assertFalse(result["accounting_complete"])
        self.assertIsNone(result["phases"][0]["duration_seconds"])
        self.assertTrue(result["warnings"])

    def test_wait_and_gate_time_and_overlap(self):
        self.phase("g", "gates", "2026-10-12T00:00:00Z", "2026-10-12T00:02:00Z", agent=None)
        self.phase("w", "checkpoint-wait", "2026-10-12T00:01:00Z", "2026-10-12T00:03:00Z", agent=None)
        result = self.analyze()
        self.assertEqual(120, result["timing"]["gate_seconds"])
        self.assertEqual(120, result["timing"]["user_wait_seconds"])
        self.assertEqual(60, result["timing"]["overlap_seconds"])
        self.assertEqual(180, result["timing"]["covered_seconds"])
        self.assertIsNone(result["usage"]["input_tokens"])

    def test_tool_correlation_metadata_avoids_description_guess(self):
        self.phase("r", "review", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z", agent=None, tool_use_id="nested-tool")
        self.save(self.transcript.with_suffix(".meta.json"), {"toolUseId": "nested-tool", "agentType": "crumb-reviewer"})
        self.write_messages([self.message("r", "2026-10-12T00:00:30Z")])
        self.assertEqual("author", self.analyze()["phases"][0]["agent_id"])

    def test_malformed_or_orphan_event_not_certified(self):
        self.save(self.run / "telemetry/orphan-end.json", {"event": "phase-end"})
        self.save(self.run / "telemetry/bad-start.json", {"phase_id": "../unsafe"})
        result = self.analyze()
        self.assertFalse(result["accounting_complete"])
        self.assertTrue(any("Orphan" in w for w in result["warnings"]))

    def test_completion_file_without_seals_not_success(self):
        self.save(self.run / "completion.json", {"run_id": "run-1", "status": "completed"})
        result = self.analyze()
        self.assertFalse(result["run_completed"])
        self.assertTrue(any("certificate" in w for w in result["warnings"]))

    def test_two_runs_same_session_have_disjoint_accounting(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z")
        self.write_messages([self.message("one", "2026-10-12T00:00:30Z"),
                             self.message("two", "2026-10-12T00:02:30Z")])
        first = self.analyze()
        self.run = self.root / "run-2"
        self.save(self.run / "contract.json", {"schema_version": 2, "workflow": "fix", "repos": ["api"]})
        self.save(self.run / "run.json", {"run_id": "run-2", "session_id": "session-1",
                                         "started_at": "2026-10-12T00:02:00Z"})
        self.phase("f", "gate-fix", "2026-10-12T00:02:00Z", "2026-10-12T00:03:00Z")
        second = self.analyze()
        self.assertEqual(10, first["usage"]["input_tokens"])
        self.assertEqual(10, second["usage"]["input_tokens"])
        self.assertEqual(1, second["phases"][0]["requests"])

    def test_malformed_transcript_never_claims_complete_accounting(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z")
        self.write_messages([self.message("one", "2026-10-12T00:00:30Z")])
        with self.transcript.open("a") as handle:
            handle.write("\n{truncated\n")
        result = self.analyze()
        self.assertFalse(result["accounting_complete"])
        self.assertFalse(result["run_accounting_complete"])

    def test_start_hash_mismatch_does_not_certify_phase(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z")
        self.write_messages([self.message("one", "2026-10-12T00:00:30Z")])
        start_path = self.run / "telemetry/i-start.json"
        record = telemetry.read(start_path)
        record["at"] = "2026-10-12T00:00:01Z"
        self.save(start_path, record)
        result = self.analyze()
        self.assertFalse(result["accounting_complete"])
        self.assertIsNone(result["phases"][0]["duration_seconds"])

    def ocx_row(self, identifier="ocx-a", **changes):
        return {"requestId": identifier, "timestamp": int(telemetry.at("2026-10-12T00:00:30Z").timestamp() * 1000),
                "model": "actual-model", "usageStatus": "reported", "usage": {
                    "inputTokens": 40, "outputTokens": 9, "cacheReadInputTokens": 20,
                    "cacheCreationInputTokens": 3}, **changes}

    def write_ocx(self, rows):
        telemetry.OCX_LOG.write_text("\n".join(json.dumps(r) for r in rows))

    def routed(self, identifier="ocx-a", moment="2026-10-12T00:00:30Z", usage=None):
        return {**self.message(identifier, moment, usage), "requestId": identifier}

    def test_complete_ocx_is_canonical_and_keeps_disagreement(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z")
        self.write_messages([self.routed()])
        self.write_ocx([self.ocx_row()])
        result = self.analyze()
        phase = result["phases"][0]
        self.assertEqual(17, phase["usage"]["input_tokens"])
        self.assertEqual(9, result["usage"]["output_tokens"])
        self.assertEqual(["actual-model"], phase["models"])
        self.assertEqual("ocx exact requestId join", phase["usage_source"])
        self.assertTrue(phase["usage_complete"])
        self.assertFalse(phase["ocx"]["vs_transcript"]["output_tokens_equal"])
        self.assertEqual(2, phase["transcript"]["usage"]["output_tokens"])

    def test_missing_ocx_expected_row_nulls_and_falls_back(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z")
        self.write_messages([self.routed(), self.routed("ocx-b")])
        self.write_ocx([self.ocx_row()])
        phase = self.analyze()["phases"][0]
        self.assertEqual("deduplicated transcript messages", phase["usage_source"])
        self.assertEqual(["ocx-b"], phase["ocx"]["missing_request_ids"])
        self.assertIsNone(phase["ocx"]["output_tokens"])
        self.assertEqual(9, phase["ocx"]["known_subtotal"]["output_tokens"])
        self.assertTrue(phase["usage_complete"])
        self.assertTrue(phase["usage_reasons"])

    def test_strict_ocx_counters_and_expected_ids(self):
        cases = [{"inputTokens": 40, "outputTokens": 9},
                 {"inputTokens": -1, "outputTokens": 9, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0},
                 {"inputTokens": 1, "outputTokens": 9, "cacheReadInputTokens": 20, "cacheCreationInputTokens": 0},
                 {"inputTokens": True, "outputTokens": 9, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0},
                 {"inputTokens": 40, "outputTokens": 9, "cacheReadInputTokens": "20", "cacheCreationInputTokens": 0}]
        for usage in cases:
            with self.subTest(usage=usage):
                sums = telemetry.ocx_sums([self.ocx_row(usage=usage)], {"ocx-a"})
                self.assertFalse(sums["complete"])
                self.assertIsNone(sums["fresh_input_tokens"])
                self.assertEqual(9, sums["output_tokens"])
        sums = telemetry.ocx_sums([self.ocx_row(usageStatus="unreported")], {"ocx-a"})
        self.assertFalse(sums["complete"])
        self.assertIsNone(sums["output_tokens"])
        self.assertEqual({"actual-model": 1}, sums["models"])
        self.assertTrue(telemetry.ocx_sums([], set())["complete"])
        self.assertFalse(telemetry.ocx_sums([], {"ocx-a"})["complete"])

    def test_partial_ocx_keeps_exact_output_without_mixing_sources(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z")
        self.write_messages([self.routed(usage={"input_tokens": 10, "output_tokens": 2})])
        self.write_ocx([self.ocx_row(usage={"inputTokens": 40, "outputTokens": 9})])
        phase = self.analyze()["phases"][0]
        self.assertEqual(9, phase["usage"]["output_tokens"])
        self.assertIsNone(phase["usage"]["input_tokens"])
        self.assertFalse(phase["usage_complete"])
        self.assertEqual(["actual-model"], phase["models"])

    def test_boundary_ownership_is_independent_of_job_order(self):
        self.phase("z", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z")
        self.phase("a", "review-fix", "2026-10-12T00:01:00Z", "2026-10-12T00:02:00Z")
        self.write_messages([self.routed(moment="2026-10-12T00:01:00Z")])
        result = self.analyze()
        self.assertEqual(1, result["coverage"]["ambiguous"]["requests"])
        self.assertEqual([0, 0], [p["requests"] for p in result["phases"]])
        self.assertTrue(all(not p["correlation_complete"] for p in result["phases"]))
        self.assertIsNone(result["usage"]["output_tokens"])
        jobs = [(p, self.transcript, telemetry.at(p["start_at"]), telemetry.at(p["end_at"])) for p in result["phases"]]
        telemetry.ocx_join(self.project, ["session-1"], list(reversed(jobs)),
                           telemetry.at("2026-10-12T00:00:00Z"), telemetry.at("2026-10-12T00:02:00Z"))
        self.assertEqual([0, 0], [p["requests"] for p in result["phases"]])

    def test_buckets_are_window_scoped_not_transcript_totals(self):
        self.phase("i", "implement", "2026-10-12T00:00:10Z", "2026-10-12T00:00:40Z")
        self.save(self.run / "completion.json", {"run_id": "run-1", "status": "completed", "at": "2026-10-12T00:01:00Z"})
        self.write_messages([self.routed("ocx-a", "2026-10-11T23:59:59Z"), self.routed("ocx-b"),
                             self.routed("ocx-c", "2026-10-12T00:00:50Z"),
                             self.routed("ocx-d", "2026-10-12T00:02:00Z"), self.routed("ocx-e", None)])
        self.write_ocx([self.ocx_row(i) for i in ("ocx-a", "ocx-b", "ocx-c", "ocx-d", "ocx-e")])
        result = self.analyze()
        self.assertEqual({"in_phase": 1, "in_run_outside_phases": 1, "outside_run": 2, "unknown_time": 1, "ambiguous": 0},
                         {k: v["requests"] for k, v in result["coverage"].items()})
        agent = next(iter(result["ocx"]["agents"].values()))
        self.assertEqual(5, agent["transcript_wide_total"]["requests"])
        self.assertEqual(2, agent["in_run"]["requests"])
        self.assertEqual(1, agent["outside_recorded_phases"]["requests"])
        self.assertFalse(result["run_accounting_complete"])

    def test_only_recognized_cli_inputs_are_excluded(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z")
        self.write_messages([self.message("one", "2026-10-12T00:00:30Z")])
        start = telemetry.read(self.run / "telemetry/i-start.json")
        start.pop("schema_version")
        self.save(self.run / "telemetry/in-i-start.json", start)
        self.save(self.run / "telemetry/in-i-end.json", {**start, "event": "phase-end"})
        result = self.analyze()
        self.assertEqual(1, len(result["phases"]))
        self.assertTrue(result["completeness"]["events"]["complete"])
        self.save(self.run / "telemetry/in-bad-start.json", {"phase_id": "in-bad", "schema_version": 2})
        result = self.analyze()
        self.assertEqual(2, len(result["phases"]))
        self.assertFalse(result["completeness"]["events"]["complete"])
        (self.run / "telemetry/broken-start.json").write_text("{bad")
        self.assertTrue(any("Malformed" in w for w in self.analyze()["warnings"]))

    def test_seal_failure_is_independent_of_tokens_and_history(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z")
        self.write_messages([self.routed()])
        self.write_ocx([self.ocx_row()])
        artifact = self.root / "handoff.txt"
        artifact.write_text("original")
        seals = {str(artifact): hashlib.sha256(artifact.read_bytes()).hexdigest(), str(self.root / "missing"): "0" * 64}
        self.save(self.run / "api/state-1.json", {"status": "verified", "artifact_hashes": seals})
        self.save(self.run / "completion.json", {"run_id": "run-1", "status": "completed", "at": "2026-10-12T00:01:00Z", "artifact_hashes": seals})
        artifact.write_text("changed")
        result = self.analyze()
        self.assertTrue(result["completeness"]["usage"]["complete"])
        self.assertTrue(result["accounting_complete"])
        self.assertEqual(9, result["usage"]["output_tokens"])
        self.assertFalse(result["run_completed"])
        self.assertTrue(result["repo_status"]["api"]["recorded_goals_verified"])
        self.assertFalse(result["repo_status"]["api"]["goals_verified"])
        audit = result["integrity"]["completion"]
        self.assertEqual(2, audit["reference_count"])
        self.assertEqual(str(artifact), audit["hash_mismatch"][0]["path"])
        self.assertNotEqual(audit["hash_mismatch"][0]["expected_sha256"], audit["hash_mismatch"][0]["actual_sha256"])
        self.assertEqual(1, len(audit["missing"]))
        self.assertEqual("completed", result["recorded_completion"]["status"])

    def test_empty_or_outside_only_transcript_never_proves_zero(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z")
        for messages in ([], [self.message("outside", "2026-10-12T00:02:00Z")]):
            for available in (False, True):
                with self.subTest(messages=len(messages), ocx=available):
                    self.write_messages(messages)
                    if available:
                        self.write_ocx([])
                    else:
                        telemetry.OCX_LOG.unlink(missing_ok=True)
                    result = self.analyze()
                    phase = result["phases"][0]
                    self.assertFalse(phase["usage_complete"])
                    self.assertFalse(phase["accounting_complete"])
                    self.assertFalse(result["accounting_complete"])
                    self.assertTrue(all(v is None for v in phase["usage"].values()))
                    self.assertTrue(all(v is None for v in result["usage"].values()))
                    self.assertTrue(any("absence" in r for r in phase["usage_reasons"]))

    def test_conflicting_ocx_rows_cannot_certify_exact_join(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z")
        self.write_messages([self.routed()])
        first, second = self.ocx_row(), self.ocx_row(model="different-model")
        for rows in ([first, second, first], [second, first, second]):
            with self.subTest(order=[r["model"] for r in rows]):
                self.write_ocx(rows)
                phase = self.analyze()["phases"][0]
                self.assertFalse(phase["ocx"]["complete"])
                self.assertEqual("deduplicated transcript messages", phase["usage_source"])
                self.assertEqual(2, phase["usage"]["output_tokens"])

    def test_negative_transcript_counters_are_not_hidden_by_valid_replay(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z")
        good = self.message("same", "2026-10-12T00:00:30Z")
        bad = self.message("same", "2026-10-12T00:00:30Z", {"output_tokens": -1})
        for messages in ([good, bad], [bad, good]):
            self.write_messages(messages)
            phase = self.analyze()["phases"][0]
            self.assertFalse(phase["usage_complete"])
            self.assertIsNone(phase["usage"]["output_tokens"])
            self.assertEqual(1, phase["requests"])

    def test_seal_audit_malformed_and_unreadable_are_distinct(self):
        directory = self.root / "cannot-read-directory-as-file"
        directory.mkdir()
        audit = telemetry.seal_audit({str(directory): "0" * 64, "relative": "not-a-hash"})
        self.assertFalse(audit["intact"])
        self.assertEqual(1, len(audit["unreadable"]))
        self.assertEqual(1, len(audit["malformed"]))
        self.assertEqual([], audit["missing"])
        self.assertEqual([], audit["hash_mismatch"])
        self.assertFalse(telemetry.seal_audit([])["intact"])

    def test_same_session_unrelated_agent_is_not_run_work(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z")
        self.write_messages([self.routed()])
        unrelated = self.transcript.with_name("agent-unrelated.jsonl")
        unrelated.write_text(json.dumps(self.routed("ocx-b")))
        self.write_ocx([self.ocx_row(), self.ocx_row("ocx-b")])
        result = self.analyze()
        self.assertEqual(1, result["coverage"]["in_phase"]["requests"])
        self.assertEqual(0, result["coverage"]["in_run_outside_phases"]["requests"])
        self.assertEqual(9, result["usage"]["output_tokens"])
        self.assertNotIn("session-1/unrelated", result["ocx"]["agents"])
        self.assertEqual(1, result["ocx"]["uncorrelated_agents_in_window"]["session-1/unrelated"]["requests"])

    def test_phase_envelopes_cannot_override_run_start_or_end(self):
        for boundary, first, last, outside in (
                ("start", "2026-10-11T23:59:00Z", "2026-10-12T00:01:00Z", "2026-10-11T23:59:30Z"),
                ("end", "2026-10-12T00:00:00Z", "2026-10-12T00:02:00Z", "2026-10-12T00:01:30Z")):
            with self.subTest(boundary=boundary):
                self.phase("i", "implement", first, last)
                self.save(self.run / "completion.json", {"run_id": "run-1", "status": "completed", "at": "2026-10-12T00:01:00Z"})
                self.write_messages([self.routed(), self.routed("ocx-b", outside)])
                self.write_ocx([self.ocx_row(), self.ocx_row("ocx-b")])
                result = self.analyze()
                self.assertEqual(1, result["coverage"]["outside_run"]["requests"])
                self.assertEqual(1, result["coverage"]["in_phase"]["requests"])
                self.assertEqual(9, result["usage"]["output_tokens"])
                self.assertFalse(result["completeness"]["events"]["complete"])
                self.assertFalse(result["phases"][0]["accounting_complete"])
                self.assertFalse(result["accounting_complete"])
                self.assertTrue(any("boundaries" in w for w in result["warnings"]))


class CodexTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ["SHIP_TEST_TMPDIR"], prefix="codex-telemetry-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = self.root / "catalog"
        self.catalog.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        self.run = self.root / "run-1"
        self.run.mkdir()
        self.save(self.run / "contract.json", {"schema_version": 2, "workflow": "fix", "repos": ["api"]})
        self.save(self.run / "run.json", {"run_id": "run-1", "workflow": "fix", "session_id": "",
                                          "started_at": "2026-10-12T00:00:00Z"})
        self.env_patch = patch.dict(os.environ, {"SHIP_ANALYZE_CODEX_TRANSCRIPTS": str(self.catalog)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def save(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def phase(self, identifier, phase, start, end, agent, session_id=None):
        data = {"schema_version": 2, "run_id": self.run.name, "workflow": "fix", "phase_id": identifier,
                "phase": phase, "repo": "api", "event": "phase-start", "at": start, "agent_id": agent}
        if session_id is not None:
            data["session_id"] = session_id
        path = self.save(self.run / f"telemetry/{identifier}-start.json", data)
        self.save(self.run / f"telemetry/{identifier}-end.json", {
            **data, "event": "phase-end", "at": end, "status": "passed",
            "start_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})

    def usage(self, input_tokens=20, cached=5, write=0, output=3, total=None):
        if total is None:
            total = input_tokens + output
        return {"input_tokens": input_tokens, "cached_input_tokens": cached,
                "cache_write_input_tokens": write, "output_tokens": output, "total_tokens": total}

    def rollout(self, thread_id, *, cwd=None, session_id=None, agent_path=None, parent=None,
                events=(), model="model-a", turn_contexts=None, ts0="2026-10-12T00:00:00Z",
                history_mode=None, forked_from_id=None):
        payload = {"id": thread_id, "session_id": session_id or thread_id, "parent_thread_id": parent,
                   "forked_from_id": forked_from_id, "history_mode": history_mode,
                   "agent_path": agent_path, "cwd": cwd,
                   "thread_source": "subagent" if agent_path else "user"}
        lines = [json.dumps({"timestamp": ts0, "type": "session_meta", "payload": payload})]
        contexts = turn_contexts if turn_contexts is not None else ([(ts0, model)] if model else [])
        for moment, name in contexts:
            lines.append(json.dumps({"timestamp": moment, "type": "turn_context",
                                     "payload": {"turn_id": "turn-" + thread_id, "model": name}}))
        for moment, last, total in events:
            lines.append(json.dumps({"timestamp": moment, "type": "event_msg",
                                     "payload": {"type": "token_count", "info": {
                                         "last_token_usage": last, "total_token_usage": total}}}))
        (self.catalog / f"rollout-{thread_id}.jsonl").write_text(chr(10).join(lines) + chr(10))
        return thread_id

    def analyze(self):
        return telemetry.analyze(self.run, self.project)

    def test_codex_exact_agent_path_match(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z", agent="/root/author")
        self.rollout("root-1", cwd=str(WORKSPACE), history_mode="paginated")
        self.rollout("child-1", session_id="root-1", agent_path="/root/author", parent="root-1",
                     history_mode="paginated", forked_from_id="root-1",
                     events=[("2026-10-12T00:00:30Z", self.usage(20, 5, 1, 3, 23), {"total_tokens": 23})])
        phase = self.analyze()["phases"][0]
        self.assertTrue(phase["correlation_complete"])
        self.assertTrue(phase["usage_complete"])
        self.assertEqual(15, phase["usage"]["input_tokens"])
        self.assertEqual(3, phase["usage"]["output_tokens"])
        self.assertEqual(5, phase["usage"]["cache_read_input_tokens"])
        self.assertEqual(1, phase["usage"]["cache_creation_input_tokens"])
        self.assertEqual("codex per-request usage (token_count last_usage; fork-baseline; window attribution)",
                         phase["usage_source"])
        self.assertTrue(self.analyze()["accounting_complete"])

    def test_codex_paginated_baseline_zero(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z", agent="/root/author")
        self.rollout("root-1", cwd=str(WORKSPACE), history_mode="paginated")
        self.rollout("child-1", session_id="root-1", agent_path="/root/author", parent="root-1",
                     history_mode="paginated",
                     events=[("2026-10-12T00:00:20Z", self.usage(50, 10, 0, 5, 55), {"total_tokens": 55}),
                             ("2026-10-12T00:00:40Z", self.usage(80, 20, 0, 5, 85), {"total_tokens": 140})])
        phase = self.analyze()["phases"][0]
        self.assertTrue(phase["usage_complete"])
        self.assertEqual(100, phase["usage"]["input_tokens"])
        self.assertEqual(10, phase["usage"]["output_tokens"])
        self.assertEqual(30, phase["usage"]["cache_read_input_tokens"])
        self.assertEqual(2, phase["requests"])
        self.assertEqual(0, phase["ocx"]["baseline"])

    def test_codex_legacy_fork_excludes_replay(self):
        self.phase("i", "implement", "2026-10-12T00:00:05Z", "2026-10-12T00:01:00Z", agent="/root/author")
        self.rollout("root-1", cwd=str(WORKSPACE), history_mode="legacy",
                     events=[("2026-10-11T23:59:00Z", self.usage(100, 0, 0, 10, 110), {"total_tokens": 110}),
                             ("2026-10-11T23:59:30Z", self.usage(200, 0, 0, 10, 210), {"total_tokens": 320})])
        self.rollout("child-1", session_id="root-1", agent_path="/root/author", parent="root-1",
                     history_mode="legacy", forked_from_id="root-1", ts0="2026-10-12T00:00:00Z",
                     events=[("2026-10-12T00:00:00Z", self.usage(100, 0, 0, 10, 110), {"total_tokens": 110}),
                             ("2026-10-12T00:00:00Z", self.usage(200, 0, 0, 10, 210), {"total_tokens": 320}),
                             ("2026-10-12T00:00:20Z", self.usage(50, 10, 0, 5, 55), {"total_tokens": 375}),
                             ("2026-10-12T00:00:40Z", self.usage(80, 20, 0, 5, 85), {"total_tokens": 460})])
        phase = self.analyze()["phases"][0]
        self.assertTrue(phase["usage_complete"])
        self.assertEqual(100, phase["usage"]["input_tokens"])
        self.assertEqual(10, phase["usage"]["output_tokens"])
        self.assertEqual(30, phase["usage"]["cache_read_input_tokens"])
        self.assertEqual(2, phase["requests"])
        self.assertEqual(320, phase["ocx"]["baseline"])

    def test_codex_baseline_fallback_when_parent_missing(self):
        moment = telemetry.at("2026-10-12T00:00:00Z")
        thread = {"parent_thread_id": "missing", "history_mode": "legacy", "forked_from_id": "missing",
                  "start": moment,
                  "token_events": [
                      (moment, self.usage(100, 0, 0, 10, 110), {"total_tokens": 110}),
                      (moment, self.usage(200, 0, 0, 10, 210), {"total_tokens": 320}),
                      (telemetry.at("2026-10-12T00:00:20Z"), self.usage(50, 10, 0, 5, 55), {"total_tokens": 375})]}
        baseline, reason = telemetry.codex_thread_baseline(thread, {})
        self.assertEqual(320, baseline)
        self.assertIsNone(reason)
        stream = telemetry.codex_thread_stream(thread, baseline)
        self.assertEqual(1, len(stream["real"]))
        self.assertTrue(stream["crosscheck"]["equal"])

    def test_codex_baseline_unknown_without_data(self):
        moment = telemetry.at("2026-10-12T00:00:00Z")
        thread = {"parent_thread_id": "missing", "history_mode": "legacy", "forked_from_id": "missing",
                  "start": moment, "token_events": []}
        baseline, reason = telemetry.codex_thread_baseline(thread, {})
        self.assertIsNone(baseline)
        self.assertIn("No parent rollout", reason)

    def test_codex_reset_splits_segments(self):
        thread = {"parent_thread_id": None, "history_mode": "paginated", "forked_from_id": None,
                  "start": telemetry.at("2026-10-12T00:00:00Z"),
                  "token_events": [
                      (telemetry.at("2026-10-12T00:00:10Z"), self.usage(100, 0, 0, 10, 110), {"total_tokens": 110}),
                      (telemetry.at("2026-10-12T00:00:20Z"), self.usage(140, 0, 0, 10, 150), {"total_tokens": 260}),
                      (telemetry.at("2026-10-12T00:00:30Z"), self.usage(80, 0, 0, 5, 85), {"total_tokens": 85}),
                      (telemetry.at("2026-10-12T00:00:40Z"), self.usage(115, 0, 0, 5, 120), {"total_tokens": 205})]}
        stream = telemetry.codex_thread_stream(thread, 0)
        self.assertTrue(stream["crosscheck"]["equal"])
        self.assertEqual(4, len(stream["real"]))
        self.assertEqual(465, stream["crosscheck"]["per_request_total"])
        self.assertEqual(465, stream["crosscheck"]["cumulative_delta"])

    def test_codex_shared_thread_splits_by_window(self):
        self.phase("a", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:00:30Z", agent="/root/author")
        self.phase("b", "review", "2026-10-12T00:00:31Z", "2026-10-12T00:01:00Z", agent="/root/author")
        self.rollout("root-1", cwd=str(WORKSPACE))
        self.rollout("child-1", session_id="root-1", agent_path="/root/author", parent="root-1",
                     events=[("2026-10-12T00:00:10Z", self.usage(50, 10, 0, 5, 55), {"total_tokens": 55}),
                             ("2026-10-12T00:00:40Z", self.usage(80, 20, 0, 5, 85), {"total_tokens": 140})])
        result = self.analyze()
        phases = {p["phase_id"]: p for p in result["phases"]}
        self.assertEqual(40, phases["a"]["usage"]["input_tokens"])
        self.assertEqual(60, phases["b"]["usage"]["input_tokens"])
        self.assertEqual(1, phases["a"]["requests"])
        self.assertEqual(1, phases["b"]["requests"])
        self.assertTrue(phases["a"]["usage_complete"])
        self.assertTrue(phases["b"]["usage_complete"])

    def test_codex_overlapping_windows_ambiguous(self):
        self.phase("a", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:00:30Z", agent="/root/author")
        self.phase("b", "review", "2026-10-12T00:00:20Z", "2026-10-12T00:00:50Z", agent="/root/author")
        self.rollout("root-1", cwd=str(WORKSPACE))
        self.rollout("child-1", session_id="root-1", agent_path="/root/author", parent="root-1",
                     events=[("2026-10-12T00:00:25Z", self.usage(50, 10, 0, 5, 55), {"total_tokens": 55}),
                             ("2026-10-12T00:00:45Z", self.usage(80, 20, 0, 5, 85), {"total_tokens": 140})])
        result = self.analyze()
        phases = {p["phase_id"]: p for p in result["phases"]}
        self.assertEqual(1, result["coverage"]["ambiguous"]["requests"])
        self.assertEqual(0, phases["a"]["requests"])
        self.assertIsNone(phases["a"]["usage"]["input_tokens"])
        self.assertEqual(1, phases["b"]["requests"])
        self.assertEqual(60, phases["b"]["usage"]["input_tokens"])

    def test_codex_inherited_models_ignored(self):
        self.phase("i", "implement", "2026-10-12T00:00:30Z", "2026-10-12T00:01:00Z", agent="/root/author")
        self.rollout("root-1", cwd=str(WORKSPACE))
        self.rollout("child-1", session_id="root-1", agent_path="/root/author", parent="root-1",
                     ts0="2026-10-12T00:00:00Z",
                     turn_contexts=[("2026-10-12T00:00:00Z", "inherited-a"),
                                    ("2026-10-12T00:00:01Z", "inherited-b"),
                                    ("2026-10-12T00:00:05Z", "real-model")],
                     events=[("2026-10-12T00:00:40Z", self.usage(50, 10, 0, 5, 55), {"total_tokens": 55})])
        phase = self.analyze()["phases"][0]
        self.assertEqual(["real-model"], phase["models"])
        self.assertTrue(phase["usage_complete"])
        self.assertFalse(any("Multiple models" in reason for reason in phase["usage_reasons"]))

    def test_codex_phase_without_events_is_null(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z", agent="/root/author")
        self.rollout("root-1", cwd=str(WORKSPACE))
        self.rollout("child-1", session_id="root-1", agent_path="/root/author", parent="root-1", events=[])
        phase = self.analyze()["phases"][0]
        self.assertTrue(phase["correlation_complete"])
        self.assertFalse(phase["usage_complete"])
        self.assertTrue(all(value is None for value in phase["usage"].values()))
        self.assertTrue(any("absence" in reason for reason in phase["usage_reasons"]))

    def test_codex_unmatched_phase_warns_once(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z", agent="/root/author")
        self.phase("x", "review", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z", agent="/root/missing")
        self.rollout("root-1", cwd=str(WORKSPACE))
        self.rollout("child-1", session_id="root-1", agent_path="/root/author", parent="root-1",
                     events=[("2026-10-12T00:00:30Z", self.usage(20, 5, 0, 3, 23), {"total_tokens": 23})])
        warnings = [w for w in self.analyze()["warnings"] if "Usage unavailable" in w]
        self.assertEqual(["Usage unavailable or ambiguous: x"], warnings)

    def test_codex_duplicate_agent_path_ambiguous(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z", agent="/root/author")
        self.rollout("root-1", cwd=str(WORKSPACE))
        self.rollout("child-1", session_id="root-1", agent_path="/root/author", parent="root-1",
                     events=[("2026-10-12T00:00:30Z", self.usage(), {"total_tokens": 23})])
        self.rollout("child-2", session_id="root-1", agent_path="/root/author", parent="root-1",
                     events=[("2026-10-12T00:00:40Z", self.usage(), {"total_tokens": 23})])
        phase = self.analyze()["phases"][0]
        self.assertFalse(phase["correlation_complete"])
        self.assertTrue(all(value is None for value in phase["usage"].values()))
        self.assertTrue(any("Ambiguous" in reason for reason in phase["usage_reasons"]))

    def test_codex_plateau_not_double_counted(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z", agent="/root/author")
        self.rollout("root-1", cwd=str(WORKSPACE))
        self.rollout("child-1", session_id="root-1", agent_path="/root/author", parent="root-1",
                     events=[("2026-10-12T00:00:20Z", self.usage(20, 5, 0, 3, 23), {"total_tokens": 23}),
                             ("2026-10-12T00:00:30Z", self.usage(20, 5, 0, 3, 23), {"total_tokens": 23})])
        phase = self.analyze()["phases"][0]
        self.assertEqual(1, phase["requests"])
        self.assertTrue(phase["usage_complete"])
        self.assertEqual(15, phase["usage"]["input_tokens"])

    def test_codex_cumulative_mismatch_fails_usage(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z", agent="/root/author")
        self.rollout("root-1", cwd=str(WORKSPACE))
        self.rollout("child-1", session_id="root-1", agent_path="/root/author", parent="root-1",
                     events=[("2026-10-12T00:00:20Z", self.usage(20, 5, 0, 3, 23), {"total_tokens": 23}),
                             ("2026-10-12T00:00:40Z", self.usage(20, 5, 0, 3, 999), {"total_tokens": 46})])
        phase = self.analyze()["phases"][0]
        self.assertTrue(phase["correlation_complete"])
        self.assertFalse(phase["usage_complete"])
        self.assertTrue(any("Cumulative delta" in reason or "Per-request total" in reason
                            for reason in phase["usage_reasons"]))

    def test_codex_missing_usage_null_not_zero(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z", agent="/root/author")
        self.rollout("root-1", cwd=str(WORKSPACE))
        self.rollout("child-1", session_id="root-1", agent_path="/root/author", parent="root-1",
                     events=[("2026-10-12T00:00:30Z", {"input_tokens": 10}, {"total_tokens": 23})])
        phase = self.analyze()["phases"][0]
        self.assertTrue(phase["correlation_complete"])
        self.assertFalse(phase["usage_complete"])
        self.assertIsNone(phase["usage"]["input_tokens"])
        self.assertIsNone(phase["usage"]["output_tokens"])
        self.assertIsNone(phase["usage"]["cache_read_input_tokens"])
        self.assertIsNone(phase["usage"]["cache_creation_input_tokens"])

    def test_codex_input_cached_semantics(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z", agent="/root/author")
        self.rollout("root-1", cwd=str(WORKSPACE))
        self.rollout("child-1", session_id="root-1", agent_path="/root/author", parent="root-1",
                     events=[("2026-10-12T00:00:30Z",
                              self.usage(input_tokens=5, cached=20, write=3, output=2, total=7),
                              {"total_tokens": 7})])
        phase = self.analyze()["phases"][0]
        self.assertEqual(0, phase["usage"]["input_tokens"])
        self.assertEqual(20, phase["usage"]["cache_read_input_tokens"])
        self.assertEqual(3, phase["usage"]["cache_creation_input_tokens"])

    def test_codex_two_roots_phase_sessions_attributed(self):
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z",
                   agent="/root/author", session_id="root-1")
        self.phase("r", "review", "2026-10-12T00:02:00Z", "2026-10-12T00:03:00Z",
                   agent="/root/reviewer", session_id="root-2")
        self.rollout("root-1", cwd=str(WORKSPACE),
                     events=[("2026-10-12T00:00:05Z", self.usage(5, 0, 0, 1, 6), {"total_tokens": 6})])
        self.rollout("child-1", session_id="root-1", agent_path="/root/author", parent="root-1",
                     events=[("2026-10-12T00:00:30Z", self.usage(20, 5, 0, 3, 23), {"total_tokens": 23})])
        self.rollout("root-2", cwd=str(WORKSPACE),
                     events=[("2026-10-12T00:02:05Z", self.usage(5, 0, 0, 1, 6), {"total_tokens": 6})])
        self.rollout("child-2", session_id="root-2", agent_path="/root/reviewer", parent="root-2",
                     events=[("2026-10-12T00:02:30Z", self.usage(30, 10, 0, 4, 34), {"total_tokens": 34})])
        result = self.analyze()
        phases = {p["phase_id"]: p for p in result["phases"]}
        self.assertTrue(phases["i"]["correlation_complete"])
        self.assertTrue(phases["i"]["usage_complete"])
        self.assertTrue(phases["r"]["correlation_complete"])
        self.assertTrue(phases["r"]["usage_complete"])
        self.assertEqual(15, phases["i"]["usage"]["input_tokens"])
        self.assertEqual(20, phases["r"]["usage"]["input_tokens"])
        self.assertEqual(2, result["coverage"]["in_phase"]["requests"])
        self.assertIsInstance(result["ocx"]["orchestrator"], dict)
        self.assertEqual(2, result["ocx"]["orchestrator"]["requests"])
        self.assertFalse(any("No Codex orchestrator identified" in reason
                             for phase in result["phases"] for reason in phase.get("usage_reasons", [])))
        self.assertFalse(any("Usage unavailable or ambiguous" in warning for warning in result["warnings"]))

    def test_codex_unknown_phase_session_falls_back_to_all_roots(self):
        self.phase("p", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z",
                   agent="/root/author", session_id="root-missing")
        self.rollout("root-1", cwd=str(WORKSPACE))
        self.rollout("child-1", session_id="root-1", agent_path="/root/author", parent="root-1",
                     events=[("2026-10-12T00:00:30Z", self.usage(20, 5, 0, 3, 23), {"total_tokens": 23})])
        result = self.analyze()
        phase = result["phases"][0]
        self.assertTrue(phase["correlation_complete"], phase["usage_reasons"])
        self.assertTrue(phase["usage_complete"], phase["usage_reasons"])
        self.assertEqual(15, phase["usage"]["input_tokens"])
        self.assertTrue(any("matches no Codex root" in note for note in result["ocx"]["notes"]))

    def test_codex_empty_phase_session_falls_back_to_unique_agent_path(self):
        self.save(self.run / "run.json", {"run_id": "run-1", "workflow": "fix", "session_id": "",
                                          "started_at": "2026-10-12T00:00:00Z"})
        self.phase("i", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z",
                   agent="/root/author", session_id="")
        self.rollout("root-1", cwd=str(WORKSPACE))
        self.rollout("child-1", session_id="root-1", agent_path="/root/author", parent="root-1",
                     events=[("2026-10-12T00:00:30Z", self.usage(20, 5, 0, 3, 23), {"total_tokens": 23})])
        phase = self.analyze()["phases"][0]
        self.assertTrue(phase["correlation_complete"])
        self.assertTrue(phase["usage_complete"])
        self.assertEqual(15, phase["usage"]["input_tokens"])

    def test_codex_rotation_old_phases_recover_from_old_root(self):
        self.phase("old", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z",
                   agent="/root/app_review")
        self.phase("new", "review", "2026-10-12T00:02:00Z", "2026-10-12T00:03:00Z",
                   agent="/root/app_review_fix", session_id="root-2")
        self.rollout("root-1", cwd=str(WORKSPACE), model="model-a",
                     events=[("2026-10-12T00:00:05Z", self.usage(5, 0, 0, 1, 6), {"total_tokens": 6})])
        self.rollout("child-old", session_id="root-1", agent_path="/root/app_review", parent="root-1",
                     events=[("2026-10-12T00:00:30Z", self.usage(20, 5, 0, 3, 23), {"total_tokens": 23})])
        self.rollout("root-2", cwd=str(WORKSPACE), model="model-b",
                     events=[("2026-10-12T00:02:05Z", self.usage(5, 0, 0, 1, 6), {"total_tokens": 6})])
        self.rollout("child-new", session_id="root-2", agent_path="/root/app_review_fix", parent="root-2",
                     events=[("2026-10-12T00:02:30Z", self.usage(40, 10, 0, 4, 44), {"total_tokens": 44})])
        result = self.analyze()
        phases = {p["phase_id"]: p for p in result["phases"]}
        self.assertTrue(phases["old"]["correlation_complete"], phases["old"]["usage_reasons"])
        self.assertTrue(phases["new"]["correlation_complete"], phases["new"]["usage_reasons"])
        self.assertEqual(15, phases["old"]["usage"]["input_tokens"])
        self.assertEqual(30, phases["new"]["usage"]["input_tokens"])
        orchestrator = result["ocx"]["orchestrator"]
        self.assertTrue(orchestrator["complete"], orchestrator["reasons"])
        self.assertEqual(["root-1", "root-2"], sorted(root["thread_id"] for root in orchestrator["roots"]))
        self.assertEqual(["model-a", "model-b"], orchestrator["models"])
        self.assertEqual(2, result["coverage"]["in_phase"]["requests"])
        self.assertIn("child-old#0", result["coverage"]["in_phase"]["request_ids"])
        self.assertNotIn("child-old#0", result["coverage"]["in_run_outside_phases"]["request_ids"])

    def test_codex_missing_session_agent_in_two_roots_is_ambiguous(self):
        self.phase("p", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z", agent="/root/author")
        self.rollout("root-1", cwd=str(WORKSPACE))
        self.rollout("child-1", session_id="root-1", agent_path="/root/author", parent="root-1",
                     events=[("2026-10-12T00:00:10Z", self.usage(), {"total_tokens": 23})])
        self.rollout("root-2", cwd=str(WORKSPACE))
        self.rollout("child-2", session_id="root-2", agent_path="/root/author", parent="root-2",
                     events=[("2026-10-12T00:00:20Z", self.usage(), {"total_tokens": 23})])
        phase = self.analyze()["phases"][0]
        self.assertFalse(phase["correlation_complete"])
        self.assertTrue(all(value is None for value in phase["usage"].values()))
        self.assertTrue(any("Ambiguous" in reason for reason in phase["usage_reasons"]))

    def test_codex_multi_model_orchestrator_stays_complete(self):
        self.phase("p", "implement", "2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z", agent="/root/author")
        self.rollout("root-1", cwd=str(WORKSPACE),
                     turn_contexts=[("2026-10-12T00:00:00Z", "model-a"), ("2026-10-12T00:00:15Z", "model-b")],
                     events=[("2026-10-12T00:00:10Z", self.usage(10, 0, 0, 1, 11), {"total_tokens": 11}),
                             ("2026-10-12T00:00:20Z", self.usage(20, 0, 0, 2, 22), {"total_tokens": 33})])
        self.rollout("child-1", session_id="root-1", agent_path="/root/author", parent="root-1",
                     events=[("2026-10-12T00:00:30Z", self.usage(5, 0, 0, 1, 6), {"total_tokens": 6})])
        orchestrator = self.analyze()["ocx"]["orchestrator"]
        self.assertTrue(orchestrator["complete"], orchestrator["reasons"])
        self.assertEqual(["model-a", "model-b"], orchestrator["models"])


if __name__ == "__main__":
    unittest.main()
