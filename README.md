# ship-workflow

Shared ship/fix/ship-analyze workflow for coding agents (Codex and Claude Code). It provides the
lifecycle protocol, the lifecycle helpers, trusted-command hooks, role text, and the two skills.
Projects install a pinned snapshot of this repository into their `.agents/` tree.

## Install from git by tag

```bash
# clone once, then install a tag into any project
git clone <private-remote> ~/ship-workflow
cd /path/to/project
SHIP_SRC=~/ship-workflow ~/ship-workflow/install/install.sh v1.0.0

# or let the installer clone the tag itself
cd /path/to/project
SHIP_REMOTE=<private-remote> ~/ship-workflow/install/install.sh v1.0.0
```

If neither `SHIP_SRC` nor `SHIP_REMOTE` is set, the installer copies its own checkout. The installer
records `.agents/.ship-version` with `ref`, resolved `commit` and `installed_at`.

The canonical helper path stays `.agents/workflows/ship-fix/scripts/<script>.py`, so `SKILL.md`,
`PROTOCOL.md`, `HOST-ADAPTERS.md` and the hooks keep working unchanged.

## What gets installed

Shared, replaced on every install:

- `workflows/ship-fix/{PROTOCOL.md,HOST-ADAPTERS.md,scripts/,hooks/}` -> `.agents/workflows/ship-fix/`
- `skills/{ship,fix,ship-analyze}/` -> `.agents/skills/`
- `roles/crumb-*.md` -> `.agents/roles/`
- `platforms/{claude,codex}/roles.json` -> `.agents/platforms/`

Per-project, generated once and never overwritten by an upgrade:

- `.agents/agent-models.yaml` (model pins; refresh the catalog with `ocx_models.py`)
- `.claude/settings.json`
- `.codex/hooks.json`

## Upgrade, sync, uninstall

```bash
make -C ~/ship-workflow install REF=v1.1.0 PROJECT=/path/to/project
make -C ~/ship-workflow sync    PROJECT=/path/to/project   # regenerate .claude/agents and .codex/agents
make -C ~/ship-workflow check   PROJECT=/path/to/project   # verify adapter drift
make -C ~/ship-workflow uninstall PROJECT=/path/to/project # remove shared files, keep config
```

An installation without `.ship-version` is backed up to `.agents/.ship-backup/<timestamp>/` before it
is replaced.

## Development

```bash
make test
```

The package targets Python 3.11+ and uses only the standard library. Helpers run as
`python3 -I -B .agents/workflows/ship-fix/scripts/<script>.py` from the project root.
