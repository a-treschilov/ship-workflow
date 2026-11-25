"""Remove obsolete workflow worktree commands while preserving surrounding hooks."""

from pathlib import Path


def migrate(root, settings, version):
    hooks = settings.get("hooks", {})
    managed = version.get("managed_settings", {}).get(".claude/settings.json", {}).get("hooks", {})
    legacy = "managed_settings" not in version
    link = root / ".claude/hooks"
    legacy = legacy and link.is_symlink() and str(link.readlink()) == "../.agents/workflows/ship-fix/hooks"
    for event, filename in (("WorktreeCreate", "worktree-create.sh"), ("WorktreeRemove", "worktree-remove.sh")):
        command = "${CLAUDE_PROJECT_DIR}/.claude/hooks/" + filename
        owned = sum(1 for entry in managed.get(event, []) if isinstance(entry, dict)
                    for hook in entry.get("hooks", [])
                    if isinstance(hook, dict) and hook.get("command") == command)
        if legacy and (link / filename).is_file():
            owned = 1
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"hooks.{event} must be a list")
        updated = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                updated.append(entry)
                continue
            remaining = []
            removed = False
            for hook in entry["hooks"]:
                value = hook.get("command", "") if isinstance(hook, dict) else ""
                if value == command and owned:
                    owned -= 1
                    removed = True
                    continue
                if filename in value and "/.claude/hooks/" in value:
                    raise ValueError(f"Unowned workflow hook remains in .claude/settings.json: {value}")
                remaining.append(hook)
            if remaining or not removed:
                updated.append({**entry, "hooks": remaining})
        if event in hooks:
            if updated:
                hooks[event] = updated
            else:
                del hooks[event]
    return settings
