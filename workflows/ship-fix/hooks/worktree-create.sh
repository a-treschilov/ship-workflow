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
cwd=$(python3 -c 'import json,sys; from pathlib import Path; v=json.load(sys.stdin)["cwd"]; assert isinstance(v,str) and v; print(Path(v).resolve())' <<<"$input")
python3 -c 'import json,sys; v=json.load(sys.stdin)["name"]; assert isinstance(v,str) and v' <<<"$input"

if [[ "$event_name" != "WorktreeCreate" ]]; then
  printf 'Unexpected hook event: %s\n' "$event_name" >&2
  exit 1
fi

case "$cwd/" in
  "$project_root"/*) ;;
  *)
    printf 'Refusing worktree isolation outside the project root: %s\n' "$cwd" >&2
    exit 1
    ;;
esac

# The project root is not necessarily a Git repository. /ship agents intentionally share sibling repos,
# so isolation resolves to the existing project root rather than creating a disposable copy.
printf '%s\n' "$project_root"
