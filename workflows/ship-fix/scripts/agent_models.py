#!/usr/bin/env python3
"""Check or sync Claude and Codex agents from shared OCX model assignments."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys


def resolve_root() -> Path:
    """Find the project root from CRUMB_SHIP_ROOT or the nearest .agents marker, not install depth."""
    override = os.environ.get("CRUMB_SHIP_ROOT")
    if override and Path(override).is_dir():
        return Path(override).resolve()
    start = Path.cwd().resolve()
    for candidate in (start, *start.parents):
        if ((candidate / ".agents/agent-models.yaml").is_file()
                or (candidate / ".agents/workflows/ship-fix/scripts/ship_state.py").is_file()):
            return candidate
    return Path(__file__).resolve().parents[4]


ROOT = resolve_root()
CONFIG = ROOT / ".agents/agent-models.yaml"
CLAUDE_ROLE_CONFIG = ROOT / ".agents/platforms/claude/roles.json"
CODEX_ROLE_CONFIG = ROOT / ".agents/platforms/codex/roles.json"
ROLE_SOURCE_DIR = ROOT / ".agents/roles"
ROLES = (
    "crumb-implementer",
    "crumb-reviewer",
    "crumb-plan-checker",
    "crumb-slicer",
)


def _valid_model_id(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+~-]*(?:\[[A-Za-z0-9_-]+\])?", value) is not None


def _scalar(value: str, path: Path, number: int) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        result = value[1:-1]
        if '"' in result or "\\" in result:
            raise ValueError(f"{path}:{number}: unsupported quoted scalar")
    elif value.startswith("'") and value.endswith("'"):
        result = value[1:-1]
        if "'" in result:
            raise ValueError(f"{path}:{number}: unsupported quoted scalar")
    elif value and not value.startswith(("#", "[", "{", "&", "*", "!", ">", "|")):
        result = value.split(" #", 1)[0].rstrip()
    else:
        raise ValueError(f"{path}:{number}: unsupported scalar")
    if not result:
        raise ValueError(f"{path}:{number}: empty value")
    if not _valid_model_id(result):
        raise ValueError(f"{path}:{number}: invalid model ID {result!r}")
    return result


def parse_config_text(text: str, path: Path = CONFIG) -> tuple[dict[str, str], list[str], dict[str, str]]:
    """Parse the limited YAML subset used here; reject unrecognized structure."""
    sections: dict[str, list[tuple[int, str]]] = {}
    active: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            match = re.fullmatch(r"([a-z_]+):\s*", line)
            if not match:
                raise ValueError(f"{path}:{number}: unsupported YAML syntax")
            active = match.group(1)
            if active not in ("models", "available_models", "claude_aliases"):
                raise ValueError(f"{path}:{number}: unknown section {active!r}")
            if active in sections:
                raise ValueError(f"{path}:{number}: duplicate section {active!r}")
            sections[active] = []
        elif active is None:
            raise ValueError(f"{path}:{number}: content before a section")
        else:
            sections[active].append((number, line))

    if not {"models", "available_models"}.issubset(sections):
        missing = {"models", "available_models"} - sections.keys()
        raise ValueError(f"missing section(s): {', '.join(sorted(missing))}")

    models: dict[str, str] = {}
    for number, line in sections["models"]:
        match = re.fullmatch(r"  ([A-Za-z0-9_-]+):\s*(.+?)\s*", line)
        if not match:
            raise ValueError(f"{path}:{number}: invalid model mapping")
        role, raw_value = match.groups()
        if role not in ROLES:
            raise ValueError(f"{path}:{number}: unknown role {role!r}")
        if role in models:
            raise ValueError(f"{path}:{number}: duplicate role {role!r}")
        models[role] = _scalar(raw_value.split(" #", 1)[0], path, number)

    missing_roles = set(ROLES) - models.keys()
    if missing_roles:
        raise ValueError(f"missing model entries: {', '.join(sorted(missing_roles))}")

    available: list[str] = []
    for number, line in sections["available_models"]:
        match = re.fullmatch(r"  -\s*(.+?)\s*", line)
        if not match:
            raise ValueError(f"{path}:{number}: invalid available_models entry")
        model_id = _scalar(match.group(1).split(" #", 1)[0], path, number)
        if model_id in available:
            raise ValueError(f"{path}:{number}: duplicate available model ID {model_id!r}")
        available.append(model_id)
    aliases: dict[str, str] = {}
    for number, line in sections.get("claude_aliases", []):
        match = re.fullmatch(r"  ([A-Za-z0-9._:/+~-]+):\s+(.+?)\s*", line)
        if not match:
            raise ValueError(f"{path}:{number}: invalid Claude alias mapping")
        model_id, raw_alias = match.groups()
        if model_id in aliases:
            raise ValueError(f"{path}:{number}: duplicate Claude alias for {model_id!r}")
        aliases[model_id] = _scalar(raw_alias.split(" #", 1)[0], path, number)
    return models, available, aliases


def parse_config(path: Path = CONFIG) -> tuple[dict[str, str], list[str], dict[str, str]]:
    return parse_config_text(path.read_text(), path)


def _load_role_config(path: Path) -> dict[str, dict[str, object]]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{path}: cannot read role configuration: {error}") from error
    if not isinstance(value, dict) or set(value) != set(ROLES):
        raise ValueError(f"{path}: expected exactly these role names: {', '.join(ROLES)}")
    return value


def _render_role(role: str, models: dict[str, str], aliases: dict[str, str], root: Path) -> dict[Path, str]:
    claude_config = _load_role_config(root / ".agents/platforms/claude/roles.json")[role]
    codex_config = _load_role_config(root / ".agents/platforms/codex/roles.json")[role]
    if claude_config.get("name") != role or codex_config.get("name") != role:
        raise ValueError(f"{role}: role name does not match platform configuration")
    source = root / ".agents/roles" / f"{role}.md"
    body = source.read_text().strip() + "\n"
    description = claude_config.get("description")
    tools = claude_config.get("tools")
    permission_mode = claude_config.get("permissionMode")
    if not isinstance(description, str) or not isinstance(tools, list) or not all(isinstance(item, str) for item in tools):
        raise ValueError(f"{role}: invalid Claude role metadata")
    if permission_mode is not None and not isinstance(permission_mode, str):
        raise ValueError(f"{role}: invalid Claude permission mode")
    if not models.get(role):
        raise ValueError(f"{role}: missing OCX model assignment")

    claude_fields = [
        "---",
        f"name: {role}",
        f"description: {description}",
        f"tools: {', '.join(tools)}",
        f"model: {aliases[models[role]]}",
    ]
    if permission_mode:
        claude_fields.append(f"permissionMode: {permission_mode}")
    claude_text = "\n".join([*claude_fields, "---", "", body])

    codex_description = codex_config.get("description")
    if not isinstance(codex_description, str) or "model_hint" in codex_config:
        raise ValueError(f"{role}: invalid Codex role metadata")
    codex_text = (
        f'name = {json.dumps(role)}\n'
        f'description = {json.dumps(codex_description, ensure_ascii=False)}\n'
        f'model = {json.dumps(models[role], ensure_ascii=False)}\n'
        f'developer_instructions = {json.dumps(body, ensure_ascii=False)}\n'
    )
    return {
        root / ".claude/agents" / f"{role}.md": claude_text,
        root / ".codex/agents" / f"{role}.toml": codex_text,
    }


def _all_role_targets(models: dict[str, str], aliases: dict[str, str], root: Path = ROOT) -> dict[Path, str]:
    if set(models) != set(ROLES):
        raise ValueError("model assignments must include each configured role exactly once")
    for model_id in models.values():
        if not _valid_model_id(model_id) or not _valid_model_id(aliases.get(model_id, "")):
            raise ValueError(f"missing or invalid OCX model/Claude alias: {model_id!r}; run ocx_models.py")
    targets: dict[Path, str] = {}
    for role in ROLES:
        targets.update(_render_role(role, models, aliases, root))
    return targets


def check(models: dict[str, str], aliases: dict[str, str], root: Path = ROOT) -> list[str]:
    targets = _all_role_targets(models, aliases, root)
    return sorted({path.stem.removesuffix(".md").removesuffix(".toml") for path, expected in targets.items()
                   if not path.is_file() or path.read_text() != expected})


def sync(models: dict[str, str], aliases: dict[str, str], root: Path = ROOT) -> list[str]:
    targets = _all_role_targets(models, aliases, root)
    changed: set[str] = set()
    for path, rendered in targets.items():
        if path.is_file() and path.read_text() == rendered:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
        changed.add(path.stem.removesuffix(".md").removesuffix(".toml"))
    return sorted(changed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "sync"))
    args = parser.parse_args()
    try:
        models, available, aliases = parse_config()
        missing = set(models.values()) - set(available)
        if missing:
            raise ValueError(f"role models absent from OCX catalog: {', '.join(sorted(missing))}")
        if args.command == "check":
            drift = check(models, aliases)
            if drift:
                print(f"Agent adapter drift: {', '.join(drift)}. Run: python3 {Path(__file__).resolve()} sync, then reload the configured host.", file=sys.stderr)
                return 1
            print("Claude and Codex agent files match the shared role sources and OCX model assignments.")
            return 0
        changed = sync(models, aliases)
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2
    if changed:
        print(f"Updated native agent files: {', '.join(changed)}. Reload the configured host to load them.")
    else:
        print("Native agent files already match the shared role sources; no files changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
