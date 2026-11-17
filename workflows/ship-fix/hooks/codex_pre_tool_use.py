#!/usr/bin/env python3
"""Codex hook adapter for the shared trusted-command policy."""

import importlib.util
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("ship_trusted_policy", HERE / "trusted_policy.py")
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


def normalize(payload):
    if not isinstance(payload, dict):
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    return {
        "hook_event_name": payload.get("hook_event_name"),
        "tool_name": payload.get("tool_name"),
        "tool_input": tool_input,
        "cwd": payload.get("cwd"),
    }


def decide(payload):
    normalized = normalize(payload)
    if not policy.is_trusted(normalized):
        return {}
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "Trusted ship-workflow/test command (simple repo-bound invocation)",
    }}


def main():
    try:
        result = decide(json.load(sys.stdin))
    except (ValueError, OSError, TypeError, RuntimeError):
        result = {}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
