"""Isolated roundtrip_audit tests; set SHIP_TEST_TMPDIR to the session scratchpad."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "roundtrip_audit.py"
spec = importlib.util.spec_from_file_location("roundtrip_audit", SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class RoundtripAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ["SHIP_TEST_TMPDIR"], prefix="roundtrip-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_transcript(self, records):
        path = self.root / "transcript.jsonl"
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        return path

    def token(self, input_tokens):
        return {"type": "event_msg", "payload": {"type": "token_count",
                "info": {"last_token_usage": {"input_tokens": input_tokens}}}}

    def exec(self, input_text):
        return {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
                                                     "input": input_text}}

    def function(self, name="wait_agent"):
        return {"type": "response_item", "payload": {"type": "function_call", "name": name}}

    def test_counts_and_missed_batching(self):
        records = [
            self.token(1000),
            self.exec('text(await tools.exec_command({cmd:"ls"}))'),
            self.exec('await Promise.all([tools.exec_command({cmd:"a"}), tools.exec_command({cmd:"b"})])'),
            self.exec('text(await tools.clock__curr_time({})); text(await tools.exec_command({cmd:"c"}))'),
            self.function(),
            self.exec('const patch = "*** Add File: /x/run/inputs/p-start.json + {} *** End Patch"; '
                      'text(await tools.apply_patch(patch))'),
            self.exec('text(JSON.stringify(await tools.exec_command({cmd:"python3 ship_state.py digest --contract C --repo shared"})))'),
            self.exec('text(JSON.stringify(await tools.exec_command({cmd:"python3 ship_state.py digest --contract C --repo api"})))'),
            self.token(2000),
        ]
        result = audit.audit(str(self.write_transcript(records)))
        self.assertEqual(2, result["requests"])
        self.assertEqual(7, result["outer_tool_calls"])
        self.assertEqual(6, result["exec_cells"])
        self.assertEqual(1, result["batched_cells"])
        self.assertEqual(4, result["single_command_cells"])
        self.assertEqual(1, result["clock_calls"])
        self.assertEqual(1, result["telemetry_apply_patch"])
        self.assertEqual(1000, result["context_start_tokens"])
        self.assertEqual(2000, result["context_end_tokens"])
        self.assertEqual([{"operation": "digest", "cells": 2, "fans": 1, "example_repos": ["api", "shared"]}],
                         result["top_missed_batching"])

    def test_missing_transcript_reports_clearly(self):
        result = audit.audit(str(self.root))
        self.assertIsNone(result["transcript"])
        self.assertEqual(0, result["requests"])
        self.assertIn("no transcript found", result["note"])

    def test_malformed_lines_are_skipped(self):
        path = self.root / "malformed.jsonl"
        path.write_text(json.dumps(self.token(500)) + "\nnot json\n" + json.dumps(self.token(900)) + "\n")
        result = audit.audit(str(path))
        self.assertEqual(2, result["requests"])
        self.assertEqual(500, result["context_start_tokens"])
        self.assertEqual(900, result["context_end_tokens"])


if __name__ == "__main__":
    unittest.main()
