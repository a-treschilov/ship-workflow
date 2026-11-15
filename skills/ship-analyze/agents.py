"""Per-agent and per-task token accounting for resumable agents. Read-only; never reads raw transcripts into context.

Usage: python3 -I -B agents.py --subagents DIR [--ocx PATH] [--agent-type crumb-implementer] [--json OUT]

Each agent transcript is split into tasks at real prompts: the first prompt and every later message the coordinator
sends into the same agent. A request is counted once (by requestId). Its tokens come only from the exact requestId
row in the OCX log; a request without that row is reported as unmatched and never estimated. Tool calls are grouped
by path and command text; anything ambiguous lands in `other`. Output sizes are characters of tool results.
"""

import argparse
import collections
import importlib.util
import json
from pathlib import Path
import re
import statistics
import sys

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("ship_analyze_telemetry", HERE / "telemetry.py")
telemetry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(telemetry)

COORDINATOR_MARKER = "The coordinator sent a message"
CATEGORIES = ("artifacts", "tests", "lint", "inspect", "read-code", "edit-code", "handback", "other")
RE_TESTS = re.compile(r"make test|pytest|npm (run )?test|go test|eslint")
RE_LINT = re.compile(r"eslint|prettier|tsc --noEmit|go vet")
RE_INSPECT = re.compile(r"^\s*(cd\s+\S+\s*&&\s*)?(cat|head|tail|grep|sed -n|ls|wc|find|rg|git (diff|show|status|log))\b")
RE_ARTIFACT_CMD = re.compile(r"ship_state\.py|ship_artifacts\.py|\.ship-runs|record-evidence|sha256sum|shasum")
RE_ARTIFACT_PATH = re.compile(r"\.ship-runs|/evidence/|/telemetry/|applied|capsule|manifest|matrix")


def categorize(name, tool_input):
    """Category from tool name and input only; no content inspection."""
    if name == "SubagentHandback":
        return "handback"
    if name in {"Read", "Edit", "Write"}:
        path = str(tool_input.get("file_path", ""))
        if RE_ARTIFACT_PATH.search(path):
            return "artifacts"
        if name == "Read":
            return "read-code"
        return "edit-code"
    if name == "Bash":
        command = str(tool_input.get("command", ""))
        if RE_ARTIFACT_CMD.search(command):
            return "artifacts"
        if RE_TESTS.search(command):
            return "tests"
        if RE_LINT.search(command):
            return "lint"
        if RE_INSPECT.search(command):
            return "inspect"
        return "other"
    return "other"


def is_prompt(record):
    """A real coordinator prompt: a user message with text (string or text blocks) and no tool results.

    Resume messages arrive as text blocks, not strings, so both shapes must count. Meta and system reminders do not.
    """
    if record.get("type") != "user":
        return False
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        # Coordinator messages sent while the agent works are stored as meta records; they are real prompts.
        if record.get("isMeta"):
            return content.startswith(COORDINATOR_MARKER)
        return not content.startswith("<system-reminder>")
    if record.get("isMeta"):
        return False
    if not isinstance(content, list) or any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return False
    texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return any(text and not text.startswith("<system-reminder>") for text in texts)


def prompt_chars(record):
    content = record["message"]["content"]
    if isinstance(content, str):
        return len(content)
    return sum(len(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")


def result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def load_ocx_rows(wanted):
    """Exact requestId rows only; ambiguous duplicates are kept and flagged so they are not summed silently."""
    rows, _ = telemetry.read_ocx_log(wanted, None)
    return rows or {}


def usage_of(row):
    usage = row.get("usage") or {}
    return {"input": usage.get("inputTokens"), "output": usage.get("outputTokens"),
            "cache_read": usage.get("cacheReadInputTokens", usage.get("cachedInputTokens")),
            "cache_write": usage.get("cacheCreationInputTokens"),
            "ambiguous": bool(row.get("_ambiguous")), "status": row.get("status")}


def analyze_agent(path, rows_by_id):
    records = list(telemetry.records(path))
    meta_path = path.with_suffix(".meta.json")
    meta = telemetry.read(meta_path) or {}
    tasks = []
    current = None
    seen_requests = set()
    results = {}
    for record in records:
        if record.get("_invalid"):
            continue
        if is_prompt(record):
            current = {"prompt_chars": prompt_chars(record), "started": record.get("timestamp"),
                       "requests": [], "tools": collections.Counter(), "tool_ids": {}, "applied_files": 0}
            tasks.append(current)
            continue
        if current is None:
            current = {"prompt_chars": 0, "started": record.get("timestamp"), "requests": [],
                       "tools": collections.Counter(), "tool_ids": {}, "applied_files": 0}
            tasks.append(current)
        if record.get("type") == "assistant":
            rid = record.get("requestId")
            if isinstance(rid, str) and rid not in seen_requests:
                seen_requests.add(rid)
                message_usage = (record.get("message") or {}).get("usage") or {}
                current["requests"].append({"requestId": rid, "timestamp": record.get("timestamp"),
                                            "transcript_input": message_usage.get("input_tokens")})
            for block in (record.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    category = categorize(block.get("name", ""), block.get("input") or {})
                    current["tool_ids"][block.get("id")] = category
                    current["tools"][category] += 1
                    file_path = str((block.get("input") or {}).get("file_path", ""))
                    if file_path.endswith("applied.json"):
                        current["applied_files"] += 1
        elif record.get("type") == "user":
            for block in (record.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    results[block.get("tool_use_id")] = len(result_text(block.get("content")))
    for task in tasks:
        task["requests_total"] = len(task["requests"])
        matched = [rows_by_id[r["requestId"]] for r in task["requests"] if r["requestId"] in rows_by_id]
        usage = [usage_of(row) for row in matched]
        task["matched"] = len(matched)
        task["unmatched"] = task["requests_total"] - len(matched)
        task["ambiguous"] = sum(1 for u in usage if u["ambiguous"])
        inputs = [u["input"] for u in usage if isinstance(u["input"], int)]
        task["input_sum"] = sum(inputs) if inputs else None
        task["input_first"] = inputs[0] if inputs else None
        task["input_last"] = inputs[-1] if inputs else None
        task["input_mean"] = int(statistics.mean(inputs)) if inputs else None
        task["cache_read_sum"] = sum(u["cache_read"] for u in usage if isinstance(u["cache_read"], int)) if usage else None
        task["output_sum"] = sum(u["output"] for u in usage if isinstance(u["output"], int)) if usage else None
        task["transcript_input_sum"] = sum(r["transcript_input"] or 0 for r in task["requests"]) or None
        task["tool_calls"] = sum(task["tools"].values())
        result_chars = collections.Counter()
        for tool_id, category in task["tool_ids"].items():
            result_chars[category] += results.get(tool_id, 0)
        task["result_chars"] = dict(result_chars)
        task["tools"] = dict(task["tools"])
        del task["tool_ids"]
        task["requests"] = [{"requestId": r["requestId"], "timestamp": r["timestamp"]} for r in task["requests"]]
    return {"agent_id": path.stem.replace("agent-", ""), "agent_type": meta.get("agentType"),
            "description": meta.get("description"), "tasks": tasks}


def summarize(agents):
    rows = []
    for agent in agents:
        tasks = agent["tasks"]
        inputs = [t["input_sum"] for t in tasks if t["input_sum"] is not None]
        requests = sum(t["requests_total"] for t in tasks)
        matched = sum(t["matched"] for t in tasks)
        rows.append({"agent_id": agent["agent_id"], "agent_type": agent["agent_type"],
                     "description": agent["description"], "tasks": len(tasks), "requests": requests,
                     "matched": matched, "unmatched": requests - matched,
                     "input_sum": sum(inputs) if inputs else None,
                     "output_sum": sum(t["output_sum"] or 0 for t in tasks) if inputs else None,
                     "cache_read_sum": sum(t["cache_read_sum"] or 0 for t in tasks) if inputs else None})
    return rows


def markdown(agents):
    lines = ["| agent | task | prompt | requests | matched | input Σ | input first→last | output Σ | tools (artifacts/tests/read/edit/other) |",
             "|---|---|---|---|---|---|---|---|---|"]
    for agent in agents:
        for index, task in enumerate(agent["tasks"], 1):
            t = task["tools"]
            tools = "/".join(str(t.get(k, 0)) for k in ("artifacts", "tests", "read-code", "edit-code", "other"))
            span = "—" if task["input_first"] is None else f"{task['input_first']}→{task['input_last']}"
            lines.append(f"| {agent['agent_id'][:8]} | {index} | {task['prompt_chars']} | {task['requests_total']} | "
                         f"{task['matched']} | {task['input_sum'] if task['input_sum'] is not None else 'null'} | "
                         f"{span} | {task['output_sum'] if task['output_sum'] is not None else 'null'} | {tools} |")
    return "\n".join(lines)


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--subagents", required=True, help="directory with agent-*.jsonl transcripts")
    parser.add_argument("--ocx", help="OCX usage log (default: ~/.opencodex/usage.jsonl)")
    parser.add_argument("--agent-type", help="only agents of this type")
    parser.add_argument("--json", help="write the full per-task JSON here (must not exist)")
    args = parser.parse_args(argv)
    if args.ocx:
        telemetry.OCX_LOG = Path(args.ocx)
    directory = Path(args.subagents)
    transcripts = sorted(directory.glob("agent-*.jsonl"))
    requested = set()
    for path in transcripts:
        for record in telemetry.records(path):
            rid = record.get("requestId")
            if record.get("type") == "assistant" and isinstance(rid, str) and telemetry.RE_OCX_ID.fullmatch(rid):
                requested.add(rid)
    rows = load_ocx_rows(requested)
    agents = [analyze_agent(path, rows) for path in transcripts]
    if args.agent_type:
        agents = [a for a in agents if a["agent_type"] == args.agent_type]
    print(markdown(agents))
    print()
    for row in summarize(agents):
        print(json.dumps(row, ensure_ascii=False))
    if args.json:
        target = Path(args.json)
        if target.exists():
            raise SystemExit(f"Refusing to overwrite {target}")
        target.write_text(json.dumps({"agents": agents, "limits": [
            "Tokens are exact only for requests matched by requestId in the OCX log; unmatched counts are reported, not estimated.",
            "input is OCX inputTokens, which includes cache reads; cache_read is reported separately.",
            "Tool output sizes are characters of tool results, not model tokens.",
            "Agent-level totals are sums over tasks; a resumed task's context grows, so input_first→input_last shows growth."]},
            ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
