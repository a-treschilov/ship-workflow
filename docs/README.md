# Documentation map

This directory holds the package documentation that a human or an AI agent needs before changing the workflow.
The repository is a distribution package: the lifecycle itself lives in `workflows/ship-fix/`, while the skills
and roles are installed into a target project's `.agents/` tree.

## Reading order

Use the shortest path for the task:

| Task | Read first | Then |
| --- | --- | --- |
| Install or upgrade the package | [INSTALLATION.md](INSTALLATION.md) | [`README.md`](../README.md) |
| Use `ship` or `fix` in a project | [`workflows/ship-fix/PROTOCOL.md`](../workflows/ship-fix/PROTOCOL.md) | [`workflows/ship-fix/HOST-ADAPTERS.md`](../workflows/ship-fix/HOST-ADAPTERS.md) |
| Change the package | [AI-CONTEXT.md](AI-CONTEXT.md) | [ARCHITECTURE.md](ARCHITECTURE.md), [DEVELOPMENT.md](DEVELOPMENT.md) |
| Debug a run | [ARCHITECTURE.md](ARCHITECTURE.md) | [`skills/ship-analyze/SKILL.md`](../skills/ship-analyze/SKILL.md) |

## Source-of-truth files

| Topic | Canonical file |
| --- | --- |
| Lifecycle, state transitions, evidence, budgets, telemetry | `workflows/ship-fix/PROTOCOL.md` |
| Host capability mapping | `workflows/ship-fix/HOST-ADAPTERS.md` |
| User-facing skill behavior | `skills/ship/SKILL.md`, `skills/fix/SKILL.md`, `skills/ship-analyze/SKILL.md` |
| Role behavior | `roles/implementer.md`, `roles/reviewer.md`, `roles/plan-checker.md`, `roles/slicer.md` |
| Host-native role metadata | `platforms/claude/roles.json`, `platforms/codex/roles.json` |
| Model assignments | `install/templates/agent-models.yaml` |
| Install contract | `install/install.sh`, `Makefile` |
| Repository rules | `AGENTS.md` and its `CLAUDE.md` symlink |

Do not create a second copy of lifecycle rules in a skill, role, or README. Link to the canonical file and add
only the behavior specific to that layer.

## Documentation freshness

Documentation is part of every change, not a follow-up task. A change is incomplete while a document still
describes the old behavior, path, role name, command, or installation flow.

Update documentation in the same change when you modify:

- a helper command, flag, artifact schema, state transition, budget, or evidence rule;
- a skill, role, host adapter, model assignment, or installer behavior;
- the repository layout, test command, release process, or supported Python version;
- any user-visible behavior or installation step.

Also update [`CHANGELOG.md`](../CHANGELOG.md) for user-visible changes. Search the repository for the old term
after a rename (`rg -n 'old-term'`) so stale references do not survive in tests, templates, or docs.
