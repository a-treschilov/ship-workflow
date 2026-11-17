"""Per-agent/per-task accounting on synthetic transcripts and an OCX log; exact requestId joins only."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve()
INSTALLED = HERE.parents[4].name == ".agents"
WORKSPACE = HERE.parents[5] if INSTALLED else HERE.parents[4]
SKILLS = WORKSPACE / (".agents/skills" if INSTALLED else "skills")
MODULE = SKILLS / "ship-analyze/agents.py"
spec = importlib.util.spec_from_file_location("ship_agents", MODULE)
agents = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agents)


def line(**kwargs):
    return json.dumps(kwargs, ensure_ascii=False)


class ShipAgentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("SHIP_TEST_TMPDIR"), prefix="agents-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.subagents = self.root / "subagents"
        self.subagents.mkdir()
        (self.subagents / "agent-abc.meta.json").write_text(json.dumps(
            {"agentType": "implementer", "description": "synthetic"}))
        transcript = [
            line(type="user", timestamp="2026-10-20T10:00:00Z", message={"role": "user", "content": "first task"}),
            line(type="assistant", timestamp="2026-10-20T10:00:01Z", requestId="ocx-0001",
                 message={"id": "m1", "usage": {"input_tokens": 1}, "content": [
                     {"type": "tool_use", "id": "t1", "name": "Bash",
                      "input": {"command": "python3 ship_artifacts.py --task T run r1 -- echo"}}]}),
            line(type="user", timestamp="2026-10-20T10:00:02Z", message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "x" * 100}]}),
            line(type="assistant", timestamp="2026-10-20T10:00:03Z", requestId="ocx-0002",
                 message={"id": "m2", "content": [
                     {"type": "tool_use", "id": "t2", "name": "Edit",
                      "input": {"file_path": "/repo/app/Service.ts"}}]}),
            line(type="user", timestamp="2026-10-20T10:00:04Z", message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t2", "content": "ok"}]}),
            line(type="user", timestamp="2026-10-20T11:00:00Z", isMeta=True,
                 message={"role": "user", "content": "The coordinator sent a message while you were working:\nfix finding 2"}),
            line(type="assistant", timestamp="2026-10-20T11:00:01Z", requestId="ocx-0003",
                 message={"id": "m3", "content": [{"type": "text", "text": "done"}]}),
            line(type="assistant", timestamp="2026-10-20T11:00:02Z", requestId="ocx-0004",
                 message={"id": "m4", "content": [{"type": "text", "text": "more"}]}),
        ]
        (self.subagents / "agent-abc.jsonl").write_text("\n".join(transcript) + "\n")
        self.ocx = self.root / "usage.jsonl"
        self.ocx.write_text("\n".join([
            line(requestId="ocx-0001", status=200, usage={"inputTokens": 5000, "cacheReadInputTokens": 4000,
                                                           "outputTokens": 10}),
            line(requestId="ocx-0002", status=200, usage={"inputTokens": 6000, "outputTokens": 0}),
            line(requestId="ocx-0003", status=200, usage={"inputTokens": 7000, "outputTokens": 20}),
        ]) + "\n")

    def run_main(self):
        target = self.root / "out.json"
        agents.telemetry.OCX_LOG = self.ocx
        self.assertEqual(0, agents.main(["--subagents", str(self.subagents), "--ocx", str(self.ocx),
                                         "--agent-type", "implementer", "--json", str(target)]))
        return json.loads(target.read_text())["agents"]

    def test_tasks_split_at_prompts_and_tokens_join_exactly(self):
        (agent,) = self.run_main()
        self.assertEqual("implementer", agent["agent_type"])
        first, second = agent["tasks"]
        self.assertEqual(2, first["requests_total"])
        self.assertEqual(2, first["matched"])
        self.assertEqual(11000, first["input_sum"])
        self.assertEqual(4000, first["cache_read_sum"])
        self.assertEqual(5000, first["input_first"])
        self.assertEqual(6000, first["input_last"])
        self.assertEqual(1, second["unmatched"])
        self.assertEqual(7000, second["input_sum"])

    def test_tools_are_categorized_and_sized_per_task(self):
        (agent,) = self.run_main()
        first = agent["tasks"][0]
        self.assertEqual({"artifacts": 1, "edit-code": 1}, first["tools"])
        self.assertEqual({"artifacts": 100, "edit-code": 2}, first["result_chars"])

    def test_missing_ocx_row_is_null_not_zero(self):
        (agent,) = self.run_main()
        second = agent["tasks"][1]
        self.assertEqual(1, second["unmatched"])
        self.assertEqual(7000, second["input_sum"])
        self.assertEqual(0, second["tool_calls"])
        self.assertEqual({}, second["result_chars"])

    def test_refuses_to_overwrite_output(self):
        target = self.root / "exists.json"
        target.write_text("{}")
        agents.telemetry.OCX_LOG = self.ocx
        with self.assertRaises(SystemExit):
            agents.main(["--subagents", str(self.subagents), "--ocx", str(self.ocx), "--json", str(target)])


if __name__ == "__main__":
    unittest.main()
