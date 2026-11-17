# Development

This repository is the source for the installed workflow snapshot. Changes here affect every project that
installs the next tag, so keep behavior, tests, templates and documentation aligned in the same change.

## Local loop

```bash
cd /path/to/ship-workflow
make test
```

`make test` runs the standard-library `unittest` suite from `workflows/ship-fix/scripts/tests/` and sets
`SHIP_TEST_TMPDIR` to a temporary directory. The package targets Python 3.11+ and has no third-party runtime
dependencies. Tests must not require network access or a real model gateway.

Run one module while iterating:

```bash
cd workflows/ship-fix/scripts/tests
SHIP_TEST_TMPDIR=$(mktemp -d) python3 -I -B -m unittest discover -s . -p 'test_agent_models.py' -v
```

The suite intentionally skips some tests in package layout that only make sense in an installed project (for
example host link checks and project-specific integration checks). A skip is not a pass for behavior covered by
the changed module; verify the relevant installed-path behavior separately when changing the installer or host
adapters.

## Change map

| Change | Files to update |
| --- | --- |
| Lifecycle, transition or artifact rule | `workflows/ship-fix/PROTOCOL.md`, `ship_state.py` or `ship_workflow.py`, tests, affected skill text |
| New helper command or flag | `ship_state.py` or `ship_artifacts.py`, `PROTOCOL.md`, role or skill text that invokes it, tests |
| Skill behavior | `skills/<name>/SKILL.md`, affected role text, `PROTOCOL.md` if the rule is shared, tests |
| Role behavior | `roles/<role>.md`, `platforms/claude/roles.json`, `platforms/codex/roles.json`, tests, docs |
| Model assignment or catalog | `install/templates/agent-models.yaml`, `agent_models.py` tests, installation docs |
| Host capability or tool name | `workflows/ship-fix/HOST-ADAPTERS.md`, host adapter, install template, tests |
| Install or upgrade behavior | `install/install.sh`, `Makefile`, `README.md`, `docs/INSTALLATION.md` |
| Repository layout or contributor workflow | `AGENTS.md`, `docs/`, `README.md`, `CHANGELOG.md` |

## Add or rename a role

Roles are declared in four places that must agree:

1. Add or rename the role file in `roles/<role>.md`.
2. Update `ROLES` in `workflows/ship-fix/scripts/agent_models.py`.
3. Update `platforms/claude/roles.json` and `platforms/codex/roles.json` with the same key and `name`.
4. Add the role to `install/templates/agent-models.yaml` under `models` and to the tests and docs that enumerate
   roles.

Then run `make test`. The generated native files are produced by `agent_models.py sync` in an installed project;
do not commit generated `.claude/agents/` or `.codex/agents/` files to this package.

Role names are portable identifiers. Do not include a product, team or repository prefix in a role name.

## Add a helper command

Helpers are intentionally narrow:

- `ship_state.py` validates state and artifacts. It may capture source and hashes; it must not execute project
  checks or launch agents.
- `ship_artifacts.py` performs implementer-side file mechanics. It must not decide semantic success.
- `ship_run_checks.py` executes declared `argv` checks and records the real result. It must not convert a
  failure into a pass.

When adding a command, keep output machine-readable, use write-once output paths, reject paths outside the run
directory where the contract requires that, and add focused tests for refusal paths as well as the happy path.
Update the protocol and every skill or role that must know the command.

The canonical invocation is always one standalone command from the project root:

```text
python3 -I -B .agents/workflows/ship-fix/scripts/<script>.py ...
```

Do not introduce a fixed install-depth lookup, a variable script path, or a wrapper that hides the real command.

## Add or change a host adapter

Keep host-specific tool names, model syntax and permission registration in `HOST-ADAPTERS.md`, the platform
metadata, host hook files, and install templates. Shared lifecycle text must not name a host tool.

An adapter change needs a test for the native response shape and the path-resolution behavior. If a host cannot
provide fresh-context dispatch or a required user decision, the workflow records the phase blocked or handed off;
it does not silently continue with inherited context.

## Documentation rule

Every behavior, path, command, role, model or installation change updates the affected documentation in the same
change. This is a repository invariant, not a release task.

Before finishing:

1. Search for the old term or path (`rg -n 'old-term'`).
2. Update `README.md`, `docs/`, `AGENTS.md`, `PROTOCOL.md`, skill text and tests as applicable.
3. Record user-visible changes in `CHANGELOG.md` using the release-note rules below.
4. Re-run `make test`.

If a required documentation correction is outside the authorized scope, report the gap instead of claiming the
docs are current.

## Release notes

Choose release versions using semantic versioning: increment PATCH for backward-compatible bug fixes and small
corrections, MINOR for backward-compatible features, and MAJOR for incompatible changes. Reset the lower
components when incrementing a higher component. A release number must reflect the actual compatibility impact;
do not relabel an incompatible change or new feature as a patch solely to fit a planned version.

Write commit subjects as `type(scope): description`, with the scope optional. Use `feat` for a new capability,
`fix` for a correction, `perf` for performance, `refactor` for restructuring, `docs` for documentation, `test`
for tests, `build` for build or dependency changes, `ci` for CI, `style` for formatting and `chore` for other
maintenance. Use a short imperative description, for example `fix(installer): preserve existing hooks`.
Mark incompatible changes with `!` before the colon and explain the migration in the commit body.

Group user-visible changes under the next version in `CHANGELOG.md`, using these headings only when needed:

- `### Breaking Changes` — incompatible behavior and the action needed to migrate.
- `### New Features` — new capabilities (`feat`).
- `### Improvements` — improvements to existing behavior or performance (`perf` or user-visible `refactor`).
- `### Bug Fixes` — corrections to existing behavior (`fix`).
- `### Documentation` — significant user-facing documentation changes (`docs`).

Write concise bullets in English describing the resulting behavior, not the implementation or commit history.
Include a pull request or issue link when available; never invent one. Omit internal-only `test`, `build`, `ci`,
`chore`, `style` and `refactor` changes unless they have a meaningful user-facing effect. Use the same grouping
in release notes. Existing commits do not need to be renamed.
Before the first release, keep changes under `v1.0.0`; afterwards, collect pending changes under `Unreleased`
and move them to the new version when preparing a release. Never rewrite published release notes to conceal a
later change.

## Release

1. Update `CHANGELOG.md` with the user-visible changes under the next version; add the release date when known.
2. Run `make test`.
3. Verify no project-specific names or paths remain.
4. Commit, create an annotated semver tag, and push the tag:

   ```bash
   git tag -a v1.2.0 -m 'ship-workflow v1.2.0'
   git push origin v1.2.0
   ```

5. Install the tag into a scratch project and run `agent_models.py check` there.

Never move or rewrite a published tag. A release is a new tag, not a replacement snapshot.
