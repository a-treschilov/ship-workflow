#!/usr/bin/env bash
set -euo pipefail

readonly project_root="$(python3 -c '
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
event_name=$(python3 -c 'import json,sys; v=json.load(sys.stdin)["hook_event_name"]; assert isinstance(v,str) and v; print(v)' <<<"$input")
worktree_path=$(python3 -c 'import json,sys; from pathlib import Path; v=json.load(sys.stdin)["worktree_path"]; assert isinstance(v,str) and v; print(Path(v).resolve())' <<<"$input")

if [[ "$event_name" != "WorktreeRemove" ]]; then
  printf 'Unexpected hook event: %s\n' "$event_name" >&2
  exit 1
fi

if [[ "$worktree_path" != "$project_root" ]]; then
  printf 'Refusing to remove unrecognized worktree path: %s\n' "$worktree_path" >&2
  exit 1
fi

# WorktreeCreate returned the original project root; there is nothing to remove.
exit 0
