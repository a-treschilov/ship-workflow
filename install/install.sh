#!/usr/bin/env bash
set -euo pipefail

# Install a pinned ship/fix workflow snapshot into a project's .agents/ from git.
# Run from the project root (the directory containing .agents).
#   ./install.sh v1.0.0 --host both
# Source selection: SHIP_SRC (local checkout) > SHIP_REMOTE clone at <ref> > this checkout.

usage() {
  cat >&2 <<'EOF'
Usage: install.sh <tag-or-ref> [--dev] [--host claude|codex|both] [--models inherit|ocx]
Run from the project root (the directory containing .agents).
  SHIP_REMOTE  git remote to clone <ref> from
  SHIP_SRC     local checkout to copy from (takes precedence)
EOF
}

ref="${1:-}"
[[ $# -gt 0 ]] && shift
dev=0
host=""
model_mode=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev) dev=1; shift ;;
    --sync) shift ;;
    --host) [[ $# -ge 2 ]] || { usage; exit 2; }; host="$2"; shift 2 ;;
    --models) [[ $# -ge 2 ]] || { usage; exit 2; }; model_mode="$2"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done
if [[ -z "$ref" ]]; then usage; exit 2; fi
case "$host" in ''|claude|codex|both) ;; *) usage; exit 2 ;; esac
case "$model_mode" in ''|inherit|ocx) ;; *) usage; exit 2 ;; esac

project_root="$(pwd)"
if [[ ! -d "$project_root/.agents" ]]; then
  echo "Not a project root (missing .agents): $project_root" >&2
  exit 2
fi
if [[ -L "$project_root/.agents" ]]; then
  echo "Refusing to modify symlinked project configuration: $project_root/.agents" >&2
  exit 2
fi
switching=0
old_host=""
if [[ -f "$project_root/.agents/.ship-version" ]]; then
  old_host="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("host", "both"))' "$project_root/.agents/.ship-version")"
  case "$old_host" in claude|codex|both) ;; *) echo "Invalid installed host: $old_host" >&2; exit 2 ;; esac
  if [[ -n "$host" && "$host" != "$old_host" ]]; then
    switching=1
  else
    host="$old_host"
  fi
fi
if [[ -z "$host" ]]; then
  claude_found=0; codex_found=0
  command -v claude >/dev/null 2>&1 && claude_found=1
  command -v codex >/dev/null 2>&1 && codex_found=1
  if [[ "$claude_found$codex_found" == 10 ]]; then host=claude
  elif [[ "$claude_found$codex_found" == 01 ]]; then host=codex
  elif [[ -t 0 ]]; then
    printf 'Select host (claude/codex/both): ' >&2
    read -r host
  else
    echo 'Specify --host claude, codex, or both.' >&2; exit 2
  fi
fi
case "$host" in claude|codex|both) ;; *) echo "Invalid host: $host" >&2; exit 2 ;; esac
paths=(.agents/workflows .agents/skills .agents/roles .agents/platforms .agents/platforms/claude
  .agents/platforms/codex .agents/agent-models.yaml .agents/.ship-version
  .agents/platforms/claude/roles.json .agents/platforms/codex/roles.json)
for role in implementer reviewer plan-checker slicer; do
  paths+=(".agents/roles/$role.md")
  [[ "$host" == claude || "$host" == both || "$switching" -eq 1 ]] && paths+=(".claude/agents/$role.md")
  [[ "$host" == codex || "$host" == both || "$switching" -eq 1 ]] && paths+=(".codex/agents/$role.toml")
done
[[ "$host" == claude || "$host" == both ]] && paths+=(.claude .claude/agents .claude/skills .claude/references .claude/settings.json)
[[ "$host" == codex || "$host" == both ]] && paths+=(.codex .codex/agents .codex/hooks.json)
if [[ "$switching" -eq 1 ]]; then
  [[ "$old_host" == claude || "$old_host" == both ]] && paths+=(.claude .claude/agents .claude/skills .claude/references .claude/settings.json)
  [[ "$old_host" == codex || "$old_host" == both ]] && paths+=(.codex .codex/agents .codex/hooks.json)
fi
for path in "${paths[@]}"; do
  if [[ -L "$project_root/$path" ]]; then
    echo "Refusing to modify symlinked project configuration: $project_root/$path" >&2
    exit 2
  fi
done
model_mode="${model_mode:-inherit}"
if [[ ! -f "$project_root/.agents/.ship-version" ]]; then
  for role in implementer reviewer plan-checker slicer; do
    for target in "$project_root/.claude/agents/$role.md" "$project_root/.codex/agents/$role.toml"; do
      if [[ "$host" == codex && "$target" == "$project_root/.claude/"* ]] ||
         [[ "$host" == claude && "$target" == "$project_root/.codex/"* ]]; then continue; fi
      if [[ -e "$target" || -L "$target" ]]; then
        echo "Conflicting existing agent file: $target" >&2
        exit 2
      fi
    done
    target="$project_root/.agents/roles/$role.md"
    if [[ -e "$target" || -L "$target" ]]; then
      echo "Conflicting existing role file: $target" >&2
      exit 2
    fi
  done
  if [[ ! -e "$project_root/.agents/workflows/ship-fix" ]]; then
    for name in ship fix ship-analyze; do
      if [[ -e "$project_root/.agents/skills/$name" || -L "$project_root/.agents/skills/$name" ]]; then
        echo "Conflicting existing skill: $project_root/.agents/skills/$name" >&2
        exit 2
      fi
    done
  fi
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
package_root="$(cd "$here/.." && pwd)"
src=""
tmpdir=""
txn=""
txn_ready=0
installed=0
managed=(.agents/workflows/ship-fix .agents/skills/ship .agents/skills/fix .agents/skills/ship-analyze
  .agents/roles .agents/platforms/claude/roles.json .agents/platforms/codex/roles.json .agents/agent-models.yaml
  .agents/.ship-version)
if [[ "$host" == claude || "$host" == both || "$switching" -eq 1 ]]; then
  managed+=(.claude/settings.json .claude/agents .claude/scripts .claude/hooks .claude/skills/ship
    .claude/skills/fix .claude/skills/ship-analyze .claude/references/ship-protocol.md)
fi
if [[ "$host" == codex || "$host" == both || "$switching" -eq 1 ]]; then
  managed+=(.codex/hooks.json .codex/agents .codex/scripts .codex/hooks)
fi
cleanup() {
  rc=$?
  if [[ -n "$txn" && "$txn_ready" -eq 1 && "$installed" -eq 0 ]]; then
    for item in "${managed[@]}"; do
      rm -rf "$project_root/$item"
      if [[ -e "$txn/files/$item" || -L "$txn/files/$item" ]]; then
        mkdir -p "$(dirname "$project_root/$item")"
        cp -pR "$txn/files/$item" "$project_root/$item"
      fi
    done
    echo 'Installation failed; previous managed files restored.' >&2
  fi
  if [[ -n "$txn" ]]; then rm -rf "$txn"; fi
  if [[ -n "$tmpdir" ]]; then rm -rf "$tmpdir"; fi
  return "$rc"
}
trap cleanup EXIT

if [[ -n "${SHIP_SRC:-}" ]]; then
  src="$SHIP_SRC"
elif [[ -n "${SHIP_REMOTE:-}" ]]; then
  if [[ "$dev" -eq 1 ]]; then echo "--dev requires a local checkout" >&2; exit 2; fi
  tmpdir="$(mktemp -d)"
  git -C "$tmpdir" init -q
  git -C "$tmpdir" remote add origin "$SHIP_REMOTE"
  if ! git -C "$tmpdir" fetch --depth 1 origin "$ref" >/dev/null 2>&1; then
    echo "Cannot fetch ref: $ref" >&2
    exit 2
  fi
  git -C "$tmpdir" checkout -q --detach FETCH_HEAD
  src="$tmpdir"
else
  src="$package_root"
fi

commit="$(git -C "$src" rev-parse --verify HEAD 2>/dev/null || echo unknown)"
if [[ -z "${SHIP_REMOTE:-}" || -n "${SHIP_SRC:-}" ]]; then
  ref_commit="$(git -C "$src" rev-parse --verify "${ref}^{commit}" 2>/dev/null || true)"
  if [[ -z "$ref_commit" ]]; then
    echo "Cannot resolve ref: $ref" >&2
    exit 2
  fi
  if [[ "$dev" -eq 1 && "$ref_commit" != "$commit" ]]; then
    echo "SHIP_SRC is at $commit, but $ref resolves to $ref_commit." >&2
    echo "Check out $ref in the source checkout, or unset SHIP_SRC to clone SHIP_REMOTE." >&2
    exit 2
  fi
  commit="$ref_commit"
  if [[ "$dev" -eq 0 ]]; then
    tmpdir="$(mktemp -d)"
    git -C "$src" archive "$ref_commit" | tar -x -C "$tmpdir"
    src="$tmpdir"
  else
    commit="worktree:$commit"
    echo "Installing development worktree, including uncommitted changes." >&2
  fi
fi

if [[ "$model_mode" == ocx && ! -e "$project_root/.agents/agent-models.yaml" ]] &&
   ! grep -q '^ocx_models:' "$src/install/templates/agent-models.yaml" &&
   ! grep -q -- '--profile' "$src/workflows/ship-fix/scripts/agent_models.py"; then
  # Older snapshots own their original OCX defaults and do not support profile initialization.
  legacy_env=(env)
  if [[ -n "${SHIP_SRC:-}" ]]; then
    legacy_env+=("SHIP_SRC=$SHIP_SRC")
  elif [[ -z "${SHIP_REMOTE:-}" ]]; then
    legacy_env+=("SHIP_SRC=$package_root")
  fi
  legacy_args=("$ref" --host "$host" --models ocx)
  [[ "$dev" -eq 1 ]] && legacy_args+=(--dev)
  "${legacy_env[@]}" "$src/install/install.sh" "${legacy_args[@]}"
  exit 0
fi

if [[ -f "$src/workflows/ship-fix/view/ship-view" ]]; then
  if [[ -e "$project_root/ship-view" || -L "$project_root/ship-view" ]]; then
    python3 - "$project_root" <<'PY'
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
launcher = root / 'ship-view'
marker = root / '.agents/.ship-version'
try:
    expected = json.loads(marker.read_text()).get('managed_files', {}).get('ship-view')
    actual = hashlib.sha256(launcher.read_bytes()).hexdigest() if launcher.is_file() and not launcher.is_symlink() else None
except (OSError, ValueError):
    expected = actual = None
if not expected or expected != actual:
    sys.exit('Conflicting existing ship-view; refusing to overwrite it.')
PY
  fi
  managed+=(ship-view)
fi

txn="$(mktemp -d)"
for item in "${managed[@]}"; do
  if [[ -e "$project_root/$item" || -L "$project_root/$item" ]]; then
    mkdir -p "$(dirname "$txn/files/$item")"
    cp -pR "$project_root/$item" "$txn/files/$item"
  fi
done
txn_ready=1
if [[ "$switching" -eq 1 ]]; then
  python3 -I -B "$package_root/install/uninstall.py" "$project_root"
fi

if [[ -e "$project_root/.agents/workflows/ship-fix" && ! -e "$project_root/.agents/.ship-version" ]]; then
  backup="$project_root/.agents/.ship-backup/$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$backup"
  mv "$project_root/.agents/workflows/ship-fix" "$backup/ship-fix"
  for shared in skills roles platforms; do
    [[ -e "$project_root/.agents/$shared" ]] && cp -R "$project_root/.agents/$shared" "$backup/$shared" || true
  done
  echo "Backed up previous installation to $backup"
fi

copy() { mkdir -p "$(dirname "$2")"; rm -rf "$2"; cp -R "$1" "$2"; }

copy "$src/workflows/ship-fix" "$project_root/.agents/workflows/ship-fix"
if [[ -f "$src/workflows/ship-fix/view/ship-view" ]]; then
  cp "$src/workflows/ship-fix/view/ship-view" "$project_root/ship-view"
  chmod +x "$project_root/ship-view"
fi
mkdir -p "$project_root/.agents/skills"
for skill in ship fix ship-analyze; do
  rm -rf "$project_root/.agents/skills/$skill"
  cp -R "$src/skills/$skill" "$project_root/.agents/skills/$skill"
done
mkdir -p "$project_root/.agents/roles" "$project_root/.agents/platforms/claude" "$project_root/.agents/platforms/codex"
cp "$src"/roles/*.md "$project_root/.agents/roles/"
cp "$src/platforms/claude/roles.json" "$project_root/.agents/platforms/claude/roles.json"
cp "$src/platforms/codex/roles.json" "$project_root/.agents/platforms/codex/roles.json"
find "$project_root/.agents/workflows/ship-fix" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

install_template() {
  if [[ ! -e "$2" ]]; then
    mkdir -p "$(dirname "$2")"
    cp "$1" "$2"
    echo "Created $2"
  fi
}
install_template "$src/install/templates/agent-models.yaml" "$project_root/.agents/agent-models.yaml"
if [[ "$host" == claude || "$host" == both ]]; then
  python3 "$package_root/install/merge_settings.py" "$src/install/templates/claude-settings.json" "$project_root/.claude/settings.json" claude
fi
if [[ "$host" == codex || "$host" == both ]]; then
  python3 "$package_root/install/merge_settings.py" "$src/install/templates/codex-hooks.json" "$project_root/.codex/hooks.json" codex
fi

link() {
  if [[ ! -e "$2" && ! -L "$2" ]]; then
    mkdir -p "$(dirname "$2")"
    ln -s "$1" "$2"
    echo "Linked $2"
  elif [[ -L "$2" && "$(readlink "$2")" == "$1" ]]; then
    :
  elif [[ "$2" == "$project_root/.claude/references/ship-protocol.md" && -L "$2" && "$(readlink "$2")" == '../.agents/workflows/ship-fix/PROTOCOL.md' ]]; then
    rm "$2"
    ln -s "$1" "$2"
  else
    echo "Conflicting existing path: $2" >&2
    exit 2
  fi
}
for host_dir in .claude .codex; do
  [[ "$host" == both || "$host_dir" == ".$host" ]] || continue
  link "../.agents/workflows/ship-fix/scripts" "$project_root/$host_dir/scripts"
  link "../.agents/workflows/ship-fix/hooks" "$project_root/$host_dir/hooks"
done
if [[ "$host" == claude || "$host" == both ]]; then
mkdir -p "$project_root/.claude/skills" "$project_root/.claude/references"
for skill in ship fix ship-analyze; do
  link "../../.agents/skills/$skill" "$project_root/.claude/skills/$skill"
done
link "../../.agents/workflows/ship-fix/PROTOCOL.md" "$project_root/.claude/references/ship-protocol.md"
fi

cd "$project_root"
if [[ "$model_mode" == ocx && ! -f "$txn/files/.agents/agent-models.yaml" ]]; then
  python3 -I -B .agents/workflows/ship-fix/scripts/agent_models.py init --profile ocx --host "$host"
fi
host_args=()
if grep -q -- '--host' .agents/workflows/ship-fix/scripts/agent_models.py; then
  host_args=(--host "$host")
elif [[ "$host" != both ]]; then
  echo 'This snapshot supports only installation for both hosts.' >&2
  exit 2
fi
python3 -I -B .agents/workflows/ship-fix/scripts/agent_models.py sync "${host_args[@]}"
set +e
python3 -I -B .agents/workflows/ship-fix/scripts/agent_models.py check "${host_args[@]}"
check_rc=$?
set -e
if [[ $check_rc -ne 0 ]]; then
  echo "Agent model catalog needs a refresh:" >&2
  echo "  python3 -I -B .agents/workflows/ship-fix/scripts/ocx_models.py" >&2
  echo "  make -C <package> sync PROJECT=$project_root" >&2
  echo "  then restart the configured host." >&2
  exit "$check_rc"
fi
python3 - "$project_root/.agents/.ship-version" "$ref" "$commit" "$host" "$project_root" "$txn/files" "$switching" "$src" <<'PY'
import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path
root, backup = Path(sys.argv[5]), Path(sys.argv[6])
old_marker = backup / '.agents/.ship-version'
old_version = json.loads(old_marker.read_text()) if old_marker.is_file() else {}
old_owned = old_version.get('managed_settings', {})
owned = {} if sys.argv[7] == '1' else dict(old_owned)
names = (['.claude/settings.json'] if sys.argv[4] == 'claude' else
         ['.codex/hooks.json'] if sys.argv[4] == 'codex' else ['.claude/settings.json', '.codex/hooks.json'])
for name in names:
    after_path, before_path = root / name, backup / name
    if not after_path.is_file():
        continue
    after = json.loads(after_path.read_text())
    before = json.loads(before_path.read_text()) if before_path.is_file() else {}
    prev = old_owned.get(name, {})
    hooks = {}
    for event, entries in after.get('hooks', {}).items():
        old_entries = before.get('hooks', {}).get(event, [])
        previous_owned = prev.get('hooks', {}).get(event, [])
        added = [entry for entry in entries if entry not in old_entries or entry in previous_owned]
        if added:
            hooks[event] = added
    rules = after.get('permissions', {}).get('allow', [])
    old_rules = before.get('permissions', {}).get('allow', [])
    previous_rules = prev.get('allow', [])
    owned[name] = {'hooks': hooks, 'allow': [rule for rule in rules if rule not in old_rules or rule in previous_rules]}
files = dict(old_version.get('managed_files', {}))
relatives = [f'.agents/roles/{role.name}' for role in (Path(sys.argv[8]) / 'roles').glob('*.md')]
relatives += ['.agents/platforms/claude/roles.json', '.agents/platforms/codex/roles.json']
if (Path(sys.argv[8]) / 'workflows/ship-fix/view/ship-view').is_file():
    relatives.append('ship-view')
for relative in relatives:
    path = root / relative
    if path.is_file() and not path.is_symlink():
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
Path(sys.argv[1]).write_text(json.dumps({"schema_version": 1, "ref": sys.argv[2], "commit": sys.argv[3],
    "host": sys.argv[4], "managed_settings": owned, "managed_files": files,
    "installed_at": datetime.now(timezone.utc).isoformat()}) + "\n")
PY
installed=1
echo "Installed ship-workflow $ref ($commit) into $project_root"
