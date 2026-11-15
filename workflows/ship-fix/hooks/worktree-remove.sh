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
worktree_path=$(jq -er '.worktree_path' <<<"$input")

if [[ "$event_name" != "WorktreeRemove" ]]; then
  printf 'Unexpected hook event: %s\n' "$event_name" >&2
  exit 1
fi

if [[ "$worktree_path" != "$umbrella_root" ]]; then
  printf 'Refusing to remove unrecognized worktree path: %s\n' "$worktree_path" >&2
  exit 1
fi

# WorktreeCreate returned the original umbrella root; there is nothing to remove.
exit 0
