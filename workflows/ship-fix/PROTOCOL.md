# /ship and /fix protocol v2

This is the shared lifecycle, artifact and gate contract. Commands select requirements; agents judge
semantics; `ship_state.py` checks completeness/freshness and never executes checks or launches agents.
Read this through absolute `PROTOCOL_PATH`; resolve helper locations from absolute `HELPER_PATH` and
invoke them using the canonical relative form below. The project root
is not necessarily a git repo. All git/build commands explicitly target the assigned repo.

### Canonical helper invocation

Run every Python helper from the project root using `python3 -I -B .agents/workflows/ship-fix/scripts/<script>.py`.
This is a literal relative script path, not a user-specific checkout path. For example:

```text
python3 -I -B .agents/workflows/ship-fix/scripts/ship_state.py validate-contract --contract /absolute/run/contract.json
python3 -I -B .agents/workflows/ship-fix/scripts/ship_state.py validate-history --contract /absolute/run/contract.json --repo app
python3 -I -B .agents/workflows/ship-fix/scripts/ship_artifacts.py --task /absolute/run/tasks/task.json scope-check
```

`validate-history` is read-only. It checks immutable lifecycle and grant chains and seals without recapturing
historical source against today's checkout. Its repo output includes the gate-fix budget and `history_sha256`.

**Each workflow helper MUST be invoked in one direct shell invocation, and that shell invocation must contain only the helper invocation.**
First ensure the shell working directory is the project root, then issue the helper call in a separate
shell invocation. Never combine `cd`, variable assignments, command substitutions, loops, heredocs,
pipelines, redirections, `&&`/`;`, or unrelated inspection/commands with a helper invocation. Do not
launch helpers via `$H`, `$HELPER_PATH`, `$(dirname ...)`, aliases or shell functions. If cwd needs changing,
use a standalone `cd /absolute/path/to/project` shell invocation, then call the helper separately.
Use the host's file-writing capability for JSON/request artifacts; pass explicit absolute input/output paths already available in run context. Do not retrieve RUN_DIR/TASK_PATH via scratchpad pointer files in a compound
shell command. Script names/subcommands elsewhere in this protocol are shorthand: expand them to the
canonical invocation. Preserve argv boundaries and original check commands after `run --`; this rule
constrains only the helper-launch shell line and does not rewrite declared check argv or waive their
authorization, scope, evidence, independent gates or completion requirements. Every orchestration prompt,
agent dispatch and resume must repeat this requirement explicitly; existing agents do not reload their
definitions automatically.

## 1. Roles and precedence

- Orchestrator: contract extraction, preflight, independent pre-review/final gates, transitions,
  checkpoints, telemetry and completion. Do not outsource independent gates to the implementer.
- Original implementer: implementation, targeted proofs, completion matrix, resumed review/gate fixes.
- Fresh reviewer: scoped code correctness, rubric passes, assertions and finding adjudication.
- Checker: exact requirement extraction and goal/evidence validity; not another general code review.
- Mechanical validation is not semantic proof. A passing test count, hash or `ready` label is insufficient.

Approved requirements/non-goals and scope take precedence over general cleanup. The target repository's
`REPO_PATH/.agents/skills/review/SKILL.md` provides substantive review criteria, not its standalone
whole-HEAD diff collection or output instructions.
This protocol supplies scope, baselines, history and output. Read applicable repo/module instructions.
Unchanged consumers broken by the new delta are reviewable: anchor the changed cause and name affected
sites. Unrelated pre-existing Boy Scout issues do not expand writable scope. Extract helpers only when
semantics are genuinely shared and abstraction lowers maintenance cost, not merely because two snippets
look alike. Address substantive Suggestions during available repair passes and present remaining findings for
the user's explicit decision at exhaustion.

Implementers/checkpoints stay sequential: the safe worktree hook shares source. No real worktrees or
parallel implementers are introduced here. Claude frontmatter and Codex role files are generated from
the shared role assignments in `.agents/agent-models.yaml` by the explicit `agent_models.py sync` command; `inherit`
uses the host model configuration, while explicit IDs select pinned models. Do this outside an active run,
then restart configured agent host. Outside a run, `agent_models.py init --profile ocx` can initialize inherited
assignments from a complete OCX profile; it does not replace pinned assignments. `ocx_models.py` refreshes the canonical OCX catalog and Claude discovery aliases
without changing role assignments. Codex uses the canonical ID; Claude uses its advertised alias (including `[1m]`
when offered). `/ship` and `/fix` only check for drift and stop rather than syncing a
model change into an already-loaded session. Keep agent model settings unchanged during a run; no model override or
nested agent runner after a spawn error. Report the exact error. Never commit/push/tag/release, reset/stash/clean,
change production, or run destructive/external checks without separate authorization.

## 2. Run setup and immutable contract

Create a unique RUN_DIR under `plans/.ship-runs/` or `.fix-runs/` with the following standalone helper; it
creates the directory exclusively and atomically publishes valid `run.json`. Do not write `run.json` by hand or
reuse an existing directory. Repeat `--repo REPO` for each assigned repo, supply a real description and for ship
pass `--plan-path` with the absolute approved plan path:

`python3 -I -B .agents/workflows/ship-fix/scripts/ship_state.py init-run --run-dir RUN_DIR --workflow fix --repo REPO --description DESCRIPTION`

Keep run.json's legacy fields and add `schema_version: 2`, `workflow: ship|fix`. Record actual session_id
(or empty string) for the first main session, started_at UTC, run_id, repos and plan_path/description.
Every phase-start/phase-end event is the authoritative record of the session it ran in: it carries the
actual host session_id, or an empty string when the host does not expose it; never guess. run.json.session_id
remains the first session only and is not overwritten on rotation. When the orchestrator rotates to a new
main session mid-run, follow the "Orchestrator rotation" rule in `ship/SKILL.md` step 2 and
`HOST-ADAPTERS.md`, and record the new session_id in every subsequent phase event. Earlier phase events keep
their original session_id; do not rewrite history.

Write contract.json once. Example (paths/commands must be resolved, not literal placeholders):

```json
{
  "schema_version": 2, "workflow": "fix", "run_id": "example-fix-20261012-120000",
  "telemetry_policy": {"repair_phase_required": true},
  "repos": ["app"],
  "repo_paths": {"app": "/absolute/repo"},
  "criteria": [{"id": "fix.ac-1", "repos": ["app"],
    "mandatory": true, "kind": "empirical", "source": "original bug report",
    "requirement": "Exact expected result", "verification": "Relevant scenario and assertion",
    "environment": "isolated test runtime", "data_scope": "synthetic null-input fixture"}],
  "required_gates": {"app": [{"id": "gate.unit", "kind": "empirical",
    "command": "make test", "verification": "unit suite passes"}]},
  "pre_review_checks": {"app": [{"id": "early.syntax", "kind": "empirical",
    "command": "npx tsc --noEmit", "verification": "syntax valid"}]},
  "preflight_checks": {"app": [{"id": "env.runtime", "kind": "static",
    "verification": "Inspect actual mount/workdir and confirm safe test connection"}]},
  "required_passes": {"app": ["logic", "security", "coverage", "di", "migrations"]},
  "prerequisites": [], "cross_repo_contracts": [],
  "acceptance": {"original_scenario": "Exact failing input/state", "expected_behavior": "Exact expected result",
    "non_goals": "Preserve unrelated behavior", "mode": "improvement", "criterion_ids": ["fix.ac-1"]}
}
```

New ship/fix contracts explicitly include `telemetry_policy: {"repair_phase_required": true}`.
The helper validates an object with a boolean repair_phase_required; old v2 contracts without policy
remain compatible. Never retrofit policy or phases into historical contracts/runs/certificates.
For policy-enabled implementation, the implemented transition request records `original_agent_id`
from the actual original implementer instance. Subsequent requests cannot override that identity.

Every required gate/check has a unique ID, `kind`, `verification`, and for empirical checks exactly one of
`command` (shell string) or `argv` (non-empty list of strings, quoted exactly once; its canonical command
identity is `shlex.join(argv)`, which evidence must repeat). Prefer `argv` or an existing repo script with a
checkable exit status over nesting one shell string inside another: an outer shell expands `$?`/`$(` before the
inner one. Run each new empirical pre-review/preflight command
once against the minimal source before spending implementation effort, and check the real exit status.
Parallel syntax pre-review example: `argv:
["sh","-c","find app components lib -name '*.ts' -o -name '*.tsx' -print0 | xargs -0 -P8 -n25 npx tsc --noEmit >/dev/null"]`.
Pipeline exit is xargs's, nonzero on any type error, so no `$?` test is needed.
When there is no runner, use a justified explicit inspection only if the requirement is truly static;
never substitute inspection for runtime correctness. When preparing a new contract, the orchestrator reads
each target repository's `.agents/skills/review/SKILL.md` and lists its complete set of review passes in
`required_passes[repo]`, using stable IDs for the rubric topics. Reviewers may mark a conditional pass
not-applicable with reasoning. Criteria include all selected
requirements, environments/data scope, stable IDs/source references; do not relabel empirical as static.
Multi-repo requirements name all repos; `cross_repo_contracts` reference their criterion_id.
For a cross-repo requirement whose final proof depends on later implementations, a new contract may set
`assessment_repo` on that mandatory criterion to the last participating repo in contract execution order.
Every participant must also have its own mandatory single-repo criterion covering its local contribution.
The deferred criterion remains mandatory at that final repo's acceptance and completion, and its evidence
must bind every other participating repo's current source manifest. Repo acceptance before then certifies
only its local criteria; it does not certify the cross-repo requirement. `complete` revalidates the final
assessment and all source bindings. Omit `assessment_repo` for legacy behavior. Never modify an existing
sealed contract to add it. Checks that depend on a later operation (such as consumer sync) belong to a
separate criterion assessed after that operation, not to an earlier repo's acceptance.
Prerequisites have id, repo, kind, verification and evidence_paths, verified independently before edits.
Excluded required repos remain gaps: do not silently remove them from criteria to satisfy repo validation.

For ship, add `plan_path`, `approved_snapshot` (immutable full approved plan copy), `plan_sha256`,
selected scope/sections/non-goals, and excerpts. Freeze the exact approved bytes, not a paraphrased plan.
`--repos` limits execution, not obligations. If a required repo is excluded/unverified, stop or report
partial work, never complete. The checker confirms extraction correctness. Plan changes during execution
block completion. Execution status/handoff edits and archival happen only after the completion certificate;
they do not rewrite approved snapshot/requirements.

For fix, acceptance mode is `reproduced-bug`, `improvement`, or `unavailable-baseline` (requires reason).
A reproduced bug records `before_evidence: {repo: absolute_evidence_path}` on the initial source state.
Before and after evidence share the exact `scenario` string. Before failure is historical evidence, not
proof of final code; never invent it. Each criterion_id refers to an actual acceptance criterion.

For each new run validate before use, including every declared slice schedule, with a unique attempt ID:
`python3 -I -B .agents/workflows/ship-fix/scripts/ship_state.py validate-run --contract CONTRACT_PATH --attempt-id ATTEMPT_ID`.
This records a run-wide `setup` phase before contract validation and a passed or failed end after its real
result. A crash or interruption leaves an open phase; never invent an end. A failed setup blocks preflight and
implementation. Retrying uses a fresh attempt ID and never overwrites events. `validate-contract` remains a
read-only validation command for existing runs and does not create setup telemetry.

### Replacing a setup-only run

When a frozen contract cannot represent the approved work, preserve the original run and create a new run with
`init-run`. Prepare its replacement contract as `RUN_DIR/inputs/replacement-contract.json`, including normal
`budget_continuity` and `successor_scopes` for all actual lifecycle predecessors. Then invoke:

`python3 -I -B .agents/workflows/ship-fix/scripts/ship_state.py replace-setup --contract OLD_CONTRACT --successor-draft NEW_DRAFT --reason REASON --read-policy READ_POLICY_JSON`

The helper publishes the original run's `setup-replacement.json` and the new `contract.json`. Validate the new
contract with `validate-run` before continuing. A replacement is permitted only when the original run's complete
available history shows setup activity and no lifecycle state, implementation, review, or budget consumption.
The original contract and setup telemetry are preserved. Both runs must identify the same repositories and work
items. A changed seal, conflicting replacement, missing related history, cycle, or inaccessible excluded path
blocks replacement. Completion of the successor requires the ordinary independent review, gates, and assessment.

Use absolute paths. The replacement draft must be `NEW_RUN/inputs/replacement-contract.json`, with the new
run's `run_id` and all ordinary contract fields. It must omit `setup_replacement`. Each run must use the same
workflow, ordered repositories, repository paths, and `budget_work_items`. The old contract must be parseable
and identify those fields even when validation failed. `replace-setup` writes `setup_replacement` containing
`contract_path`, `contract_sha256`, `reason`, and `files` mapping every original artifact path to its SHA-256.
The old run's closure binds that declaration, the work-item and repository maps, the successor run ID and
the new contract path and SHA-256. Rerun the same command
after interruption to finish publication. The helper records its publication transaction under the new run's
`inputs/setup-replacement-transaction.json` and serializes publication against workflow operations on both runs.
A different draft, reason, or destination is refused.

Supply `--read-policy` as a JSON object with `repo_paths` and `read_excludes`, containing the union of the
exclusions already known to the orchestrator from both contracts and every predecessor. Supply `{}` when none
has exclusions.
The helper applies this bootstrap policy before opening either contract and rejects any omitted contract
exclusion. The policy argument must itself be complete, because contract contents cannot be read safely to
discover an exclusion that forbids reading the other contract.

The helper reserves audited predecessor scopes with write-once `successor-reservation.json` records. It
refuses an open work phase or a repair phase that is not accounted for by lifecycle history. Stop work on
predecessors before invoking it. The current helpers coordinate their state changes and honor reservations.
An older installed helper, host file write or external process that does not use this coordination can still
write concurrently. Stop those processes before choosing a predecessor and preserve their recorded histories.

The helper-generated contract member and closure have the following shape. Every path is absolute and every
hash is SHA-256 over the file bytes. `files` includes the old `run.json`, contract, setup events and all other
original files.

```json
{
  "setup_replacement": {
    "contract_path": "/absolute/old-run/contract.json",
    "contract_sha256": "OLD_CONTRACT_SHA256",
    "reason": "Reason for replacing the setup",
    "files": {"/absolute/old-run/run.json": "RUN_METADATA_SHA256"}
  }
}
```

```json
{
  "schema_version": 2,
  "status": "replaced",
  "run_id": "old-run",
  "workflow": "fix",
  "work_items": {"app": "stable-work-item"},
  "repo_paths": {"app": "/absolute/app"},
  "replacement": {"contract_path": "/absolute/old-run/contract.json", "contract_sha256": "OLD_CONTRACT_SHA256",
                  "reason": "Reason for replacing the setup", "files": {"/absolute/old-run/run.json": "RUN_METADATA_SHA256"}},
  "successor_run_id": "new-run",
  "successor_contract_path": "/absolute/new-run/contract.json",
  "successor_contract_sha256": "NEW_CONTRACT_SHA256"
}
```

Setup events and their contract seals are retained byte-for-byte. For runs predating setup contract seals, the
contract hash is first bound by the replacement. The helper rejects non-setup telemetry, lifecycle state files,
work and budget artifacts, symlinks within the run, and histories that cannot be read under the combined
`read_excludes`. Both the successor and any subsequent successor must account for all locally identified related
runs. A closed setup remains unverified. Its digest reports `replaced`; successor digests and completion
certificates list its reason and sealed artifact paths.

For each started predecessor slice, run `validate-history --contract CONTRACT --repo REPO --slice SLICE` and
copy its `history_sha256` into the successor's slice continuity record as `source_history_sha256`. Include the
record and matching successor scope even if repo lifecycle state has not started. A started slice must map to
one slice in the replacement's approved schedule. If that mapping is ambiguous, resolve it before continuing.
The history hash covers the contract, ordered slice states and prior slice history. Existing contracts without
this optional hash continue to use their original validation rules.
All commands below use actual absolute variables, not literal placeholders.

For policy-enabled repo repairs, `phase-start` validates the current state path/hash, action, author and
round or attempt before dispatch. Capsule rotation also requires the current run-local capsule at start;
`phase-end` repeats these bindings and names the observed new author. Invalid bindings are rejected before
the phase event is written.
Telemetry for slice-level phases supplies `--slice-id` on both boundaries; slice IDs must belong to the
repo's approved schedule and are checked against one another.

## 3. First-touch source and preflight

Initialize a repo's touched-files JSON array, baseline directory and starting source manifest. Record
starting HEAD/status/dirty WIP. For non-git or ignored dependencies, maintain a separate source-scope
array containing relevant config/source dependencies, without widening writable scope. Pass both arrays
to transitions. Git capture includes all tracked/nonignored source plus explicit paths. External symlink
contents need separate authorized dependency verification; symlink target text does not bind contents.
Capture uses two matching scans. Artifact checks use initialized source dependencies and exclusions for both
captures. Same untracked generated-output exclusions must be used throughout;
tracked/scoped source cannot be excluded. No blanket cache/source-directory exclusions.

Capture records a single path when Git, the directory listing or explicit scope supply alternate
spellings of the same filesystem entry. It prefers the Git index spelling for tracked paths and the
directory spelling for other existing entries. Distinct entries remain separate, including hard links.
Two index paths resolving to the same entry are rejected. Exact scope paths and directory prefixes
can refer to an entry by either confirmed spelling. Globs keep their case-sensitive pattern matching.
Batch ownership must still resolve to exactly one writer per changed entry.

Historical manifests retain their stored paths and fingerprints. Live freshness and file ownership
comparisons can reconcile alternate spellings while the current checkout confirms the entry and all
recorded states agree. Conflicting records are rejected. If spelling identity cannot be confirmed,
the paths are compared separately. Review, authorization and seal hashes are still checked against the
artifacts that recorded them.

Before first edit/delete/rename (both paths):
`python3 -I -B HELPER_PATH baseline --repo REPO_PATH --baseline BASELINE_DIR --path RELATIVE_PATH`.
Union every changed/deleted path into touched-files; never shrink it or overwrite baseline records.
`capture --repo ... --scope SOURCE_SCOPE --out UNIQUE_MANIFEST` and
`delta --repo ... --scope TOUCHED_FILES_OUT --baseline ... --out UNIQUE_DELTA` remain available.
The helper compares against initial source and blocks changed paths outside writable scope or baselines
captured after a concurrent edit. Resolve ownership instead of adopting unrelated edits.

Preflight precedes implementation. For each mandatory acceptance/gate determine command/assertion,
environment, data scope, fixture availability and authorization. Inspect container mount/workdir before
assuming a command runs assigned source. A DSN alone does not prove isolation. Check prerequisites,
test runners, fixtures, benchmark baselines and isolated DB when needed. Probes use normal permission
checks; do not prepare databases or call brokers autonomously.

Write bound `preflight.json` with status passed/blocked, evidence_paths and blocker reason. A passed
preflight requires independent evidence for every declared preflight check and prerequisite. Unknown or
unavailable environment is blocked. A user-authorized code-only continuation records bound authorization
JSON with `user_authorization`; it can reach implemented, never verified/completed/archive. No silent waiver.

## 4. Evidence and self-check

Every v2 artifact contains schema_version, run_id and repo. Evidence uses the existing fields:
criterion_id, producer (`implementer|orchestrator`, or `reviewer|checker` for static inspection only),
kind, status, command/exit_code or assertion,
environment, data_scope, before_manifest, after_manifest, artifact_paths. Add explicit
`claims: [{"id": "criterion-or-finding-ID", "assertion": "What this check actually proves"}]`.
One batch check may support multiple claims only if each maps to a real assertion. Fix scenario evidence
also carries `scenario`. Multi-repo criterion proof includes `dependency_manifests: {repo: path}`.

Execute checks with normal tools, save actual sanitized logs and manifests before/after. Never create
passing logs from claimed results. Then use `record-evidence --contract ... --repo ... --file INPUT_JSON
--out UNIQUE_RECORD` to bind version/run/repo and compute `artifact_hashes` for logs/manifests. This helper
never runs the command. It also accepts the same fields as flags (`--claim-id`, `--claim-assertion`, `--kind`,
`--producer`, `--status`, `--environment`, `--data-scope`, `--before-manifest`, `--after-manifest`,
`--artifact`, `--reason`) or `--from-file`; flag mode supports one claim. `--batch FILE` (or `-`) takes a JSON array or `{"records": [...]}` of repo/out/record entries, validates all records before writing, and prints a per-record `{id, status, path, error}` result; `--stop-on-fail` stops at the first error. Preserve failed/unavailable results too. Unavailable empirical exit_code is null;
failed exit is nonzero. Checks that change source cannot certify it: stabilize, review and rerun.
After later edits rerun affected checks rather than overwriting proof. Independent gates need orchestrator
producer. A matching source hash establishes freshness, not the validity of an assertion. Source files are version-bound by their event's `source_manifest`/`source_fingerprint`; they are not immutable run artifacts across a later authorized edit. Prefer manifests for source binding and place logs, review reports and other immutable evidence in `artifact_paths`, not the source files already represented by the manifest. Historical lifecycle validation confirms that any newly introduced source-file seal agrees with that event's historical manifest. Because lifecycle events carry earlier seals forward, a source-file hash that differs from the current event's manifest is accepted only when it is exactly the same seal already validated in the immediately preceding event; this permits a later authorized edit without rebasing old evidence. Non-source artifacts (including logs, proof records, reviews, requests and manifests) remain hash-sealed and any mutation blocks progression. Current-version/scope freshness remains the next transition's responsibility.

Record the exit status of the check command itself, never of a pipeline tail: `cmd | tail` exits 0 when
`cmd` fails, so redirect output to a log file and read `$?` of the command (or use an `argv` check whose last
pipeline stage is the real tool). A log with failures next to `exit_code: 0` is invalid evidence.

Shared scratch resources: a DB-backed check (integration, concurrency, migrations) must not run while another
process uses the same scratch DB (for example, a service's test database container); concurrent runs produce deadlocks
and shifted row counts that look like
product failures. Immediately before
each such check, probe read-only for other users (e.g. `ps` for a running `go test` and `show
processlist` on the MySQL). If busy, wait or report `blocked: scratch busy`; never kill a process
you did not start, and never record the contaminated run as product evidence. Rerun after the foreign
process ends and keep the failed log as an unavailable/failed record, not an overwritten one.

To bind these observations to the run, declare them as `argv` checks in the contract's `preflight_checks` (for example
mount, effective DSN/database, and idle probes that exit non-zero when the scratch is busy) and run them with
`python3 -I -B .agents/workflows/ship-fix/scripts/ship_run_checks.py --contract C --repo R --scope SCOPE --out-dir NEW_DIR --group preflight_checks`
immediately before the DB-backed gates (also usable with `--group required_gates` / `pre_review_checks`). It runs each declared argv
unchanged, saves the log and real exit code, captures before/after manifests and records orchestrator evidence through
`record-evidence`, keeping failed results with a `reason`. Output directories are write-once; use a new one per run.

Implementer writes a bound matrix JSON with `criteria` rows:
`criterion_id, implementation, assertion, status: verified|unverified, reason, evidence_paths`.
Every mandatory repo criterion appears. Check fixture domain identity, defect-sensitive assertions,
observable output (not only mock calls), consumers and non-goals. Do not advertise unrun tests as proof.
Targeted mutation checks may help critical financial invariants; a full mutation suite is not mandatory.

## 5. Shared lifecycle and CLI

Save a unique transition request JSON and invoke:
`transition --contract CONTRACT_PATH --repo REPO --file REQUEST --out RUN_DIR/REPO/state-N.json`.
Use next N; immutable records/counters are authoritative, not free-text reports. Every request names
status, source_manifest, scope_path; pass baseline_dir and source_scope_path when applicable. The helper
recaptures actual source and verifies previously sealed artifacts. Scope arrays are mutable convenience
files; each event saves their values. Generated outputs belong outside source or in justified exclusions.

| Transition | Required input/proof |
|---|---|
| initialized | initial stable manifest and scope before editing |
| ready / preflight-blocked | preflight_path; blocked also has reason |
| implemented | matrix_path; code-only from blocked additionally code_only_authorization |
| pre-review-passed | matrix_path and pre_review_evidence_paths |
| needs-fix / reviewed | review_path, stable accepted fingerprint, complete coverage and dispositions |
| reviewed-with-findings | user decision bound to the exhausted review and unchanged source |
| edited-unverified | action review-fix/gate-fix/plan-gap; fix artifacts below |
| gates-passed | full independent gate_evidence_paths, current clean or user-accepted review |
| verified | assessment_path and all mandatory current acceptance proof |
| blocked / capped / handoff | concrete reason, never offer Proceed |

Run cheap checks and targeted tests at explicit pre-review stage before expensive independent review.
Gate failures return through original implementer, pre-review, fresh review, full final gates; previously
green affected gates rerun. No reset of counters. At most 3 review-fix passes (plan-gap repairs share this
budget), plus final independent review. Gate-fix has at most 2 attempts per repo across the work item's run
history. After the last available review fix, review findings of any severity mean capped until the user decides. Source edits invalidate
review/evidence; final independent review is required after every repair, including repairs beyond pass 3.
Interrupted edits stay unverified.

Before preparing any repo-level review-fix, plan-gap or gate-fix, the orchestrator runs
`python3 -I -B .agents/workflows/ship-fix/scripts/ship_state.py review-budget --contract CONTRACT --repo REPO`
as one standalone read-only command from the project root. This check is first: it precedes repair-phase
telemetry, `ship_artifacts.py init-task`, capsule/handoff preparation, and author dispatch. It verifies the
immutable history and reports the exact latest state path/hash, current limit, used/remaining passes,
open finding IDs, full finding records, counts by severity, and whether current scoped source still matches that state. Never prepare a repair when
`source_matches_state` is false. For already accepted, checkpointed slice repairs, follow the repo repair
recovery procedure below. Resolve other source drift before preparing a repair. Repeat the
check immediately before each subsequent repair, including after a grant is applied. For gate-fix use
`gate_fix_budget.remaining_attempts`. If zero, report the historical, current and cumulative usage, record
the failed gate and stop gate repair. A review-budget grant cannot add gate-fix attempts.
For a slice add `--slice SLICE` to inspect its independent budget and findings.

### Explicit repo-level review-budget grants

New contracts declare `budget_work_items` as a map from each repo to a stable, nonempty work-item identifier.
Reuse the same identifier for the same approved work across ship and fix runs. Before creating a run, inspect
both `plans/.ship-runs/` and `plans/.fix-runs/`, including legacy run metadata and descriptions, for related
work. Resolve ambiguous legacy matches with the user; never assume that a missing identifier means unrelated.
Continue the existing run when possible. For identified runs, transitions, budget checks and completion refuse
an uncarried local related contract. This lookup cannot discover missing, moved, unidentified, or inaccessible
histories and is not proof that the search is complete.

If independent histories already omitted their carryover, reconcile them in a new successor, never by editing
the old histories. A repo-level `budget_continuity` record may use `recovery_sources` instead of its single
source. Each source contains `run_id`, absolute `contract_path` and `state_path`, their `contract_sha256` and
`state_sha256`, `history_sha256`, and `used_passes`. Supply all related independent runs once. Each must pass `validate-history`
and name its latest state and the same repository path. The record and its JSON decision contain the same
`repo`, `work_item`, `approved_by: "user"`, `approval_question`, exact `user_authorization`, `approved_at`,
`reason`, `additional_passes`, `used_passes`, `effective_limit`, and `recovery_sources`. The record additionally
contains absolute `decision_path` and its `decision_sha256`. Obtain explicit approval of the actual cumulative
usage and positive additional passes; the effective limit is 3 plus previously applied grants plus this decision's
additional passes. The helper verifies both totals and all seals. Four used with +2 and no prior grants leaves
one pass. Nested continuity and historical-source grants are refused rather than double-counted; stop and report
those cases. This decision accounts for usage only: it does not validate past work, waive prerequisites, or
retroactively authorize past repairs. Perform fresh review, gates and assessment before any new certificate.

The default repo-level budget is always 3 unless a user explicitly approves one or more additions during the
same run. It is distinct from gate-fix (2) and slice budgets. When capped or at needs-fix with no remaining
passes, the orchestrator shows the repo, limit, used count, every open finding ID/severity/failure scenario
(or plan-gap criterion and remaining work), then asks using host's user-input capability for a positive number N,
accept all open review findings, or stop. Acceptance applies only to review findings, not plan-gap or failed gates.
No automatic renewal. An approval to implement this mechanism is not approval for any particular run.

To accept findings, first record `capped` after `needs-fix`. Show every finding's ID, severity, category, file,
line, description and failure scenario and the count by severity. Save a unique run-local JSON containing
schema_version, run_id, workflow, repo, optional slice_id, contract_sha256, approved_by `user`, approval_question,
user_authorization, approved_at UTC, previous_state_path and previous_state_sha256, review_path and review_sha256,
review_round, source_fingerprint and sorted finding_ids. Use `transition` or `slice-transition` with status
`reviewed-with-findings` and `review_acceptance_path`. The helper binds the decision to the current review and
source. Continue to slice acceptance or required gates and acceptance assessment. Every edit requires fresh
review and a new decision if findings remain at exhaustion. Report all remaining findings by repo and slice,
with severity, location, scenario and whether the user accepted them.

Existing runs can record review-fix carryover in a user-approved grant as `historical_budget_sources` plus
`historical_used_passes`. Each source names the absolute contract/state paths and their SHA-256 values, run ID,
used passes and effective limit. The helper revalidates those prior immutable histories and hashes whenever it
reads the grant. Historical runs are read-only. The effective allowance in the successor is
`max(0, 3 + sum(new-run grants) - historical_used_passes)`; current-run repair counters remain separate. A
grant is applicable when validated historical usage plus current-run usage has exhausted the base allowance.
Assessment and completion reports distinguish carried-over usage, current-run usage, cumulative usage, grants
and remaining effective allowance. A grant cannot reduce historical usage, reference its own run, or cite a
modified/invalid historical state. When no historical source is supplied, legacy per-run behavior is unchanged.

A new repo successor declares `budget_continuity` before initialization. This is an array of scope records, one per repo
or slice, and a matching `successor_scopes` array of `{repo, slice_id, work_item}` records (omit `slice_id` for
a repo). Each continuity record contains `repo`, optional `slice_id`, `source_slice_id` for a slice, `work_item`,
`source_contract_path`/`source_contract_sha256`, `source_state_path`/`source_state_sha256`, `used_passes`,
`effective_limit`, `approved_by: "user"`, `user_authorization`, `reason`, `approved_at`, and
`decision_path`/`decision_sha256`. Paths are absolute; the source state must be the latest state in its
original run. The decision is a user-approved accounting decision, not proof that missing lifecycle events
occurred. For example, 10 used out of 12 leaves two new-run repairs. The new run's repair counter starts at zero;
reports show historical and new usage separately. Keep the source history and decision immutable. Do not mix
this mechanism with legacy historical sources in a grant. A later exhaustion needs a normal explicit grant.
The same source cannot be imported twice, and cycles and reduction of recorded usage are refused.

Before writing a repo successor contract, run `validate-history` for each predecessor. Copy its latest state
path and SHA-256 and its `history_sha256`. Set `source_history_sha256` for a single predecessor or
`history_sha256` on each entry in `recovery_sources`, including in the recovery decision. This applies even
when the predecessor used zero gate-fix attempts. Stop using a predecessor's lifecycle once its state is
selected for continuation. The helper checks each predecessor is still at its cited latest state. Historical
validation follows the predecessor links and checks any scope amendment against its sealed schedule. It
continues to work after successor contracts appear. Active contract validation checks for missing related runs
against the full audited predecessor lineage.

The history hash is SHA-256 over canonical JSON with keys `contract_sha256`, `states` (ordered
`[filename, SHA-256]` pairs) and `sources` (sorted `[canonical absolute contract path, history_sha256]`
pairs). Encode JSON with sorted keys, ASCII escaping, and no insignificant whitespace. Use the helper output
when preparing a contract. The helper follows validated repo history links, including existing historical
review grant sources. Related-run discovery counts sources from sealed applied grants as already carried
forward. Every identified ancestor must match the successor's work item. The helper counts gate-fix events
once per predecessor, rejects conflicting snapshots and cycles, and derives all counters from history.
For single-source continuity, an absolute alias for the latest predecessor state must resolve to that file
and match its SHA-256.
Slice continuity accounts for slice review-fix only.

New repo states seal their immediate predecessor and record `gate_fix_budget` and
`gate_fix_sources_sha256` (SHA-256 of the canonical `sources` array). The budget object has integer fields
`limit`, `historical_used_attempts`, `current_used_attempts`, `cumulative_used_attempts` and
`remaining_attempts`. `gate_fix_count` remains the local counter, and gate-fix telemetry attempts are numbered
within the current run. Gate-failure evidence uses the same normalized path spelling when sealing and
checking files. Repo `review-budget`, `digest` and `validate-history` expose the same budget object.

Initialized legacy runs can use their existing continuity or applied grants. The helper derives their usage
from available verified histories and applies the remaining allowance immediately. It reports the actual
cumulative count even if old runs together exceeded 2, with zero attempts remaining. Legacy contracts,
states, seals and certificates are read-only. Older events may have incomplete hash coverage before their
first full history snapshot. Use documented work item discovery to reconcile any older histories whose
links were omitted.

After a yes, write a unique JSON under `RUN_DIR/budget-grants/` with schema_version, run_id, workflow, repo,
contract_sha256, additional_passes (positive int, bool is invalid), purpose (`review-fix` or `plan-gap`), reason,
`approved_by: "user"`, exact approval_question and user_authorization response, approved_at UTC,
previous_state_path and previous_state_sha256, review_path/review_sha256/review_round and exact sorted finding_ids;
for plan-gap, provide criterion_ids instead of finding_ids and an assessment path/hash when one exists. Preserve actual question/answer correlation IDs only
when available, never guess them. The JSON is user-approved by process/procedure; its checksum seals contents but
is not a cryptographic proof of human identity.

Apply the grant before any repair preparation with a separate transition request containing budget_grant_path
and no source change, author handoff, scope change, replacement review/proof, task initialization, or repair
telemetry. Its exact predecessor state and source must still be current; helper rejects a grant if source bytes
changed after the state, even if the grant was already approved. From needs-fix it is
a same-state needs-fix transition. From capped it restores only the immediately prior eligible review/plan-gap
context, without generating a review round or certifying new source. Gate-only capped states cannot receive a
review grant. Helper validates the run-local path, contract/run/repo, current exact predecessor state hash,
time ordering, author/source/scope/base and review binding/finding set (or plan-gap criteria), rejects one grant
per predecessor/reuse/copies, seals the artifact and derives the effective limit from applied immutable state
events only. Caller-supplied contract/request/state limit values never grant budget. Each later approval binds to
the immediately preceding immutable state; grants accumulate (e.g. +1 then +1) without resetting counters or
latest_review_round. Both review-fix and plan-gap use `3 + sum(applied additional_passes)`.
Apply the ordinary repair proof, pre-review, fresh review, gates and acceptance afterward.

An applied grant event is not a review result and must be omitted from review-round numbering, dispositions and
prior-review packets. Old histories with no grant fields remain valid and use limit 3. Do not rewrite old
contracts, state files or completion certificates. New state/certificate records expose grants and derived
limits. If approval is declined, stop/cap; do not create a replacement run to reset counters.

For review-fix: request action=review-fix, applied_path. Applied JSON retains round/applied/failed/skipped;
every finding exactly once. Applied objects include id, verified, verification_kind, evidence_paths.
Empirical proof must exercise behavior; structural proof can be concrete inspection. Edited-but-unverified
belongs in failed with explanation. For gate-fix: action=gate-fix and gate_failure_path of actual failed
independent check; original implementer writes unique gate-attempt result, no fake findings round.
For plan-gap: action=plan-gap, original implementer fixes the goal gap, then repeat review/gates/assessment.
No budget reset. At exhaustion, obtain the user decision above; do not create a new run to disguise exhausted counters.

`--from-review`: no contextless fixer; hand findings/gate failures back to original author session. Use
verified pre-implementation revision or true first-touch baseline, never snapshot finished code as base.
Initialized source is current submitted code; review delta uses supplied base separately. Author repairs
are recorded with author_handoff and plan-gap action before repeat pre-review/review. Missing safe baseline
or prerequisites block. Returning to review may adjudicate an unchanged disputed finding with evidence,
not invent an applied result. Counters/rounds stay monotonic.

### Slice schedule (optional, large repo scope)

For a repo scope too large for one bounded implementer dispatch, `slicer` (read-only) proposes slices and
a human approves them before any source edit. The orchestrator saves the proposal, shows the table (id,
depends_on, parallel, write_scope, criteria, interfaces, unknowns), asks via host's user-input capability
(approve / change / do not slice), and on approval writes immutable `RUN_DIR/slices-approved.json`:
`{schema_version, run_id, repo, approved: true, approved_by, approved_at, max_parallel (1-3, default 1), slices,
cross_slice_criteria}` and declares `slice_schedules: {repo: {path, sha256}}` in contract.json. Validate with
`python3 -I -B HELPER_PATH validate-slices --contract ... --repo ... --file slices-approved.json`; it prints
the execution `groups` computed from depends_on (never from the proposal). Rules enforced there: every mandatory
criterion has a contribution; criteria with several contributing slices are listed in `cross_slice_criteria`
and still need a final whole-criterion proof; no unknown/self/cyclic dependency; write_scope is a relative
path or glob; slices share a group only if all allow parallelism, scopes (conservative prefix overlap) and
resources are disjoint and the group fits max_parallel. A computed group with several members runs as one parallel batch (below); single-member groups and every
group when max_parallel is 1 run one writer at a time.

### Slice lifecycle and final integration review

Each slice has its own immutable history `RUN_DIR/REPO/slices/SLICE/state-N.json`, driven by
`slice-transition --contract --repo --slice --schedule --file --out`:
`implemented → pre-review-passed → (needs-fix → edited-unverified → pre-review-passed)* → reviewed → accepted`.
At an exhausted review, `needs-fix → capped → reviewed-with-findings → accepted` records the user's decision.
Requests carry `source_manifest`, `base_manifest` (captured before the slice started; never refreshed), the
cumulative `touched` list, and per status: `implemented` (author_agent_id, slice matrix with one row per
contribution), `pre-review-passed` (the repo's `pre_review_checks` evidence), `needs-fix`/`reviewed` (a fresh
review of the slice's touched files, round +1, all rubric passes), `edited-unverified` (`review-fix` with
applied_path, or `integration-fix` for an already reviewed/accepted slice), `accepted` (clean or user-accepted review on the
current fingerprint). Enforced: touched files lie inside the slice write_scope; every change since base is a
declared touched file; predecessors must be accepted first; repairs go to the slice author (replacement needs a
human-approved handoff); at most 3 review-fix passes per slice; `blocked`/`capped` need a reason and end the
slice until a human continues. A bounded human budget extension uses `capped → needs-fix` with `continuation_path` pointing to immutable run-local JSON: schema_version/run_id/repo/slice_id, original author_agent_id, user_authorization, approved_at UTC, exact previous_state_path/SHA-256, current finding_ids and positive integer additional_passes. The helper seals this authorization, requires unchanged source/base/scope/review, preserves counters and derives review_fix_limit from the prior limit plus the extension; no new review round is fabricated. Authorization reuse, foreign state/slice/author and direct budget/counter overrides are rejected. New repairs still require normal proof, pre-review and fresh review; another exhausted pass stops again. Editing an accepted slice invalidates its acceptance and, through recorded
predecessor state hashes, every dependent slice.

#### Explicit integration checkpoints

Reopening an early accepted slice after later slices changed the shared checkout must not replace its base
or adopt unrelated edits. A human-approved integration alignment may use a run-local `integration_path`
checkpoint. Without one the existing ownership rules are unchanged. This is not a budget extension.

The run-local authorization is bound to schema/run/repo and the exact contract/schedule hashes. It records
`user_authorization`, `approved_at`, `starting_slices` (each original state path/hash and author), a per-slice
`edit_paths` allowlist, `recertify_slices`, and, if needed, explicitly authorized `partial_slices` paths from
an immutable `start_manifest_path`/SHA-256. Partial slices must have no lifecycle history; their checkpointed
source is preserved, not certified or accepted.

Each checkpoint names the authorization path/hash, slice/author, immediately preceding reviewed/accepted
state path/hash, `mode` (`repair` or `dependency-recheck`), timestamp `at`, original contract/schedule hashes,
and an immutable `start_manifest`/hash captured before the slice edits. `slice_states` binds every sibling
in the authorization (except the target) to its current accepted state path/hash; omission is rejected,
even if its source was reverted before operation-start capture. `partial_slices` must exactly match the
authorization, including entries whose files were deleted before capture. At every transition sibling source must match both the checkpoint and acceptance;
partial source must match the authorized snapshot. Independently of supplied bindings, every non-target
path is frozen between operation-start and final manifests, and every foreign change already present at
operation start must have accepted-sibling or explicit partial provenance. Omitting a binding cannot hide
an unauthorized revert to the original base. The authorization start manifest is always validated/sealed;
its HEAD, the anchored source HEAD, operation-start HEAD and final HEAD must all agree. Unknown foreign
paths, changed sibling states/source, HEAD drift, foreign authors, changed base/touched scope, checkpoint reuse or uncertified sibling versions
block. Checkpoints, authorization, manifests and sibling state records are sealed like other proof artifacts.

A repair uses the normal `edited-unverified`, `action=integration-fix` lifecycle and stays with the original
author. A read-only dependency recheck may instead take `accepted → pre-review-passed` with
`action=dependency-recheck`, an authorized checkpoint, current matrix and newly executed pre-review evidence.
It may not change the slice's source; it still needs the next fresh review round before re-acceptance and
new dependency hashes. It does not manufacture an edit or reset counters. Acceptance/implementation also
require recursively current predecessor source and dependency bindings; an old accepted status alone is
insufficient. Whole-scope integration review and completion obligations are unchanged.

When a slice previously recorded `predecessor_states`, a validated integration checkpoint supplies the
current accepted bindings for dependencies it names. The next state records those bindings while retaining
the original base and all earlier sealed states. Unnamed dependencies retain their previous bindings.

If pre-review is blocked by an already checkpointed, unsubmitted partial slice, do not edit that frozen
source or replace the checkpoint silently. A separate human-bound `resume_authorization_path`/SHA-256 on
a new alignment authorization may permit an exact partial repair: schema/run/repo/slice, original author,
user authorization/time, paused edited-unverified state path/hash, superseded checkpoint path/hash, paused
source manifest/hash and per-partial-slice repair paths. These paths must be a subset of the original
checkpointed partial paths; accepted siblings, target edits, HEAD, original permissions and history remain
unchanged. The repaired start snapshot must differ from the paused source only at these authorized paths.
The new checkpoint retains the original accepted anchor and starts from the authorized repaired snapshot.
It may enter `pre-review-passed` with `action=integration-resume` only from that exact paused state, with
newly executed checks and no additional target edits. This records a partial-source repair, not a new
slice edit, acceptance or budget reset. All superseded proof remains immutable and the resumed operation
still needs a fresh review before acceptance.

`slice-gate --contract --repo --schedule --initial REPO_INITIAL_MANIFEST --manifest CURRENT --out` certifies that
every slice is accepted, its touched files and predecessor states are unchanged since acceptance, and every repo
change since the run start is owned by some slice. It records `slice_authors` and `cross_slice_criteria`.

The repo-level lifecycle then runs over the WHOLE diff. Both `implemented` and `pre-review-passed` require
`slice_gate_path` (fresh for the current fingerprint) and, on `implemented`, `slice_authors` equal to the gate's.
The repo matrix must prove every criterion as a whole; slice matrices are partial and never complete a
cross-slice criterion. The final fresh review reads the entire scoped diff and all mandatory passes; slice
reviews are prior context, not permission to skip. It adds what slice review cannot see: interface agreement on
both sides, DI/wiring and shared files, duplicated or diverging behavior between slices, unseen consumers,
non-goals, cross-slice criteria. Repo-level review-fix/gate-fix budgets (3/2) are unchanged and separate from the
per-slice budgets. A final-pass fix that edits slice files uses `slice_id` (author routing) and then needs
`integration-fix` re-acceptance of the touched slices and a new slice-gate before the repo returns to pre-review.

#### Recovering a repo review fix after slice acceptance

An orchestrator may record the repo repair after the slice authors finish their approved integration operations
and all slices are accepted. Before starting these operations, check the repo budget against `needs-fix`.
When resuming already accepted repairs, `review-budget` may report source drift and point to this procedure.
Keep the current `needs-fix` as the recovery anchor. Capture the full repo scope and frozen dependencies,
then run `slice-gate` with the repo's original initial manifest and the new capture. Use that same capture
in the recovery transition.

Write a run-local recovery JSON with `schema_version`, `run_id`, `repo`, and `previous_state`, `review`,
`slice_gate` and `applied` references of the form `{"path": "ABSOLUTE_PATH", "sha256": "HASH"}`. Its `slices`
map keys are the slice IDs whose accepted states changed since the reviewed gate. Each entry has
`previous_state`, `checkpoint` and `accepted_state` references in the same form. Include dependency-only
rechecks whose acceptances changed. The operation must start at the old accepted state and retain its
checkpoint through the new acceptance. Each checkpoint needs existing user authorization after the repo review.
The `finding_slices` map keys are the applied finding IDs. The values list their changed slice IDs and must
cover every changed slice. Write the applied artifact as a new `round-ROUND-applied.json` in a repo round
directory named `rounds-*`. It uses the ordinary `applied`, `failed`, `skipped` arrays and proves applied
findings against the full final repo snapshot. The next repo reviewer adjudicates the findings.

Use `transition --contract CONTRACT --repo REPO --file REQUEST --out NEXT_STATE` with a request containing
`status: edited-unverified`, `action: review-fix`, `source_manifest` and `slice_repair_recovery_path`.
The helper verifies the original and current accepted versions, all checkpoint bindings, current gate and
the exact repo delta. It keeps the original author, scope, source dependencies and first-touch baselines.
The sealed slice states supply the repair record for this transition. Use the existing phase records as written.
One repo review-fix pass covers the whole recovery. At the cap, add `budget_grant_path` to the same request
and use a normal user-approved `review-fix` grant against the exact `needs-fix` and review. Include
`slice_repair_recovery_sha256` in this grant and bind it to the recovery JSON. This joint transition spends
one granted pass and records the grant once. Apply ordinary grants before edits when starting new repair work.

Resume at repo pre-review with the current full-scope matrix and fresh independent check evidence. Then obtain a fresh
whole-repo review, required gates and requirement assessment. Repo scope amendments use the newly `reviewed`
state and the applicable ordinary budget rules. Keep older states, reviews, author records and telemetry as written.

### Parallel batches (shared checkout, no worktrees)

A group with several members runs as one batch. Writers share the checkout, so correctness comes from a bounded
write window, not trust. The helper validates; the orchestrator runs the agents.
1. `batch-start --contract --repo --schedule --member A --member B --base MANIFEST --baseline BASELINE_DIR
   [--scope FILE] --out RUN_DIR/REPO/batches/batch-N-start.json`: members must be exactly one computed group,
   predecessors accepted, no slice started, no other batch open, previous batch not a violation, `base` a stable
   capture matching the checkout now. `capture` it immediately before (git repos capture every tracked and
   unignored file, so any stray edit is visible).
2. Dispatch all member implementers in ONE message (several `Agent` calls, `run_in_background: false`), each with
   its own slice inputs, EVIDENCE_DIR and the shared BASELINE_DIR. Each baselines a path before its first edit
   (markers are per path, scopes are disjoint, so this is safe). While writers run: no gates, review, git state
   change, formatter/autofix, or repo-wide certification; local results are provisional. The call returns only when
   every writer has returned, which is the barrier.
3. `batch-barrier --contract --repo --schedule --start ... --manifest NEW_CAPTURE --results RESULTS.json --out
   .../batch-N-barrier.json` (RESULTS.json = `[{slice_id, agent_id, status}]` for exactly every member, actual
   statuses). It attributes every changed path to exactly one member by write_scope, requires an unmoved git
   HEAD and a first-touch baseline for each changed path equal to the batch start state. Any unowned/multiply-owned
   path, HEAD move or baseline gap writes a `violation` record and raises: stop, report, a human decides; no
   automatic revert of anyone's work. A non-passed writer closes the batch `closed-incomplete`; that slice cannot
   proceed, and the others' work is kept.
4. Each member then starts `slice-transition ... implemented` with `batch_barrier_path`; `touched` must equal what
   the barrier attributed to it and `base_manifest` is the batch start manifest. Sibling files are not counted
   against it. Pre-review, slice review and acceptance then proceed per slice; independent slice reviews may be
   dispatched together because nothing writes during review. Fixes are a separate repair batch (same rules,
   members limited to non-overlapping scopes) or sequential.
Fallback is explicit: a group of one member, `max_parallel: 1`, a resource clash, an overlap, or an unestablished
barrier all mean sequential execution, and the report says which. Never run a second DB-backed check while
another uses the shared scratch DB (see "Shared scratch resources").

Each slice gets a fresh implementer. Record every slice author on implementation as
`slice_authors: {slice_id: agent_id}`; a slice-targeted repair request carries `slice_id`, and the repair phase
must name that slice's author. Changing an approved schedule needs a new human-approved file, a new contract
declaration and a recorded reason; requirements never change. Small scopes skip slicing entirely.

#### User-approved supplementary repo scope

For confirmed drift or a necessary tracked-file correction outside every slice, a user-approved, run-local
scope amendment can add exact tracked file paths at the whole-repo level without changing the approved slice
schedule or any existing contract or lifecycle artifact. This is not permission to alter requirements or to
edit a slice outside its schedule. Before editing, capture each added file with `baseline`; its recorded state
must equal the run's initial source. Write a unique amendment JSON with `schema_version`, `run_id`, `repo`,
`approved_by: "user"`, `user_authorization`, `approved_at`, `reason`, `contract_sha256`, `schedule_sha256`,
`previous_state_path`/`previous_state_sha256` (the current reviewed repo state), `author_agent_id` (the current
repo author), `before_manifest`/`before_manifest_sha256` (that state's source manifest), `slice_states` (the
slice-id-to-state-hash map of its slice gate), and unique exact tracked `paths`. Record the user's actual
approval; no wildcard paths, untracked files, pre-existing drift, or implicit scope extensions are allowed.

After the authorized edits, capture the full source, including frozen dependencies. `slice-gate` accepts
`--amendment AMENDMENT` and checks every slice remains accepted and at the approved version, all other source
remains unchanged from the reviewed anchor, the requested files have valid initial baselines, and the original
contract and schedule hashes match. The gate seals the amendment and reports its paths. Advance the repo from
`reviewed` to `edited-unverified` with `action: "plan-gap"`, `scope_amendment_path`, expanded `scope_path`, and
that new `slice_gate_path`; the ordinary repair-phase telemetry and review budget apply. This reopens the entire
repo, requires pre-review, a fresh independent whole-scope review, all gates and plan assessment; it does not
accept the amendment or mark a criterion verified by itself. Existing seals and earlier review/fix counters
remain intact. Later transitions inherit the exact same amendment and scope; replacing it or changing accepted
slice versions is not supported by this mechanism.

### Submission validation

Before dispatching a reviewer, run `python3 -I -B .agents/workflows/ship-fix/scripts/ship_state.py validate-submission --contract CONTRACT --repo REPO
--matrix MATRIX --manifest MANIFEST` as a standalone helper command. For slices also supply `--slice ID
--schedule SCHEDULE`. This read-only validation checks every supplied evidence path, including paths in
unverified rows, against the effective criterion and source manifest. An empty unverified row remains
unverified; validation does not certify implementation, review or acceptance. The same checks apply at
matrix validation during lifecycle transitions.
An approved requirement amendment may include paired `old_data_scope` and `new_data_scope` fields; evidence
is validated against the effective amended scope and retains the amendment hash binding.
`scope-check` compares the current tree to the initialized manifest, so unchanged pre-existing WIP is
preserved; new or modified changes outside the assigned scope remain violations.

Contracts may declare `read_excludes` as a repo-to-relative-path-list map. Captures must pass every path
as `--read-exclude`. Helpers use the contract automatically. Capture prunes the untracked walk at each
excluded boundary. It applies exclusion pathspecs to a private copy of the guarded Git index before Git
emits any tracked paths. An exclusion covers potential case and Unicode aliases unless distinct entries
can be established from their allowed parent directory. The capture hashes only allowed source files.
A touched or dependency scope intersecting an exclusion is refused, including overlap via a symlink target.
Discovered symlinks are recorded by link text and their targets are never followed during the source walk.

Restricted capture resolves Git directory, common directory, index and ref pointers under the read policy.
Git receives a private worktree and sanitized environment. The helper supplies guarded files or reftable
HEAD metadata and applicable worktree, repository, user and system ignore rules. It resolves conditional
config includes before opening their targets. Git itself never loads the live configuration. For a custom Git
build whose default system config path cannot be identified, supply a guarded `GIT_CONFIG_SYSTEM` or use
`GIT_CONFIG_NOSYSTEM=1`. Index and system-config paths from the environment are guarded. Other Git environment
overrides are stripped, and `GIT_DIR`, `GIT_COMMON_DIR` and `GIT_WORK_TREE` are refused.

Capture expands a sparse index by reading local tree objects for allowed directories. The guarded object
lookup uses only the requested loose trees or pinned pack descriptors, with no network fetch. A parent tree's
metadata identifies the boundary entry. Capture skips excluded child trees before requesting their objects.
Contract-bound captures project read exclusions from nested repositories into the parent repository and apply
the full contract policy to scope artifacts and metadata pointers. Git runs in a private directory under the
system temporary root, independent of `TMPDIR`.

The worktree walk and restricted metadata reads open path components through directory descriptors with
symlink following disabled, so replacing a checked path with a symlink cannot redirect the read. Capture
checks drift by comparing two snapshots. Read exclusions bind paths and their supported filesystem spellings.
Use an operating-system sandbox for inode-based restrictions against hard links, mounts or moving protected
objects into an allowed name.

Historical audits apply the exclusions of the audited contract and its callers to nested predecessors and
artifact reads. Scope amendments check the frozen slice write scopes and the added paths for exclusion overlap,
including symlink targets. Under a read policy, the amendment's tracked-path check uses literal Git pathspecs
against a temporary copy of guarded index metadata. Index layouts that require the live object database for the
query fail validation. Manifest format and historical seals are unchanged. `ship_checks.py` binds read
exclusions in its execution specification and applies them to both captures. An uninitialized scope-check
refuses read exclusions rather than running an unrestricted Git status scan. Existing `excludes` retains its
untracked-file meaning. Do not dispatch arbitrary commands that would read a forbidden path.

First-touch baseline lookup accepts another spelling of a source path when the filesystem identifies both
spellings as the same directory entry and the existing marker and backup remain valid. An unrecorded backup
or conflicting marker remains an error. Distinct directory entries retain separate baselines.

### Published deliverables and check inputs

Declare `deliverables` as a repo-to-list map. Each entry has a unique `id`, absolute `path`, and nonempty
`criterion_ids` linked to that repo. For staged delivery set `required_at` to `{"scope":"repo"}` for whole-repo
pre-review or `{"scope":"slice","slice_id":"cli"}` for a slice's pre-review. The slice must belong to the approved
schedule and contribute to at least one linked criterion. Omitted `required_at` preserves the existing slice
selection by shared criteria. Whole-repo pre-review and every later certification require all repo deliverables.

The approved plan or fix brief authorizes each external STORE. Freeze its named root in the contract:

```json
{
  "external_stores": {
    "results": {"path": "/data/workflow-results", "device": 16777220, "inode": 245899}
  },
  "deliverables": {
    "cli": [{
      "id": "result",
      "path": "/data/workflow-results/result.bin",
      "criterion_ids": ["result-verified"],
      "required_at": {"scope": "repo"},
      "store": "results"
    }]
  }
}
```

Use the actual values from the canonical directory's `os.stat()` fields `st_dev` and `st_ino` when freezing the
contract. Setup validates the approved schedule and opens the declared STORE roots. Roots must be disjoint from
one another, from repository trees, and from the run. STORE entries require exact canonical absolute paths
strictly beneath their named root. New staged deliverables also use canonical absolute paths. A STORE path must
use explicit external binding, including when declared as an input. A file without `store` uses the existing
run-local snapshot format.

Publish the files due for the selected scope, then run
`python3 -I -B .agents/workflows/ship-fix/scripts/ship_state.py snapshot-deliverables --contract CONTRACT --repo REPO --out-dir RUN_DIR/UNIQUE_DIR`.
Pass the immutable manifest as `--deliverable-manifest` to `validate-submission` and as
`deliverable_manifest_path` to the pre-review transition. Add `--slice ID --schedule SCHEDULE` to both helpers
for a slice. Staged manifests name the scope and enumerate pending IDs. A slice with only pending deliverables
can advance without a snapshot. Pending paths are omitted from file observations. A missing due file blocks
validation and progress. The slice gate verifies current accepted slice versions and the repo manifest must
agree with all slice versions.

For a stage with due staged-contract deliverables, include `"deliverable_binding":{"path":"ABSOLUTE_MANIFEST",
"sha256":"MANIFEST_SHA256"}` in independent pre-review and gate evidence as applicable, the review result and
the repo assessment. Proof for a linked criterion in the repo assessment uses the same binding. Evidence lists
the manifest in `artifact_paths`. Pass `--deliverable-manifest` to the declared check runners for pre-review,
along with the slice selectors for a slice. Final `gates` uses the manifest from current whole-repo review.
Implementer `run` accepts `--deliverable-manifest` for evidence about the selected version. A shared pre-review
check must be runnable on each slice and at the repo boundary. Put a check that consumes a later deliverable
in `required_gates`.

For an empirical `argv` check, list all inputs in `input_paths` and map each STORE input's absolute path to its
STORE name in `external_input_stores`, for example
`"input_paths":["/data/workflow-results/result.bin"],"external_input_stores":{"/data/workflow-results/result.bin":"results"}`.
Runners save small input snapshots as before. For STORE inputs, they save distinct run-local before and after
observations. The execution record and evidence reference both by path and hash in `input_bindings`. Both
observations must agree for valid evidence. A command can produce a real exit result even when its proof is
refused because inputs changed. At live evidence validation, the validators check the complete declared input
binding and reobserve each external input. Boundary observations establish versions around the command.
The declared command remains responsible for reading those paths and asserting the required behavior.

The helper streams STORE files in chunks of at most 1 MiB. The run-local manifest contains path, byte count,
SHA-256, device, inode, timestamps and directory identities. Deliverable rows also carry the deliverable ID.
Only the manifest enters the run's artifact seals. The helper opens each path component by descriptor, guards
it with the full `read_excludes` policy, and refuses traversal, symlinks, filesystem changes beneath the
declared root, non-regular files and files with multiple hard links. It checks root and file identity, size,
modification and change times, compares the directory chain, and reopens the path before accepting the hash.
Input observations also compare directory metadata across command execution. Unrelated writes in a shared
STORE directory can invalidate an in-progress observation. Use a stable publication directory and keep the
file and STORE available through completion. These checks rely on filesystem identities and timestamps.
Keep check logs limited to observations and assertions when STORE contents must remain external.
Use an OS sandbox if a command must be prevented from opening other paths or a privileged writer can move
protected objects into an allowed name.

A new file version requires a fresh manifest, affected pre-review checks, independent review, gates and
assessment. For an already accepted slice, use an approved integration checkpoint with
`action: dependency-recheck`, re-accept affected dependent slices, then produce a new slice gate. At a clean
pre-review or reviewed boundary before slice acceptance, `edited-unverified` with `action: binding-recheck`
requires a stale binding and the same source, scope and base. The same action can reopen a clean repo
pre-review, reviewed, gates-passed or verified boundary. It retains history and counters. Resolve open
findings through the usual repair lifecycle.

`digest` shows pending, byte-verified and stale deliverables and recorded external check input versions.
A new completion certificate records verified deliverable versions and external input bindings. Historical
audits read sealed artifacts and can succeed when the published file has changed since certification.
Live certification requires the published files. Earlier contracts, artifacts and certificates retain their
formats and seals.

For matrix repairs, `python3 -I -B .agents/workflows/ship-fix/scripts/ship_artifacts.py --task TASK_PATH matrix ROWS.json --base OLD_MATRIX` copies the
previous rows and applies explicit replacements. Each replacement names its old `replaces_evidence_paths`;
unmentioned rows remain unchanged. This is a mechanical update, not a correctness judgment.

### Review packet and results

`packet --contract ... --repo ... --scope ... --baseline ... --out UNIQUE_PACKET_DIR` batches stable
source/delta/index construction. Index contains changed-since-review paths, prior reviews/open findings,
criteria and mandatory passes. No transcript dumps. Reviewer still follows impacted unchanged consumers;
hash equality is not impact analysis. For revision-based --from-review, prepare scoped git diff and read
untracked files explicitly instead of the baseline packet until a true baseline is supplied.

Review output extends existing findings format with v2 binding, accepted:true only after stable review,
source_fingerprint and coverage. `coverage.read` contains plain relative paths; skipped entries are
`{path, reason}`; every scoped path covered. `mandatory_passes` entries contain id, status inspected/
not-applicable, reason, sites. Finding fields retain ID/file/line/severity/category/status/description/
recommendation/failure_scenario/verification_expectation/criterion_ids and add verification_kind.
Use all historical IDs/manifests/deltas: new IDs exceed historical maximum; recurring/regressed reuse ID.

Every finding omitted from the preceding review needs `dispositions: [{id, status: resolved|dismissed,
reason, evidence_paths}]`. Resolved uses original verification_kind proof; dismissed uses independent
static reasoning proof bound to final source. Dismissal is not silently accepting an open finding.
Pass prior applied claims/evidence to reviewer; weak behavioral `verified` text does not close it.
Immutable round outputs are authoritative; .claude/review-findings.json and review-applied.json remain
mutable compatibility copies and are excluded only when untracked/non-scoped.

### Gates

If a passed gate's evidence is misleading or invalid without a source change, rerun the independent
gate and use a `gates-passed` to `gates-passed` transition with the complete replacement
`gate_evidence_paths` and a `gate_correction_reason`. The reviewed source must remain unchanged; new
proofs undergo ordinary gate validation and the old state and evidence remain sealed in history. This
corrects evidence selection, not a failed check or code repair; never use it to waive a gate.

Declare resolved gates in the contract before implementation. Use the target repository's own documented
commands; this protocol does not prescribe a stack. Record each gate with a stable ID, `kind`,
`verification` and `command` or `argv`, and include documentation and changelog checks where the project
tracks them.

Example gate sets (replace with the project's actual commands):
- Node.js application: `npm run lint`, `npm run format:check`, `npx tsc --noEmit`; add `npm run build`
  when routes, server/client boundaries, build configuration or dependencies change.
- Go service: `go vet ./...` and `go test ./...`; declare any integration database and whether it must be
  idle before the run.
- Shared package: build the package before consumers build; run any declared sync step as a separate
  operation before consumer gates.
- Database migrations: never edit an applied migration when the project's migration runner
  checksum-guards it; inspect and follow the repository's migration convention.

Version bumps, tags, releases and production actions remain uncommitted preparations for the user unless
separately authorized. Missing required runner blocks; don't invent one.
Static inspection is not a replacement for missing empirical verification. Pin the actual runtime.

### Acceptance and completion

Assessment is bound JSON: producer=checker for ship, checker/orchestrator for fix; requirements_match=true
only after checking original requirements; criteria rows have criterion_id, implementation met/partial/
unmet, verification verified/unverified/failed, reason, evidence_paths. Checker is read-only and returns
JSON; orchestrator saves it. Before verified/completion capture final source after checker. Require every
mandatory criterion for every assigned repo. Fix assessment explicitly exercises original scenario.

Invoke `complete --contract ... --out RUN_DIR/completion.json` only after every repo verified. It
recaptures all repos and revalidates gates/acceptance/dependency versions after later changes. Each repo's
`review_budget` in the certificate records standard_limit, granted_additional_passes, effective_limit,
used_passes, extra_passes_used and sealed grant summaries (amount, reason, approval role/time and review
context), with granted and consumed passes shown separately. Each repo also records `gate_fix_budget` and
`gate_fix_history_sha256`. When a certificate exists, `validate-history` requires entries for all contract
repos and verifies the contract and verified-state seals for the selected repo. The certificate must name
the latest repo state it seals. The helper checks the gate-fix fields against the history at that state.
The certificate records open findings and their acceptance
decision for each repo and slice. Only this certificate permits scoped success; clean review
alone is not completed verification. Don't offer Proceed past a blocker. Multi-repo checkpoint requires human
Proceed; single-repo skips pause but prints status.
Archive only fully completed whole plans. A selected item's certificate does not verify other items.

### Author rotation (capsule), default for new runs

Long-lived implementer sessions re-send their whole history on every tool call, so a repair that resumes the
author costs its full context again. New contracts therefore declare `"author_rotation": "capsule"`
(`--no-rotation` in /ship or /fix writes `"resume"`). Absent or `resume` keeps the legacy behaviour unchanged.

- Before each spawn or resume the orchestrator runs `ship_artifacts.py init-task --contract C --repo R
  --touched TOUCHED_FILES_OUT --baseline BASELINE_DIR --evidence-dir EVIDENCE_DIR --out RUN_DIR/tasks/<unique>.json`
  with `--scope-file SCOPE` for a repo or `--slice ID --schedule slices-approved.json` for a slice; a review-fix
  adds `--findings ROUND_FINDINGS --round-dir DIR`. One task file per call; a repair gets a new file. The file
  sets `repo_path` from the contract and the slice's write_scope from the approved schedule, and refuses an
  `--out` outside the run directory or an existing file.
- The author writes a draft JSON with `task, decisions, invariants, files, pitfalls, verify, do_not_touch,
  open_items` (at most 16 KB) and checks it with `ship_artifacts.py --task TASK_PATH capsule-check DRAFT.json`,
  which writes nothing. The author reports the draft path. Binding fields such as `written_by` in the draft are
  refused.
- After the author returns, the orchestrator seals the capsule with `ship_artifacts.py --task TASK_PATH capsule
  DRAFT.json --agent-id ACTUAL_ID`, using the id from the agent dispatch result. The helper binds it to the recorded source
  (`source_fingerprint` over the same scope the helper uses: a slice's touched list, a repo's scope file plus
  dependency scope) and seals it with a manifest sidecar. Sealing requires `--agent-id`; no default author.
- A repair with a different author sends `capsule_handoff: {capsule_path, new_agent_id}` on the
  `edited-unverified` request (action `review-fix`, `gate-fix`, `plan-gap`, or slice `review-fix` /
  `integration-fix`). The helper accepts it only when: the capsule is run-local and bound to this run/repo
  (and slice), `written_by` equals the current author, its `source_fingerprint` equals the previous recorded
  state's fingerprint, its manifest matches, the capsule has not been consumed by an earlier handoff, and the
  new author differs from the current one. A repo-level handoff also needs `telemetry_policy.repair_phase_required`
  and no `from_review`. A capsule is single-use.
- Repo-level state records `current_author_agent_id` (set once an implementer exists; never accepted from the
  request). `original_agent_id` stays the implementer of record. On a handoff repair the phase-start names the
  predecessor (the current author) and the capsule path, both known before the spawn. The phase-end names the new
  author with the same capsule path. Without a handoff both events name the current author.
- Slice state records the new `author_agent_id` on the handoff; slice-gate uses the latest author.
- A same-author resume of a refused repair is still allowed; a handoff is never implied by a refusal.
- Provider quota or API errors: the orchestrator records phase-end `interrupted`/`api-error`, runs
  `ship_artifacts.py scope-check` and `since-capsule`, and dispatches a fresh author with the last capsule and
  that list. Nothing is edited by the orchestrator.
- Not supported in this version: handoff for `integration-fix` on repo-level state, continuation
  authorization, and integration-resume. These keep the existing resume behaviour.

The first implementation in a new run is done by a fresh implementer with a brief (plan excerpt and/or findings),
not a capsule: a capsule exists only to hand a repair to another author. That implementer still runs `begin-edit`
before every edit and ends with `capsule-check`; the orchestrator seals its capsule with the actual agent id.

Helper input is machine-checked; the capsule text itself is not proof. Verification still needs the usual
fresh review and gates after every repair, with budgets unchanged.

## 6. Telemetry and historical compatibility

Before/after each phase use `phase --contract ... --file INPUT --out RUN_DIR/telemetry/PHASE_ID-start.json`
(or -end.json). Input: run_id, workflow, repo (null for run-wide checker), phase_id, phase, event
phase-start/phase-end, at UTC. End adds status passed/failed/blocked/interrupted and, when known,
`end_reason` (done|context|api-error|user-stop|timeout|unknown) and an absolute `checkpoint_path` of the last
implementer checkpoint. The same helper accepts the boundary as flags (`--repo`, `--phase-id`, `--phase`,
`--event`, `--status`, `--end-reason`, `--agent-id`, `--round`, `--attempt`, `--capsule-path`, `--session-id`, `--at`); `at` defaults to now
and `session_id` is read from CODEX_SESSION_ID/CODEX_THREAD_ID. `digest --contract ... --repo ...` (or `--all-repos`) prints a
compact read-only status, and `ship_run_checks.py gates --contract ... --repo ... --scope ... --out-dir ...
--phase-id ...` runs required_gates with phase telemetry; batch a repo fan-out with `gates --all-repos` / `gates --repos a,b` plus `--phase-id-template {repo}-gates-1` and `--scope-dir DIR`.
`unknown` is legitimate; never infer a reason (e.g. context overflow) from duration alone.
Contracts with `telemetry_policy.run_local_seals: true` make `complete` reject sealing mutable documents outside
RUN_DIR and the assigned repos (stage-05's certificate sealed the live `stage-05.md`, so a later status edit
invalidated it). Contracts with `telemetry_policy.end_reason_required: true` (optional boolean) require end_reason on every
non-passed phase end. Include actual agent_id,
tool_use_id/message IDs, session_id, round/attempt when available; never guess. Start may precede knowing
agent_id; put it on end. Start hash binds end. Phases: setup, preflight, implement, pre-review, review, review-fix,
gate-fix, checker, gates, checkpoint-wait. The `setup` phase is written only by `validate-run` and does not
prove preflight or any check passed. Capture phases for resumed agents too, not just fresh spawns. Tool-level wait calls (waiting for an agent result, status checks) are not phases and emit no phase-start/phase-end events; only the workflow stages listed above emit phase events.

### Resumed repairs: record the actual envelope

For each new review-fix, gate-fix and acceptance/plan-gap repair dispatched to an author:
1. Choose a new unique phase_id across the run; write CLI inputs in `RUN_DIR/inputs/`, outside `telemetry/`.
2. Before host's continuation capability, record phase-start via the existing phase CLI/write_once, with actual run_id,
   workflow, repo, original agent_id, session_id when known, action and round/attempt. Review-fix and
   plan-gap use phase=review-fix (action=review-fix or plan-gap); gate-fix uses phase=gate-fix.
   Round is the preceding state's latest_review_round; gate attempt is preceding gate_fix_count + 1.
   Both events carry `previous_state_path` (absolute immediately preceding immutable state path) and
   `previous_state_sha256` (its SHA-256). This is a state binding, not a mutable status reference.
3. host's continuation capability to the original implementer instance, retaining actual message/tool_use_id correlation
   fields on the events when available. Wait for that specific resumed task's actual result.
   A delivery acknowledgement is not completion. Do not invent IDs or reinterpret a pending task.
4. Only after receiving the result, record phase-end with actual passed|failed|blocked|interrupted;
   the CLI binds start_sha256. Include the same identity/action/state/round-or-attempt fields, actual
   correlation IDs and result paths. A lost result leaves an open phase; never fabricate an end.
5. Read immutable result artifacts, then use normal proof/lifecycle/review/gate checks. Put the unique
   `repair_phase_id` in the current edited-unverified request, not only in an inherited earlier state.

For enabled policy, the helper requires a closed canonical PHASE_ID-start.json / PHASE_ID-end.json pair,
matching run/workflow/repo/action/original agent and immediately preceding state path/hash. It checks
UTC-aware timestamps (preceding state <= start <= end <= now), start hash and round/attempt, rejects
missing/open/stale/foreign/reused phases, and seals both events plus preceding state. Failed, blocked
and interrupted closures record observed boundaries, not successful proof; edited-unverified remains
unverified and all existing proof checks apply. Blocked/handoff snapshots may truthfully contain partial
edits, but do not authorize a new source version: policy-enabled proof progression must match the last
implemented/edited-unverified source (initial submitted source before a from-review repair). Record the
repair before re-entering review/gates/acceptance, even after terminal/block detours; restarting
implementation cannot reset this anchor. Unchanged block/unblock remains supported.
For the repo recovery transition after already accepted slice repairs, use the sealed slice evidence
defined in "Recovering a repo review fix after slice acceptance".
The guard does not mechanically prove host's continuation capability order.
If a fresh-author repair pair was already written but omitted the required capsule binding, never rewrite the
immutable phase records. A narrowly scoped, run-local `ship-phase-binding-recovery-v1` record may be used
only when the immediately preceding state is `needs-fix`, the current state is a `blocked` snapshot of that
same source/review context, the original phase pair is closed and hash-linked, and a sealed author capsule
binds the same source. The next `edited-unverified` request names this recovery record and the same capsule
handoff; validation seals all artifacts, charges the original repair pass, and resumes fresh review. This
mechanism repairs a missing telemetry binding only; it does not establish review or gate success.
No new dispatch wrapper or hook is introduced. With --from-review retain explicit author_handoff and
plan-gap instead of a fictitious implementer/repair pair; the orchestrator hands off and stops normally.

For future handoff evidence, copy the exact observed handoff/status bytes to a unique run-local snapshot
before sealing them; use that immutable snapshot's absolute path and hash in evidence, not solely the
mutable plan/status document. Never overwrite the snapshot when updating status after completion.
Do not rewrite old handoffs or certificates to repair historical hash mismatches.

Analyzer prefers run-scoped phase events and request/message-level usage, deduplicates replay/streaming
chunks, binds actual model by agent instance/phase. Missing input/output/cache metrics stay null; cumulative
agent totals aren't per-resume increments. Report elapsed, agent service, gate and user-wait separately,
plus gaps/overlap/accounting completeness. No cost estimate without real request-level USD attribution;
session USD and legacy session durations cannot be claimed as exact run cost/time. Separate review clean,
goals verified and run completed. Legacy records remain readable/uncertified, never retroactively completed.
No real-world speed/cost claim from synthetic tests. Avoid extra models/agents, severity-based early exits
or skipped Suggestions as an optimization.
