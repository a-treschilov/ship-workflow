#!/usr/bin/env bash
set -euo pipefail

readonly umbrella_root="$(python3 -c '
from pathlib import Path
import sys
p = Path.cwd().resolve()
for x in (p, *p.parents):
    if (x / ".agents/agent-models.yaml").is_file() or (x / ".agents/workflows/ship-fix/scripts/ship_state.py").is_file():
        print(x)
        sys.exit(0)
print(Path(sys.argv[1]).resolve().parents[4])
' "${BASH_SOURCE[0]}")"

input=$(</dev/stdin)
event_name=$(jq -er '.hook_event_name' <<<"$input")
cwd=$(jq -er '.cwd' <<<"$input")
jq -er '.name | type == "string" and length > 0' <<<"$input" >/dev/null

if [[ "$event_name" != "WorktreeCreate" ]]; then
  printf 'Unexpected hook event: %s\n' "$event_name" >&2
  exit 1
fi

case "$cwd/" in
  "$umbrella_root"/*) ;;
  *)
    printf 'Refusing worktree isolation outside the Crumb umbrella: %s\n' "$cwd" >&2
    exit 1
    ;;
esac

# The umbrella is not a Git repository. /ship agents intentionally share its sibling repos,
# so isolation resolves to the existing umbrella root rather than creating a disposable copy.
printf '%s\n' "$umbrella_root"
