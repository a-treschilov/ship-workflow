# ship-workflow repository guide

This repository is the single source for the `ship`, `fix` and `ship-analyze` skills and the ship-fix lifecycle
helpers. Projects install a pinned snapshot into their `.agents/` tree; this repository itself is not a project
and keeps no run state.

`CLAUDE.md` is a symlink to this file. Edit `AGENTS.md` and keep both entry points identical.

## Start here

- [`docs/AI-CONTEXT.md`](docs/AI-CONTEXT.md) — short context pack and common tasks.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — components, state, evidence and invariants.
- [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) — change map, tests, roles, helpers and releases.
- [`docs/INSTALLATION.md`](docs/INSTALLATION.md) — install, upgrade, configuration and troubleshooting.
- [`workflows/ship-fix/PROTOCOL.md`](workflows/ship-fix/PROTOCOL.md) — canonical lifecycle and artifact rules.

## Repository map

- `workflows/ship-fix/` — lifecycle protocol, host adapters, helper scripts, hooks and their tests.
- `workflows/ship-fix/view/` — read-only Run Explorer assets and the installed `./ship-view` launcher.
- `skills/` — `ship`, `fix` and `ship-analyze` skill instructions.
- `roles/` — shared implementer, reviewer, plan-checker and slicer behavior.
- `platforms/` — Claude Code and Codex role metadata.
- `install/` — installer and per-project configuration templates.
- `docs/` — documentation for humans and coding agents.

## Non-negotiable invariants

- Keep the package project-agnostic. Role names, defaults, examples, templates and protocol text must not carry a
  product, company, team, repository or host prefix.
- Keep one source of truth. Lifecycle rules live in `PROTOCOL.md`; host-specific tool names live in
  `HOST-ADAPTERS.md` and the platform files; role behavior lives in `roles/`.
- Installed lifecycle helpers run as one standalone command from the project root:
  `python3 -I -B .agents/workflows/ship-fix/scripts/<script>.py ...`. Do not combine a helper with `cd`,
  assignments, command substitution, loops, heredocs, pipelines, redirections or unrelated commands.
- `agent_models.py` and `trusted_policy.py` resolve the project root from `SHIP_ROOT` or the nearest `.agents`
  marker. Never reintroduce a fixed install-depth lookup.
- Do not commit generated `.claude/agents/` or `.codex/agents/` files. Generate them in an installed project
  with `agent_models.py sync`.
- Never weaken evidence or lifecycle rules to make a run look complete. A clean review, a skipped check or a
  green test count is not a verified requirement.
- Never rewrite immutable run history, published tags or committed release notes to hide a later change.

## Documentation is part of the change

Documentation must always describe the current code. A change is incomplete while any document still describes
the previous behavior, path, role name, command, model, or install flow.

Update documentation in the same change whenever you modify:

- a helper command, flag, artifact schema, transition, budget or evidence rule;
- a skill, role, host adapter, model assignment or installer behavior;
- a path, directory layout, Python requirement or test command;
- any user-visible behavior or release step.

At minimum, inspect and update as applicable: `README.md`, `AGENTS.md`, `CLAUDE.md` (through the symlink),
`docs/`, `workflows/ship-fix/PROTOCOL.md`, `workflows/ship-fix/HOST-ADAPTERS.md`, affected `skills/` and
`roles/` text, install templates, tests that encode the contract, and `CHANGELOG.md` for user-visible changes.

Before reporting completion:

1. Search for renamed or removed terms (`rg -n 'old-term'`).
2. Verify every affected document matches the final behavior.
3. Record user-visible changes in `CHANGELOG.md` using the release-note rules in `docs/DEVELOPMENT.md`.
4. Run `make test`.

If a required documentation correction is outside the authorized scope or crosses a repository boundary, report
the gap and obtain scope approval; do not silently leave it stale and do not claim full alignment.

## Commands

```bash
make test

cd workflows/ship-fix/scripts/tests
SHIP_TEST_TMPDIR=$(mktemp -d) python3 -I -B -m unittest discover -s . -p 'test_agent_models.py' -v
```

The package targets Python 3.11+ and uses only the standard library. Tests must not require network access or a
model gateway.

## Release

Tag releases with annotated semver tags (`vX.Y.Z`) after updating `CHANGELOG.md` and passing `make test`. Never
move or rewrite a published tag. Install the new tag into a scratch project and run `agent_models.py check`
there before announcing the release.

Use the source checkout's `release` skill to fast-forward GitHub `main` and push the tag atomically before
publishing the GitHub Release. The release title is `vX.Y.Z`. See [Development](docs/DEVELOPMENT.md#release).
