#!/usr/bin/env bash
set -euo pipefail

# Install a pinned ship/fix workflow snapshot into a project's .agents/ from git.
# Run from the project root (the directory containing .agents).
#   ./install.sh v1.0.0 [--sync]
# Source selection: SHIP_SRC (local checkout) > SHIP_REMOTE clone at <ref> > this checkout.

usage() {
  cat >&2 <<'EOF'
Usage: install.sh <tag-or-ref> [--sync]
Run from the project root (the directory containing .agents).
  SHIP_REMOTE  git remote to clone <ref> from
  SHIP_SRC     local checkout to copy from (takes precedence)
EOF
}

ref="${1:-}"
sync_flag=0
[[ "${2:-}" == "--sync" ]] && sync_flag=1
if [[ -z "$ref" ]]; then usage; exit 2; fi

project_root="$(pwd)"
if [[ ! -d "$project_root/.agents" ]]; then
  echo "Not a project root (missing .agents): $project_root" >&2
  exit 2
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
package_root="$(cd "$here/.." && pwd)"
src=""
tmpdir=""
cleanup() { [[ -n "$tmpdir" ]] && rm -rf "$tmpdir"; }
trap cleanup EXIT

if [[ -n "${SHIP_SRC:-}" ]]; then
  src="$SHIP_SRC"
elif [[ -n "${SHIP_REMOTE:-}" ]]; then
  tmpdir="$(mktemp -d)"
  git clone --depth 1 --branch "$ref" --single-branch "$SHIP_REMOTE" "$tmpdir" >/dev/null 2>&1
  src="$tmpdir"
else
  src="$package_root"
fi

commit="$(git -C "$src" rev-parse --verify HEAD 2>/dev/null || echo unknown)"

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
install_template "$src/install/templates/claude-settings.json" "$project_root/.claude/settings.json"
install_template "$src/install/templates/codex-hooks.json" "$project_root/.codex/hooks.json"

link() {
  if [[ ! -e "$2" && ! -L "$2" ]]; then
    mkdir -p "$(dirname "$2")"
    ln -s "$1" "$2"
    echo "Linked $2"
  fi
}
for host in .claude .codex; do
  link "../.agents/workflows/ship-fix/scripts" "$project_root/$host/scripts"
  link "../.agents/workflows/ship-fix/hooks" "$project_root/$host/hooks"
done
mkdir -p "$project_root/.claude/skills" "$project_root/.claude/references"
for skill in ship fix ship-analyze; do
  link "../../.agents/skills/$skill" "$project_root/.claude/skills/$skill"
done
link "../.agents/workflows/ship-fix/PROTOCOL.md" "$project_root/.claude/references/ship-protocol.md"

printf '{"schema_version":1,"ref":"%s","commit":"%s","installed_at":"%s"}\n' \
  "$ref" "$commit" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$project_root/.agents/.ship-version"

echo "Installed ship-workflow $ref ($commit) into $project_root"
cd "$project_root"
python3 -I -B .agents/workflows/ship-fix/scripts/agent_models.py sync || true
set +e
python3 -I -B .agents/workflows/ship-fix/scripts/agent_models.py check
check_rc=$?
set -e
if [[ $check_rc -ne 0 ]]; then
  echo "Agent model catalog needs a refresh:" >&2
  echo "  python3 -I -B .agents/workflows/ship-fix/scripts/ocx_models.py" >&2
  echo "  make -C <package> sync PROJECT=$project_root" >&2
  echo "  then restart the configured host." >&2
fi
