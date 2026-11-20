# Changelog

## v1.3.2 - 2026-11-21

### Bug Fixes

- Capture nested directories with read exclusions enabled when they contain Git metadata.

## v1.3.1 - 2026-11-20

### Bug Fixes

- Allow authorized integration checkpoints to bind dependent slices to newly accepted predecessor states.
- Capture, compare and track confirmed alternate path spellings as one filesystem entry while preserving historical manifests and verified first-touch baselines.

## v1.3.0 - 2026-11-18

### New Features

- Allow users to accept open review findings after exhausting the repair budget.

### Improvements

- Show full open review findings and severity counts in repository review-budget checks.

### Documentation

- Explain how projects configure review criteria and how review passes are recorded.

## v1.2.0 - 2026-11-18

### New Features

- Explore ship and fix runs in a live local browser with `./ship-view`.
- Create run metadata safely and record contract-validation setup failures as telemetry.

### Improvements

- Configure the OCX example assignments in the model template and validate the profile when applying it.
- Show run progress, remaining steps, phase durations and available token usage in Run Explorer, including a token summary on the overview.
- Use `gpt-6.2-sol` for the OCX reviewer, plan-checker, and slicer example assignments and catalog.
- Identify related ship and fix runs and support user-approved recovery of independent review-budget histories.
- Resolve an exact run name before matching run ID prefixes in `ship-analyze`.

### Bug Fixes

- Keep run details updating when an individual artifact contains incomplete JSON and identify unavailable data accurately.
- Validate ship contracts with read exclusions and declared deliverables without losing the contract path.

## v1.1.0 - 2026-11-17

### New Features

- Carry user-approved review-budget decisions into a successor repo or slice while preserving historical states.

### Bug Fixes

- Avoid traversing excluded paths during untracked source capture and initialized scope checks.
- Apply read exclusions to opt-in check captures, execution specifications and declared input snapshots.

## v1.0.0

### New Features

- Snapshot declared deliverables and verify the selected published versions before review and completion.
- Exclude declared paths from source capture and update matrices from a prior version.
- Bind declared check inputs and runner execution records to their evidence.
- Allow approved requirement amendments to update the effective data scope.
- Provide the `ship`, `fix` and `ship-analyze` skills, lifecycle protocol, helpers and trusted-command hooks.
- Support scope and requirement amendments, successor-run budgets, author recovery, slicing and parallel batches.
- Install for Claude, Codex or both, with inherited host models by default and optional OCX assignments.
- Support host-specific model overrides and target individual hosts when syncing or checking agents.
- Install pinned local snapshots or explicitly use the working tree with `--dev`.

### Improvements

- Allow run analysis against an explicitly selected workspace and reject ambiguous run prefixes.
- Use portable agent role names and project-agnostic defaults, examples and protocol text.
- Preserve existing project settings when installing or removing package-owned hooks and links.
- Remove the worktree hooks' dependency on `jq`.
- Allow gate-fix phase telemetry to specify an attempt number.
- Provide installation, architecture and development documentation and a `CLAUDE.md` entry point.

### Bug Fixes

- Validate evidence supplied by unverified matrix rows before review.
- Preserve unchanged pre-existing work when checking an implementer's write scope.
- Restore managed files after a failed installation or upgrade and propagate agent generation and check failures.
- Register the Claude trusted-command hook and resolve the installed protocol reference correctly.
- Reject unresolved local refs and accept unambiguous legacy role prefixes in existing model configurations.
- Validate repair phase bindings, declared slice schedules and recovery author identity.
- Apply initialized exclusions when checking captured artifacts.
- Correct glob matching, bounded check execution, gate log sanitization and capsule exclusion fingerprints.
- Fix non-git scope checks and resolve project roots independently of installation depth.
