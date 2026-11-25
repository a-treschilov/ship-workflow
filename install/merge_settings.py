#!/usr/bin/env python3
"""Merge only the workflow's host settings, preserving project settings."""
import json
from pathlib import Path
import sys

template_path, target_path, host = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
template = json.loads(template_path.read_text())
existing = json.loads(target_path.read_text()) if target_path.exists() else {}
if not isinstance(existing, dict):
    raise ValueError(f"{target_path}: expected an object")
hooks = existing.setdefault("hooks", {})
if not isinstance(hooks, dict):
    raise ValueError(f"{target_path}: hooks must be an object")
for event, entries in template.get("hooks", {}).items():
    current = hooks.setdefault(event, [])
    if not isinstance(current, list):
        raise ValueError(f"{target_path}: hooks.{event} must be a list")
    for entry in entries:
        commands = [h.get("command", "") for item in current if isinstance(item, dict)
                    for h in item.get("hooks", []) if isinstance(h, dict)]
        known_claude = (host == "claude" and event == "PreToolUse" and any(
            "/.claude/hooks/trusted-ship-helpers.py" in c or "/.claude/hooks/claude_pre_tool_use.py" in c for c in commands))
        known_codex = host == "codex" and any(".agents/workflows/ship-fix/hooks/codex_pre_tool_use.py" in c for c in commands)
        if entry in current or known_claude or known_codex:
            continue
        current.append(entry)
if host == "claude":
    permissions = existing.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise ValueError(f"{target_path}: permissions must be an object")
    allow = permissions.setdefault("allow", [])
    if not isinstance(allow, list):
        raise ValueError(f"{target_path}: permissions.allow must be a list")
    for rule in template.get("permissions", {}).get("allow", []):
        if rule not in allow:
            allow.append(rule)
target_path.parent.mkdir(parents=True, exist_ok=True)
target_path.write_text(json.dumps(existing, indent=2) + "\n")
