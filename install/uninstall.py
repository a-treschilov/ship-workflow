#!/usr/bin/env python3
"""Remove installed workflow files and known hook registrations."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1]).resolve()
for relative in (".agents", ".agents/workflows", ".agents/skills", ".agents/roles", ".agents/platforms",
                 ".agents/platforms/claude", ".agents/platforms/codex",
                 ".claude", ".claude/agents", ".claude/skills", ".claude/references", ".codex", ".codex/agents"):
    path = root / relative
    if path.is_symlink():
        sys.exit(f"Cannot safely uninstall through symlinked directory: {path}")
marker = root / ".agents/.ship-version"
if not marker.is_file():
    sys.exit(f"not installed: {root}")
package = Path(__file__).resolve().parent
version = json.loads(marker.read_text())
owned_settings = version.get("managed_settings", {})
settings = {}
for relative in (".claude/settings.json", ".codex/hooks.json"):
    path = root / relative
    if path.is_symlink() or path.parent.is_symlink():
        sys.exit(f"Cannot safely update symlinked settings: {path}")
    if path.is_file():
        current = json.loads(path.read_text())
        if not isinstance(current, dict) or not isinstance(current.get("hooks", {}), dict):
            sys.exit(f"Cannot safely update malformed settings: {path}")
        permissions = current.get("permissions", {})
        if not isinstance(permissions, dict):
            sys.exit(f"Cannot safely update malformed permissions: {path}")
        managed = owned_settings.get(relative, {}).get("hooks", {})
        for event, entries in current.get("hooks", {}).items():
            if not isinstance(entries, list):
                sys.exit(f"Cannot safely update malformed hooks: {path}")
            for entry in entries:
                if entry in managed.get(event, []) or not isinstance(entry, dict):
                    continue
                for hook in entry.get("hooks", []):
                    command = hook.get("command", "") if isinstance(hook, dict) else ""
                    if any(name in command for name in ("codex_pre_tool_use.py", "claude_pre_tool_use.py",
                                                        "trusted-ship-helpers.py", "worktree-create.sh", "worktree-remove.sh")):
                        sys.exit(f"Unowned workflow hook remains in {path}; remove it before uninstalling")
        settings[relative] = current
script = root / ".agents/workflows/ship-fix/scripts/agent_models.py"
if script.is_file():
    try:
        spec = importlib.util.spec_from_file_location("installed_agent_models", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        models, _, aliases = module.parse_config(root / ".agents/agent-models.yaml")
        host = version.get("host", "both")
        if hasattr(module, "host_overrides"):
            overrides = module.host_overrides((root / ".agents/agent-models.yaml").read_text())
            targets = module._all_role_targets(models, aliases, root, host, overrides)
        else:
            targets = module._all_role_targets(models, aliases, root)
        for path, expected in targets.items():
            if path.is_file() and not path.is_symlink() and path.read_text() == expected:
                path.unlink()
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"Leaving generated agents in place: {error}", file=sys.stderr)

for relative in (".claude/settings.json", ".codex/hooks.json"):
    path = root / relative
    if not path.is_file():
        continue
    current = settings[relative]
    defaults = owned_settings.get(relative, {})
    hooks = current.get("hooks", {})
    for event, entries in defaults.get("hooks", {}).items():
        if isinstance(hooks.get(event), list):
            hooks[event] = [item for item in hooks[event] if item not in entries]
            if not hooks[event]:
                del hooks[event]
    if not hooks and "hooks" in current:
        del current["hooks"]
    allow = current.get("permissions", {}).get("allow")
    if isinstance(allow, list):
        current["permissions"]["allow"] = [item for item in allow
                                            if item not in defaults.get("allow", [])]
    path.write_text(json.dumps(current, indent=2) + "\n")

links = {".claude/scripts": "../.agents/workflows/ship-fix/scripts",
         ".claude/hooks": "../.agents/workflows/ship-fix/hooks",
         ".codex/scripts": "../.agents/workflows/ship-fix/scripts",
         ".codex/hooks": "../.agents/workflows/ship-fix/hooks",
         ".claude/references/ship-protocol.md": "../../.agents/workflows/ship-fix/PROTOCOL.md"}
for name in ("ship", "fix", "ship-analyze"):
    links[f".claude/skills/{name}"] = f"../../.agents/skills/{name}"
for relative, expected in links.items():
    path = root / relative
    if path.is_symlink() and str(path.readlink()) == expected:
        path.unlink()
for relative, expected in version.get("managed_files", {}).items():
    path = root / relative
    allowed = (relative.startswith(".agents/roles/") and Path(relative).parent.as_posix() == ".agents/roles"
               or relative in {".agents/platforms/claude/roles.json", ".agents/platforms/codex/roles.json"})
    if allowed and path.is_file() and not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest() == expected:
        path.unlink()
for relative in (".agents/workflows/ship-fix", ".agents/skills/ship", ".agents/skills/fix",
                 ".agents/skills/ship-analyze"):
    path = root / relative
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
marker.unlink()
print("Removed ship-workflow files and known hook registrations; project configuration preserved.")
