# AI context

Read this file before changing the package. It is the short context pack; the linked documents are the details.

## What this repository is

`ship-workflow` is a portable delivery workflow for AI coding agents. It is not an application and not a
monorepo. A project installs a tagged snapshot into `.agents/`; this repository only develops that snapshot.

The package contains two lifecycle skills (`ship`, `fix`), one analysis skill (`ship-analyze`), four agent roles,
lifecycle helpers, host adapters for Claude Code and Codex, and an installer.

## Read order

1. This file.
2. [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) for components and invariants.
3. [`docs/DEVELOPMENT.md`](DEVELOPMENT.md) for the change map and release process.
4. [`workflows/ship-fix/PROTOCOL.md`](../workflows/ship-fix/PROTOCOL.md) before changing lifecycle behavior.
5. [`AGENTS.md`](../AGENTS.md) for repository rules.

## Repository map

| Path | What lives there |
| --- | --- |
| `workflows/ship-fix/PROTOCOL.md` | Canonical lifecycle, schemas, evidence and budget rules |
| `workflows/ship-fix/HOST-ADAPTERS.md` | Operation-to-host mapping |
| `workflows/ship-fix/scripts/` | Lifecycle helpers and tests |
| `workflows/ship-fix/hooks/` | Trusted-command and worktree adapters |
| `skills/ship/`, `skills/fix/`, `skills/ship-analyze/` | User-facing skills |
| `roles/` | Implementer, reviewer, plan-checker and slicer behavior |
| `platforms/` | Claude Code and Codex role metadata |
| `install/` | Installer and per-project templates |
| `docs/` | Documentation for humans and agents |

## Hard rules

- Keep the package project-agnostic. Do not introduce a product, company, team, repository or host prefix into
  a role name, model name, default path, protocol rule or example.
- Keep one source of truth. Lifecycle rules belong in `PROTOCOL.md`; host tool names belong in adapter files.
- Helpers are invoked as one standalone command from the project root:
  `python3 -I -B .agents/workflows/ship-fix/scripts/<script>.py ...`. Do not combine the helper with `cd`,
  assignments, command substitution, heredocs, pipelines, redirections or unrelated commands.
- Do not reintroduce a fixed install-depth lookup. Resolve the project root from `SHIP_ROOT`, the nearest
  `.agents` marker, and only then a fallback.
- Do not edit generated host role files in this repository. Generate them in an installed project with
  `agent_models.py sync`.
- Do not weaken evidence: a skipped check, missing log, clean review, or green test count is not a completed
  requirement.
- Do not rewrite immutable run history or published tags.

## Documentation is part of the change

Any change to code, configuration, a command, a path, a role, a model, an install step or user-visible behavior
must update the affected documentation in the same change. This includes `README.md`, `docs/`, `AGENTS.md`,
`PROTOCOL.md`, skill text, install templates, tests that encode the contract, and `CHANGELOG.md` for
user-visible changes.

Before reporting completion, search for stale names with `rg -n 'old-name'` and verify the docs describe the
final behavior, not the previous one. If a required correction is outside the authorized scope, report the gap
instead of claiming the docs are current.

## Commands

```bash
make test

cd workflows/ship-fix/scripts/tests
SHIP_TEST_TMPDIR=$(mktemp -d) python3 -I -B -m unittest discover -s . -p 'test_agent_models.py' -v
```

The package targets Python 3.11+ and uses only the standard library. Tests must not require network access or a
model gateway.

## Common tasks

**Add or rename a role.** Update `roles/<role>.md`, `ROLES` in `agent_models.py`, both `platforms/*/roles.json`
files, `install/templates/agent-models.yaml`, tests and docs. Run `make test`.

**Change a lifecycle rule.** Update `PROTOCOL.md` first, then the validator or helper, then every skill and role
that must apply the rule. Add a refusal-path test as well as a happy-path test.

**Change the installer.** Update `install/install.sh`, the Makefile, `README.md` and
`docs/INSTALLATION.md`. Verify tag selection, existing-install backup, config preservation, host links, and the
`agent_models.py check` result in a scratch project.

**Change a host adapter.** Keep native tool names and model syntax in the platform layer. Update
`HOST-ADAPTERS.md`, the hook or template, and the native-response test. Shared workflow text must stay
host-agnostic.

**Fix a documentation gap.** Correct confirmed drift in the same task, including pre-existing drift you noticed.
If the correction crosses an authorized scope or repository boundary, report it and obtain scope approval.

## Definition of done

- The behavior is implemented, not only described.
- Focused tests pass, and the full `make test` suite passes unless a documented environment limit prevents it.
- Every affected document and `CHANGELOG.md` entry is updated.
- No stale role names, project-specific prefixes, fixed install depths or host tool names leaked into shared
  files.
- The final report states what was verified and what remains unverified or blocked.
