#!/usr/bin/env python3
"""Migrate settings before replacing the installed workflow."""
import importlib.util
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
path = root / ".claude/settings.json"
spec = importlib.util.spec_from_file_location("worktree_migration", Path(__file__).with_name("worktree_migration.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
settings = json.loads(path.read_text())
version = json.loads((root / ".agents/.ship-version").read_text())
module.migrate(root, settings, version)
path.write_text(json.dumps(settings, indent=2) + "\n")
