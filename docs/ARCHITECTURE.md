# Architecture

`ship-workflow` is a snapshot-distributed workflow package. A project installs a tagged copy into its
`.agents/` directory, and the installed skills, protocol, roles, helpers, and hooks read only from that tree.
The package repository is not a project and does not keep run state.

## Package layout

```text
workflows/ship-fix/
  PROTOCOL.md          canonical lifecycle, schemas, budgets and telemetry rules
  HOST-ADAPTERS.md     mapping from workflow operations to host capabilities
  scripts/             lifecycle validators, artifact helpers and run-check executor
  hooks/               trusted-command and worktree adapters
skills/
  ship/                approved-plan workflow
  fix/                 bug-brief workflow
  ship-analyze/        read-only run analysis
roles/                 implementer, reviewer, plan-checker, slicer behavior
platforms/             host-native role metadata for Claude Code and Codex
install/               installer and per-project configuration templates
docs/                  human and AI documentation for this repository
```

The installer preserves one canonical runtime path:

```text
.agents/workflows/ship-fix/scripts/<script>.py
```

Skills, protocol text, and hooks use that relative path from the project root. Do not make helpers discover
themselves through a fixed number of parent directories. `agent_models.py` and `trusted_policy.py` resolve the
project root from `SHIP_ROOT`, then from the nearest marker (`.agents/agent-models.yaml` or the installed
`ship_state.py`), and only then fall back to their own location.

## Runtime components

| Component | Responsibility | Must not do |
| --- | --- | --- |
| `ship_state.py` | Capture source, baselines and deltas; validate contracts, findings, evidence, slices and history; apply transitions, phase telemetry, budgets and completion | Execute checks or dispatch agents |
| `ship_artifacts.py` | Implementer-side mechanics: task files, first-touch backups, declared check runs, evidence records, matrices, applied results, capsules | Decide semantic success or run independent gates |
| `ship_run_checks.py` | Orchestrator-side execution of declared `argv` checks; saves logs, real exit codes, before/after manifests and bound evidence | Decide that a failed command passed |
| `ship_checks.py` | Opt-in single declared check execution with an explicit execution spec and digest | Become the default `/ship` executor |
| `ship_workflow.py` | Versioned lifecycle validation and transition rules | Launch agents or execute project code |
| `roundtrip_audit.py` | Read-only transcript audit for batching and tool-call patterns | Rewrite transcripts or run state |
| `agent_models.py` | Validate and initialize model profiles, generate native Claude and Codex role files, and detect drift | Guess an unavailable model or alias |
| `ocx_models.py` | Refresh the optional OCX model catalog and Claude aliases | Change role assignments |
| `trusted_policy.py` | Conservative auto-approval for a simple helper or repository-bound test/lint command | Approve shell composition, expansion, redirection or unknown commands |

The trusted-command policy is an allowlist, not a sandbox. Commands outside it fall through to the host's normal
permission prompt. The hook adapters `claude_pre_tool_use.py` and `codex_pre_tool_use.py` normalize host payloads
and return the native response shape.

The worktree hooks intentionally resolve to the existing project root. The workflow requires the author and
reviewer to share the source being reviewed; a disposable worktree would break that binding.

## Lifecycle

`PROTOCOL.md` is the source of truth. The core path is:

```text
contract -> preflight -> implementation -> pre-review -> fresh review
         -> gates -> acceptance assessment -> verified -> completion certificate
```

A review repair inserts `needs-fix` and `edited-unverified`, then requires a fresh review and rerun of affected
gates. A gate repair follows the same rule. The orchestrator owns independent pre-review checks and final gates;
the implementer owns targeted checks and its completion matrix. The checker owns the requirement assessment.

The orchestrator may split a large repository scope into approved slices. Slices have independent state
histories and budgets, but the final repository review still reads the whole scoped diff and may reopen an
accepted slice when a later edit invalidates it.

## State and evidence

A run lives in a unique directory under `plans/.ship-runs/` or `plans/.fix-runs/`. It contains:

- `run.json` and an immutable `contract.json`;
- numbered repository state records such as `app/state-3.json`;
- telemetry events, evidence records, review rounds, applied results and task files;
- optional slice histories and approved slice schedules;
- `completion.json` after every mandatory criterion is verified.

State transitions recapture source and verify earlier seals. Artifacts are write-once; a correction is a new
artifact, not an edit to history. Source files are bound by the manifest recorded in the event, while logs,
reviews and other immutable proof are bound by hash. A later authorized edit makes earlier source proof stale;
it does not make prior logs mutable.

Contracts declare the criteria, non-goals, required gates, preflight checks, pre-review checks, rubric passes,
prerequisites and cross-repo bindings. Evidence records name a producer, kind, status, command or assertion,
environment, data scope, before/after manifests and bounded artifact paths. Hash freshness is not semantic
proof; a matching manifest only shows that the evidence was captured against the current source.

Optional contract read exclusions prune matching paths from helper capture and reject intersecting input snapshots.
Successor budget continuity records bind prior states and explicit user decisions without rewriting history. Declared
deliverables use run-local snapshots, checked against the live published paths before review and completion.
Declared check inputs can be snapshotted at execution; these bindings do not independently establish the
semantic correctness of a claim or authenticate a process identity.

## Roles

| Role | Scope | Independence |
| --- | --- | --- |
| `implementer` | Edits one assigned repository or slice; runs targeted checks; writes matrix and capsule | Fresh context for each repair; never certifies independent review |
| `reviewer` | Reads the scoped diff and consumers; writes immutable findings and dispositions | Fresh context; no source edits |
| `plan-checker` | Judges the approved requirement against final source and version-bound evidence | Read-only; no general code re-review |
| `slicer` | Proposes dependency-ordered slices and write scopes | Read-only; a human approves the schedule |

Native role files are generated from `roles/`, `platforms/` and `.agents/agent-models.yaml`. The shared role text
is the behavior source; host files only carry native syntax, tools, permissions and model selection.
The template's `ocx_models` section defines the optional OCX profile; initialization copies it to `models` only
when all current assignments inherit. Existing project configuration remains authoritative on upgrade.

## Invariants

- Workflow helpers run as one standalone command from the project root. Do not combine them with `cd`,
  assignments, command substitution, loops, pipelines, redirections or unrelated commands.
- The orchestrator never fabricates an agent result, evidence, exit code, session ID or phase end.
- A blocked, interrupted, capped or unverified state is reported as such. It is not converted into success by
  starting a replacement run.
- Review and gate budgets never reset through a new directory. Successor runs carry historical usage explicitly.
- Requirements and non-goals cannot be changed during execution. Scope or requirement amendments require the
  documented user approval artifacts.
- The final completion certificate must bind every assigned repository, mandatory criterion and dependency
  version after the last change.
