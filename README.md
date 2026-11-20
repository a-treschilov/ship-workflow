# ship-workflow

Related ship and fix runs preserve review-fix budgets through stable work-item identifiers and explicit,
audited continuity. The protocol describes recovery when independent run histories omitted that continuity.

Contracts can declare read exclusions and versioned deliverables. The lifecycle helpers validate submitted
evidence and compare declared published files with run-local snapshots before review and completion; see
the [protocol](workflows/ship-fix/PROTOCOL.md) for commands and limitations.

A delivery workflow for AI coding agents that makes the agent prove its work before it calls a task done.

An agent can produce a plausible diff in minutes. The harder question is whether that diff implements the
approved requirement, survives an independent review, passes the project's real gates, and still matches the
code at the moment someone accepts it. `ship-workflow` encodes that loop as a protocol with immutable run
artifacts, explicit budgets, and role separation.

It ships three skills:

- **`ship`** drives an approved plan through preflight, implementation, independent review, gates, and
  version-bound acceptance.
- **`fix`** handles a bug or small improvement from a brief without requiring an approved plan.
- **`ship-analyze`** reads a finished run and reports goal verification, review churn, phase time, and token
  accounting with explicit limits when data is missing.

The skills are host-agnostic. Adapters are included for Claude Code and Codex, with a shared lifecycle,
role text, helper scripts, and trusted-command hooks.

## Why this exists

A green test suite is not proof that the requested behavior exists. A clean review is not proof that the
goal was met. `ship-workflow` keeps those claims separate:

- The contract freezes the requirement, scope, non-goals, checks, and acceptance criteria before edits.
- Implementation, review, and final gates run with separate context and separate permissions.
- Every transition binds the current source, evidence, logs, and prior state by hash.
- Repair budgets stop an endless review loop; extensions require an explicit user decision.
- Already accepted slice integration repairs can resume the same repo lifecycle using the current gate and existing budget.
- Completion is a certificate over verified evidence, not a status label written by the implementer.

The result is a reviewed, verified, uncommitted working tree for a human to inspect and release.

## Requirements

- Python 3.11 or newer, standard library only.
- `git` and a POSIX shell (`bash`).
- Claude Code or Codex for interactive use. The protocol and helpers can be driven by another host that can
  run shell commands, dispatch agents, and ask the user for decisions.
- A project directory with an `.agents/` directory. Create it with `mkdir -p .agents` if the project does not
  have one yet.

## Install a pinned version

The installer copies a snapshot of this repository into the target project. It does not add this repository as
a submodule, workspace, or runtime dependency.

Clone once:

```bash
git clone https://github.com/<owner>/ship-workflow.git ~/ship-workflow
```

Install a tag into a project:

```bash
cd /path/to/project
mkdir -p .agents
SHIP_REMOTE=https://github.com/<owner>/ship-workflow.git \
  ~/ship-workflow/install/install.sh v1.1.0 --host both
```

The remote form fetches the requested ref into a temporary directory. To install from a local checkout:

```bash
cd /path/to/project
SHIP_SRC=~/ship-workflow ~/ship-workflow/install/install.sh v1.1.0 --host both
```

`SHIP_SRC` takes precedence over `SHIP_REMOTE`. Local installs copy the committed bytes at the requested ref,
regardless of the current checkout. For development, pass `--dev` with the checked-out ref to include worktree
changes; the version record identifies the result as a worktree snapshot.
Choose `--host claude`, `--host codex`, or `--host both`; a single installed host is detected automatically.
Use `--models ocx` on a new install to select OCX example assignments; otherwise roles inherit the host model.
The OCX assignments come from the template's `ocx_models` profile. Existing model configurations are preserved on upgrade.

Each install writes `.agents/.ship-version`:

```json
{"schema_version":1,"ref":"v1.1.0","commit":"<resolved commit>","installed_at":"<UTC>"}
```

An existing installation without `.ship-version` is backed up to `.agents/.ship-backup/<timestamp>/` before
the shared files are replaced.

## Use the workflow

Open the project in Claude Code or Codex and invoke the skill:

```text
/ship plans/my-feature.md
/fix Fix the duplicate charge when a retry reuses an idempotency key
/ship-analyze --run latest --format compact
```

`ship` expects an already approved plan or a named item in one. It freezes the selected scope, then runs the
lifecycle. `fix` extracts a small acceptance brief from the bug description and uses the same review and gate
contract. Both leave changes uncommitted.

Run artifacts live under `plans/.ship-runs/` and `plans/.fix-runs/` by default. Treat them as append-only: the
helpers seal contracts, state records, evidence, reviews, and completion certificates by hash. Do not hand-edit
or delete an active run.

New runs are created with the workflow's `init-run` helper; `validate-run` records setup success or failure
before preflight. An interrupted setup can remain open, and a failed setup does not count as a passed check.

### Configure review criteria

Each target repository owns its review rubric at `.agents/skills/review/SKILL.md`. The installer does not
create this file. Create or edit it in each repository to describe the checks your project needs. The reviewer
reads the rubric and applicable repository and module instructions in every review round. The workflow protocol
defines the review scope, baseline and result format.

For example, add a `simplification` section that explains when the check applies and what problems to look for,
such as unnecessarily nested conditions or duplicated behavior. Give each rubric topic a stable ID. Before a
new `/ship` or `/fix` run, ask the orchestrator to include the checks in the contract. It reads the rubric and
records the complete set of IDs in `required_passes` for each repository. An excerpt might look like this:

```json
"required_passes": {"app": ["logic", "security", "simplification"]}
```

Keep all existing rubric IDs when adding a new one. Inspect the run's `contract.json` to see the selected IDs.
Each review records an `inspected` result or a justified `not-applicable` result for every ID under
`coverage.mandatory_passes`. Run contracts and review records are immutable; rubric changes apply when a new
contract is prepared.

Rubrics can include Boy Scout suggestions for nearby code. The approved scope and protocol govern the review;
unrelated pre-existing improvements do not expand the writable scope. See the
[protocol](workflows/ship-fix/PROTOCOL.md) for the complete rules.

### Explore runs visually

Run `./ship-view` from an installed project's root. It opens a local browser with repository progress, remaining
steps, phase durations, available model and token usage, and searchable run artifacts. JSON opens on request.
The token summary counts known input, output and cache tokens from recorded agent phases and marks partial usage.
Use `--no-open` to print the URL without opening a browser, or `--port`
to select a port. Stop the server with Ctrl+C. If the default port is busy, a free port is selected.

The viewer is read-only and listens on loopback. An open phase only means its end has not been recorded; it
does not prove the agent is active. Legacy or incomplete runs show the data available, with limitations. Metrics
and certification use `ship-analyze`; unavailable usage is not zero and recorded completion is not a fresh
certification of the current source.

## Configure models

The installer creates `.agents/agent-models.yaml` once and never overwrites it on upgrade. It maps four roles to
model IDs:

```yaml
models:
  implementer: "your-implementer-model"
  reviewer: "your-reviewer-model"
  plan-checker: "your-checker-model"
  slicer: "your-slicer-model"
```

The template uses `inherit` for every role, using the host's model configuration. For a new configuration,
`init --profile ocx` copies the template's `ocx_models` profile into `models` after validating the catalog and
aliases required by the selected host; it refuses to replace existing pinned assignments. To pin other models, use IDs and aliases that your
provider exposes. `available_models` is the catalog the checker accepts; `claude_aliases` maps canonical IDs
to Claude Code aliases. Then regenerate the native role files:

```bash
cd /path/to/project
python3 -I -B .agents/workflows/ship-fix/scripts/agent_models.py sync
python3 -I -B .agents/workflows/ship-fix/scripts/agent_models.py check
```

`sync` writes `.claude/agents/<role>.md` and `.codex/agents/<role>.toml` from the shared role text. Re-run it
after changing a role, a platform description, or a model assignment, then reload the host.

`ocx_models.py` is optional. It refreshes the catalog from an OCX gateway without changing role assignments.

The trusted-command hook in `workflows/ship-fix/hooks/trusted_policy.py` ships with conservative defaults for
common Node.js and Go repository layouts. Extend it for a different layout; commands it does not recognize fall
through to the host's normal permission prompt.

## Upgrade and remove

```bash
# install or upgrade to another pinned tag
make -C ~/ship-workflow install REF=v1.2.0 PROJECT=/path/to/project

# regenerate native agent files after a role or model edit
make -C ~/ship-workflow sync PROJECT=/path/to/project

# verify adapter drift without writing
make -C ~/ship-workflow check PROJECT=/path/to/project

# remove shared files, keep per-project configuration
make -C ~/ship-workflow uninstall PROJECT=/path/to/project
```

An upgrade replaces the shared workflow, skills, roles, platform metadata, and generated links. It preserves
`.agents/agent-models.yaml`, `.claude/settings.json`, and `.codex/hooks.json` if they already exist.

## What gets installed

Shared files, replaced on every install:

| Source | Installed path |
| --- | --- |
| `workflows/ship-fix/` | `.agents/workflows/ship-fix/` |
| `skills/{ship,fix,ship-analyze}/` | `.agents/skills/` |
| `roles/*.md` | `.agents/roles/` |
| `platforms/{claude,codex}/roles.json` | `.agents/platforms/` |
| `workflows/ship-fix/view/ship-view` | `ship-view` launcher at the project root |

Per-project files, created once and preserved on upgrade:

| File | Purpose |
| --- | --- |
| `.agents/agent-models.yaml` | Role-to-model assignments and catalog |
| `.claude/settings.json` | Claude Code permissions and hook registration |
| `.codex/hooks.json` | Codex hook registration |

The installer also creates `.claude/` and `.codex/` links to the shared scripts, hooks, skills, and protocol so
each host reads one source of truth.

The canonical helper path is always `.agents/workflows/ship-fix/scripts/<script>.py`. Keeping it stable is what
lets installed skills, protocol text, and hooks work without rewriting paths per project.

## How a run works

When the review-fix budget is exhausted, the user can grant more passes, accept the displayed open review
findings, or stop. Accepted findings remain visible in the completion record and final report. Independent
checks and requirement verification remain required for completion.

At a high level, each repository moves through:

```text
initialized -> ready -> implemented -> pre-review-passed -> reviewed -> gates-passed -> verified
```

Review fixes insert `needs-fix` and `edited-unverified` before the next fresh review. A blocked preflight,
exhausted budget, or unavailable check stops the run instead of being recorded as success. The checker assesses
the approved requirement against final source; the completion helper then revalidates every repo and produces
`completion.json`.

The full lifecycle, artifact schemas, budgets, telemetry, and historical compatibility rules are in
[`workflows/ship-fix/PROTOCOL.md`](workflows/ship-fix/PROTOCOL.md). Host capability mappings are in
[`workflows/ship-fix/HOST-ADAPTERS.md`](workflows/ship-fix/HOST-ADAPTERS.md).

## Documentation

Start with [`docs/README.md`](docs/README.md) for the reading order and source-of-truth map.

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — package layout, runtime components, state and evidence model.
- [`docs/INSTALLATION.md`](docs/INSTALLATION.md) — detailed install, upgrade, configuration, and troubleshooting.
- [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) — development loop, tests, roles, helpers, and release process.
- [`docs/AI-CONTEXT.md`](docs/AI-CONTEXT.md) — short context pack for coding agents working in this repository.
- [`AGENTS.md`](AGENTS.md) — repository rules. [`CLAUDE.md`](CLAUDE.md) is a symlink to the same file.

## Development

```bash
make test
```

Tests use `SHIP_TEST_TMPDIR` for scratch state. The package targets Python 3.11+ and has no third-party
runtime dependencies. See [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) before changing a role, a helper, or a
host adapter.

## Versioning

Releases are annotated semver tags (`vX.Y.Z`). Install a tag, not a moving branch, when you need a stable
snapshot. PATCH releases contain backward-compatible fixes and small corrections; MINOR releases add backward-
compatible features; MAJOR releases contain incompatible changes. User-visible changes are recorded in
[`CHANGELOG.md`](CHANGELOG.md) before tagging. See [Development](docs/DEVELOPMENT.md) for the release procedure.

## License

MIT. See [`LICENSE`](LICENSE).
