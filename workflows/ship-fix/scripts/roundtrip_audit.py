#!/usr/bin/env python3
"""Read-only roundtrip audit for a Codex/Claude transcript.

Streams a JSONL transcript without dumping its raw text into the model context and
prints a compact JSON summary: model requests, outer tool calls, exec cells, batched
cells, single-command cells, clock calls, telemetry apply_patch calls, context token
range, and the top patterns where adjacent single-repo commands should have been one
--all-repos/--repos call or one Promise.all cell.

It never writes or rewrites run artifacts. Accepts a transcript path or a run dir; a
run dir is searched for a transcript file, and the script reports clearly when none
is found instead of crashing.
"""
import argparse
import json
import re
import sys
from pathlib import Path

PHASE_FILE_RE = re.compile(
    r"\*\*\* (?:Add|Update) File: [^\n]*(?:telemetry/|inputs/)[^\n]*(?:-start\.json|-end\.json)")
OP_REPO_RE = re.compile(
    r"\b(digest|review-budget|validate-history|record-evidence|packet|gates|checks)\b"
    r"[^\n]{0,240}?--repo\s+([A-Za-z0-9_.-]+)")


def find_transcript(path):
    path = Path(path).expanduser()
    if path.is_file():
        return path
    if path.is_dir():
        candidates = sorted(path.glob("*.jsonl"))
        if candidates:
            return candidates[0]
    return None


def iter_lines(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def telemetry_apply_patch_count(input_text):
    """Count apply_patch calls that write phase telemetry (telemetry/ paths or phase events)."""
    calls = input_text.count("tools.apply_patch(")
    if calls and PHASE_FILE_RE.search(input_text):
        return calls
    return 0


def audit(path):
    transcript = find_transcript(path)
    if transcript is None:
        return {"transcript": None, "requests": 0, "outer_tool_calls": 0, "exec_cells": 0,
                "batched_cells": 0, "single_command_cells": 0, "clock_calls": 0,
                "telemetry_apply_patch": 0, "context_start_tokens": None,
                "context_end_tokens": None, "top_missed_batching": [],
                "note": f"no transcript found in {path}"}
    requests = 0
    outer_tool_calls = 0
    exec_inputs = []
    context_values = []
    for record in iter_lines(transcript):
        if not isinstance(record, dict):
            continue
        kind = record.get("type")
        payload = record.get("payload") or {}
        if kind == "event_msg" and payload.get("type") == "token_count":
            requests += 1
            info = payload.get("info") or {}
            usage = info.get("last_token_usage") or info.get("total_token_usage") or {}
            if isinstance(usage.get("input_tokens"), int):
                context_values.append(usage["input_tokens"])
        elif kind == "response_item":
            item_type = payload.get("type")
            if item_type in ("custom_tool_call", "function_call"):
                outer_tool_calls += 1
                if item_type == "custom_tool_call" and payload.get("name") == "exec":
                    exec_inputs.append(payload.get("input") or "")
    exec_cells = len(exec_inputs)
    batched_cells = 0
    single_command_cells = 0
    clock_calls = 0
    telemetry_apply_patch = 0
    cell_ops = []
    for input_text in exec_inputs:
        if "Promise.all" in input_text:
            batched_cells += 1
        elif input_text.count("tools.exec_command(") == 1:
            single_command_cells += 1
        clock_calls += input_text.count("clock__curr_time")
        telemetry_apply_patch += telemetry_apply_patch_count(input_text)
        cell_ops.append({(match[0], match[1]) for match in OP_REPO_RE.findall(input_text)})
    fans = []
    index = 0
    while index < len(cell_ops):
        if len(cell_ops[index]) == 1:
            op, repo = next(iter(cell_ops[index]))
            repos = [repo]
            cursor = index + 1
            while cursor < len(cell_ops) and len(cell_ops[cursor]) == 1:
                next_op, next_repo = next(iter(cell_ops[cursor]))
                if next_op == op and next_repo not in repos:
                    repos.append(next_repo)
                    cursor += 1
                else:
                    break
            if len(repos) >= 2:
                fans.append((op, repos))
            index = cursor
        else:
            index += 1
    summary = {}
    for op, repos in fans:
        entry = summary.setdefault(op, {"operation": op, "cells": 0, "fans": 0, "repos": set()})
        entry["cells"] += len(repos)
        entry["fans"] += 1
        entry["repos"].update(repos)
    top = [{"operation": entry["operation"], "cells": entry["cells"], "fans": entry["fans"],
            "example_repos": sorted(entry["repos"])[:8]}
           for entry in sorted(summary.values(), key=lambda item: (-item["cells"], item["operation"]))[:10]]
    return {"transcript": str(transcript), "requests": requests, "outer_tool_calls": outer_tool_calls,
            "exec_cells": exec_cells, "batched_cells": batched_cells,
            "single_command_cells": single_command_cells, "clock_calls": clock_calls,
            "telemetry_apply_patch": telemetry_apply_patch,
            "context_start_tokens": context_values[0] if context_values else None,
            "context_end_tokens": context_values[-1] if context_values else None,
            "top_missed_batching": top}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="Codex/Claude transcript file or a run directory containing one")
    args = parser.parse_args()
    print(json.dumps(audit(args.path), separators=(",", ":"), ensure_ascii=False))


if __name__ == "__main__":
    main()
