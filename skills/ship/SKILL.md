---
name: ship
description: Drive an approved plan through preflight, implementation, independent review, gates and version-bound acceptance, leaving verified uncommitted diffs.
---

For each target repository, select the review rubric and freeze its stable IDs in the contract as described in
PROTOCOL.md. Resolve the selected rubric again for every reviewer and pass its contents in the handoff.

Read `.agents/workflows/ship-fix/HOST-ADAPTERS.md` to map workflow operations to this host's shell, agent-dispatch, continuation, and user-input capabilities. Stop and report unsupported capabilities instead of silently skipping workflow phases.


Run this workflow for the already-approved plan identified in the user's prompt. If no plan or plan item is identified, ask the user for it and stop.
**Orchestrator MUST:** issue every workflow helper as one direct shell invocation from the project root:
`python3 -I -B .agents/workflows/ship-fix/scripts/<script>.py ...`. No `cd`, assignments, command substitution, heredoc,
pipeline, redirection, shell chaining or unrelated command in the same shell invocation. Use the host's file-writing capability for JSON inputs;
pass explicit paths from run context. If cwd must change, do so with a separate standalone shell invocation first.
Repeat this exact constraint in every agent dispatch and resume prompt. Do not relax the trusted-tools hook.
Spawn with fresh context as established by the host adapter; do not rely on inherited parent history; pass every input by absolute path.
Before run creation, replacement, contract freezing or any agent dispatch, perform the session capability check in HOST-ADAPTERS.md. Repeat it on resume before continuing. Stop with the capability, consequence and developer action if unsupported or unverified.
Work from the project root. Before any agent dispatch, run
`python3 -I -B .agents/workflows/ship-fix/scripts/agent_models.py check` as a standalone shell invocation.
If it reports drift, stop; regenerate the native agent files and restart the configured host before continuing.
Read `.agents/workflows/ship-fix/PROTOCOL.md` fully before starting. Resolve it as absolute PROTOCOL_PATH
and `.agents/workflows/ship-fix/scripts/ship_state.py` as absolute HELPER_PATH for handoffs.
Follow the protocol's canonical helper invocation: `python3 -I -B .agents/workflows/ship-fix/scripts/<script>.py` from the
project root, without variable script paths or unrelated shell operations; include this rule in every
agent dispatch/resume.
The protocol is the single source for lifecycle, schemas, budgets, evidence, gates and telemetry.
This command adds ship-specific selection and archival, not a second implementation of that loop.

## 1. Select approved scope and prepare contract

Flags: `--repos a,b`, `--from-review`, `--no-rotation` (writes `author_rotation: resume`), `--base REVISION`, `--scope-files JSON`, `--baseline DIR`,
`--resume-run RUN_DIR` (continue that immutable contract/history and counters, never initialize anew).
On resume verify plan/scope/repo paths match; inspect latest state and prior blockers before acting.
For multi-repo --from-review use explicit per-repo base/scope/baseline maps; never assume one revision
belongs to different repos. Verify revisions with git at absolute repo paths.

Read the plan fully. If an item is named, select it. For "next"/"continue", select the single next Pending
work-item row top-to-bottom and announce it without a redundant question. Ask only when genuinely ambiguous
or no Pending work remains. Preserve the plan's status vocabulary, session boundaries and exclusions;
paths/contracts can establish repo scope without Repo tags.

Extract selected item IDs, exact requirements/acceptance, prerequisites, out-of-scope work and cross-repo
contracts. A --repos filter limits execution, not obligations; excluded mandatory work remains a gap unless
independently verified. Do not drop it merely to make a contract validate. Resolve these gaps before edits.
For cross-repo criteria whose proof requires later repos, use the protocol's `assessment_repo` only with
separate local criteria for every participant and final version-bound proof for the full criterion. Keep a
shared package's build and sync out of its pre-sync acceptance criteria: prove sync after acceptance and bind
the consumers' actual versions in the final cross-repo assessment. Do not alter existing sealed contracts.
Order repositories so each shared package is built and synced before its consumers, and declare that order in
the contract. Print scope/order and author handoffs.

Use the protocol's `required_at` for staged deliverables. Bind external files only under STORE roots approved
in the plan. Schedule checks that consume a late deliverable at the whole-repo gates.

Before creating a run, search existing `plans/.ship-runs/*/run.json` for the selected plan and item, inspect
the latest related state and report open work and budget usage. Continue the matching run where possible;
never reset its budget or treat a new directory as a fresh result.
Also search `plans/.fix-runs/` and inspect legacy descriptions for related work. Assign each repo a stable
`budget_work_items` identifier in the contract, reuse it for related ship/fix runs, and resolve ambiguous legacy
matches with the user. Follow the protocol for multi-history recovery before dispatch or completion.
Create exclusive RUN_DIR and valid run.json with the protocol's `init-run` helper, then write the immutable approved-plan snapshot,
per-repo excerpts and v2 contract.json per protocol. Excerpts include selected repo work plus cross-cutting
constraints/verification; referenced cross-repo sections are accessible in the snapshot. Store actual
session ID or empty string. repo_paths refer to existing assigned sibling repos; no new worktrees.
New contracts include `telemetry_policy: {"repair_phase_required": true, "end_reason_required": true, "run_local_seals": true}` explicitly, and `"author_rotation": "capsule"` (omit only with `--no-rotation`, which writes `"resume"`).
Declare empirical checks as `argv` lists where practical, and run each new pre-review/preflight command once
before implementation. On any non-passed phase-end record `end_reason` (use `unknown` when not established).
Validate each new run with the protocol's `validate-run` helper so setup success or failure is recorded before preflight. If a frozen setup-only contract cannot express the approved plan, follow the protocol's `replace-setup`
procedure and carry every actual predecessor's budget and history into the new contract. No approved requirement edits during execution. Approved plans tag repo
ownership via subsection headings under `## Changes` / `## Implementation` (e.g. `### 1. Backend
(\`server/\`) — …`); grep heading lines for a repo name (backtick-quoted directory or plain prose) to build
each repo's `PLAN_EXCERPT` = cross-cutting sections (Context, Architecture, Verification,
Rollback/Out-of-scope, locked contracts) + only that repo's subsections. If a heading doesn't
unambiguously name exactly one repo, include it in every relevant repo's excerpt rather than guessing it
out.
If a repo's scope is too large for one bounded dispatch (many subsystems, shared files, a long plan excerpt, or
earlier runs of the same stage were interrupted), spawn `slicer` (read-only; give REPO, REPO_PATH,
PLAN_PATH, CONTRACT_PATH, PROTOCOL_PATH, RUN_DIR) before edits, show its table and get an explicit human
decision with host's user-input capability. Only an approved schedule is used (protocol "Slice schedule"); never
slice without that approval. Then drive each slice (dependency order; a computed multi-member group runs as one parallel batch per protocol "Parallel batches", otherwise one at a time) through `slice-transition` — implement, pre-review, fresh slice review, fixes by that slice's author, accepted — then run `slice-gate`, and only then the normal repo lifecycle over the whole diff with a fresh final review (protocol "Slice lifecycle and final integration review"). Use a fresh `implementer` per slice,
passing the slice's write_scope, interfaces and predecessors' checkpoints, not the whole excerpt history.
At a slice review cap, use `review-budget --slice SLICE`, present every finding and offer the same three decisions
as for repo review. Record acceptance through `slice-transition` as described in the protocol.

Before freezing each repo's scope, include impacted documentation and, for user-, consumer- or operator-visible changes, `CHANGELOG.md` (`## Unreleased`; skip repositories that do not track releases). Confirmed drift discovered outside authorized scope is a blocking gap: seek approval to expand scope rather than silently defer it. Initialize each repo's source/baseline/touched scope before first edits. For --from-review verify the true
pre-implementation base; do not baseline already-written code as its own starting state. Preserve WIP.
Perform protocol preflight and record ready/preflight-blocked before implementation. Missing safe DB,
runtime mount, fixture, prerequisite or permission blocks. If the user explicitly permits code-only work,
record that boundary; don't later call it verified or archive. For performance plans also inspect the
representative profile: if target function is absent/cheap, resolve representative inputs/approved proof
instead of promising an end-to-end win from an irrelevant optimization.

If the project has a shared package that consumers build against, after the package repo passes
acceptance and before any consumer repo is processed, run its build and any declared sync step as
separate plain shell invocations so consumers see the fresh build. Never run a version bump, never create
or push a git tag; version and dependency-pin edits stay uncommitted prepared changes for the user to
release manually. The completion certificate covers code and local sync, not the release.

## 2. Execute shared lifecycle, sequentially per repo

Follow protocol §3–5 using helper transitions, not free-text success labels. No skipped completion check.
Invoke every helper in its own shell invocation using the canonical relative path from the project root; do not
wrap helper calls with shell setup, assignments, heredocs, pipelines or unrelated commands. Prepare JSON
inputs with the host's file-writing capability and pass explicit paths from this run context. When --from-review is absent, dispatch original
implementer with PLAN_PATH, PLAN_EXCERPT, CONTRACT_PATH, REPO/REPO_PATH, PROTOCOL_PATH, HELPER_PATH,
RUN_DIR, TOUCHED_FILES_OUT, BASELINE_DIR and EVIDENCE_DIR. Add the exact standalone-helper-call rule to
every spawn/resume prompt. Require its completion matrix. Record its instance ID as original_agent_id.
Each repair receives all open findings, effective amendments, the latest matrix and selected deliverable snapshot manifest. For repo review-fix, use review-fix-next below to select the author. For other repairs follow protocol "Author rotation". Before every spawn or resume run `ship_artifacts.py init-task` as its own helper call. After the author returns, seal its capsule with `ship_artifacts.py --task TASK_PATH capsule DRAFT.json --agent-id ACTUAL_ID`. Repeat the helper-call invariant in every message.

Dispatch implementer, reviewer and checker through the host adapter's agent capability with explicit absolute inputs and task context; record the returned agent ID and correlation metadata. Every spawn runs with fresh context using the confirmed host mechanism; all required context is passed as files and paths, not chat history. Host-specific agent configuration selects the model. Do not request worktree isolation: the project root is not necessarily a Git repository, and author continuation requires agents to share the original source. On dispatch failure, stop and report the exact error; never substitute a model or launch a nested agent runner.
Wrap every spawn/resume/gate/checkpoint wait with protocol phase telemetry. Write each boundary with the helper's flag form, for example `python3 -I -B .agents/workflows/ship-fix/scripts/ship_state.py phase --contract CONTRACT --repo REPO --phase-id ID --phase review --event phase-start --out RUN_DIR/telemetry/ID-start.json` and the matching `--event phase-end --status passed|failed|blocked|interrupted --end-reason ...`; omit `--at` (the helper uses now) and `--session-id` (it reads `CODEX_SESSION_ID`/`CODEX_THREAD_ID`). Do not build phase JSON with `apply_patch` and do not call a clock tool for `at`. Wait for the actual phase result using the host adapter's supported wait form and longest supported timeout; do not poll in short loops. Record actual agent/correlation
IDs on phase-end; unknown IDs stay unknown. Record actual original_agent_id on implemented.
For every newly dispatched repair use a new unique phase_id: phase-start before the host's continuation capability (or before the fresh spawn) to the current author →
wait for the specific actual result (delivery acknowledgement is not completion) → phase-end with actual
passed|failed|blocked|interrupted → normal result/proof checks. Lost result leaves an open phase.
Review-fix/gate-fix are separate; acceptance/plan-gap uses phase=review-fix, action=plan-gap. Both events
carry run/workflow/repo/action/original agent_id, round/attempt, previous_state_path/previous_state_sha256,
and actual session/message/tool_use_id correlation fields when known, per protocol §6. Pass the fields the
flag form accepts (`--action`, `--agent-id`, `--round` or `--attempt`, `--previous-state-path`,
`--previous-state-sha256`, `--capsule-path` for rotation, `--slice-id` for slice phases, `--checkpoint-path`) directly; use `--file` only for correlation fields the
flags do not cover, and store any CLI inputs outside telemetry/. Put repair_phase_id in the current
edited-unverified request. Never reuse IDs.
When a slice review-fix stops with partial changes, retain the actual unfinished-finding checkpoint and
bind a partial repair recovery record to the retry as specified in protocol §6.
For --from-review preserve author_handoff instead of fabricating an implementer or closed repair.

Validate the submission with the protocol's `validate-submission` command before dispatching each reviewer.
Pass the selected deliverable manifest to the pre-review checks and reviewer. Report pending deliverables at
slice handoff. Use `digest` to detect stale bindings on resume and follow the protocol's recheck lifecycle.
Run independent pre-review checks before the first expensive review. Run all final gates in one
`python3 -I -B .agents/workflows/ship-fix/scripts/ship_run_checks.py gates --contract CONTRACT --repo REPO --scope SCOPE --out-dir DIR --phase-id ID`
call; it writes phase telemetry and prints results per check. For unsupported checks follow the reported
independent execution or inspection action and record evidence. An `--ids` subset has its own exit status and
does not pass the complete gates phase. Prepare helper packet or revision scoped diff/untracked files.
Fresh reviewer gets scope, baselines/base, contract/excerpt, manifests/deltas
and all prior findings/applied files. Validate stable review and transition needs-fix/reviewed. Use review-fix-next for repo review fixes; read its immutable results before transition. Every edit requires pre-review,
fresh review and final gates again; counters never reset. Review Suggestions and perform final review.

After a `needs-fix` review, a user may explicitly defer an eligible Suggestion using the protocol's
`review_deferral_path` decision. Present the finding and linked criteria, obtain the user's reason and exact
authorization, and record a separate transition. Pass the decision paths and deferred IDs to the implementer
when repairing other findings. Report deferred IDs,
reasons and review rounds, including decisions that became stale after an edit or fresh review.

Before preparing a repo review-fix after needs-fix in an implementer-owned run, run
`python3 -I -B .agents/workflows/ship-fix/scripts/ship_state.py review-fix-next --contract CONTRACT --repo REPO --capsule-path CAPSULE_PATH`
as a standalone command. Pass the optional capsule path when available. Follow the returned action, role, inputs
and blocker as specified in the protocol; rerun after a budget grant. For other repair types use the budget check below.

Before preparing or dispatching a plan-gap, gate-fix or from-review author handoff, run the standalone read-only budget check
from the project root: `python3 -I -B .agents/workflows/ship-fix/scripts/ship_state.py review-budget --contract CONTRACT --repo REPO`.
For an identified repo successor, run `validate-history` on the predecessor before writing the contract. Include
its latest state and history SHA-256 in `budget_continuity` with matching `successor_scopes`. Follow the protocol
for recovery sources and per-slice decisions. For gate-fix, check `gate_fix_budget.remaining_attempts` and stop
gate repair if it is zero.
It must precede repair telemetry, `ship_artifacts.py init-task`, capsule/handoff preparation and author dispatch;
use its current repo/state result to show the limit, used passes and open finding IDs. If source differs from
the latest state because the slice authors already completed authorized integration repairs, follow
"Recovering a repo review fix after slice acceptance" in the protocol. Record the accepted work as one
repo review-fix and run fresh repo pre-review and whole-scope review. At exhaustion, use the user-approved
grant bound to the recovery in the same transition. Resolve other source drift before preparing a repair.
Repeat the check before each new repair; after exhaustion, apply any
approved grant to the exact previous state before preparing the repair, then check again.
When a repo reaches the standard review-fix cap (3), do not start a replacement run and do not route review
findings through gate-fix.
Before dispatching any more repair work, show the user the repo, passes used/limit,
open findings (IDs, severity and concise failure scenarios) or the plan-gap criteria, plus the bounded work
remaining. For review findings record `capped` before asking. Ask the user through the host's user-input capability whether to grant a specific positive number of additional review-fix passes
or accept all shown review findings or stop; no automatic extension or assumed yes. For acceptance, follow the
protocol's `reviewed-with-findings` transition and retain every open finding in the final report. On approval of
additional passes, write a unique run-local budget grant JSON with
`approved_by: "user"`, the exact answer/question, reason, UTC timestamp, repo/run/workflow, purpose, additional
passes, exact preceding state path/SHA-256, contract SHA-256 and current review path/hash/round/findings (or
plan-gap criterion IDs). Apply it with one `transition` carrying `budget_grant_path`, without source edits,
changing scope/author/review evidence, or starting a repair telemetry phase. The helper validates and seals it
before any author dispatch. Ask again after each new exhaustion; grants accumulate (for example +1, then +1)
and each binds to the immediately preceding state. If the user chooses stop, remain capped. A grant is not a review round;
continue through normal repair proof, pre-review, fresh review, gates and acceptance.

The completion certificate and final report must state standard limit, every granted amount and reason, effective
limit, passes used and any passes used above 3. Distinguish granted from consumed. Historical runs without grants
retain the default limit of 3. Report historical, current and cumulative gate-fix usage and remaining attempts.

With --from-review there is no implementer to resume: hand findings/gate failures to the author's coding
session through the host's author-handoff mechanism, record the handoff and stop, not a fabricated clean round. Continue
against verified scope/base and preserve counters/history; don't disguise caps by starting over.

Run all independent final gates and plan-specific acceptance checks per contract. Record actual commands,
exit statuses, sanitized logs and final-version evidence. Gate-fix uses original implementer and protocol
GATE_FAILURE/GATE_ATTEMPT_DIR, not a fake findings round. Always return edits to fresh review and rerun
previously green affected checks. Unavailable empirical checks stay unverified.

Checkpoint: scoped baseline/revision delta, findings applied, rounds/counters and review/gate/acceptance
status. Label any whole-HEAD stat as possibly unrelated WIP. Multi-repo runs require human Proceed / Stop /
Redo; don't offer Proceed past blocked/capped/unverified. Single-repo skips pause, not the checks/report.

### Orchestrator rotation

Rotate the main (orchestrator) session only at clean boundaries: after a repo transitions to `verified`
(before the next repo), after a slice-gate/barrier batch of slices, at the user's request, or before
starting a new large segment after a long pause. Never rotate mid-phase, during review-fix, or while any
agent is running. Before rotating, confirm every phase is closed (no phase-start without a phase-end) and
print a compact handoff block to the user: RUN_DIR, contract.json, the path and SHA-256 of the latest state
for each repo, the next action, open findings/blockers, user decisions still awaited, and the list of active
agents (must be empty). Ask the user to close the current session and start a new one with
`/ship --resume-run <RUN_DIR>` and the same plan item or "continue".

On resume: verify plan/scope/paths match, run `ship_state.py digest --contract CONTRACT --repo REPO` per repo
(add `--verbose` for detail) instead of separate contract/history/budget reads, read the latest state and
blockers, and do not restart completed gates/reviews; preserve counters. Explicitly confirm
that the old session is closed and no second orchestrator is working on RUN_DIR; if the old agent is still
alive, wait for its result and record it rather than launching a duplicate with a new phase_id. In phase
events when continuing, record the new host's actual session_id when known (PROTOCOL requires "Record each
phase session_id when continuing in another session"); if the host does not expose it, write an empty string
without guessing.

Rationale: a long-lived main session re-sends its whole history on every request; rotating on sealed
boundaries caps its context.

### Batch per-repo fan-outs

Issue each cross-repo-independent operation once, not once per repo: one exec cell with `Promise.all` or one batch flag. Use `ship_state.py digest --contract CONTRACT --all-repos`, `ship_state.py record-evidence --contract CONTRACT --batch FILE`, and `ship_run_checks.py gates --contract CONTRACT --all-repos` (or `--repos a,b --phase-id-template {repo}-gates-1 --scope-dir DIR`). Before validation ask: one repo or a fan-out? is there an `--all-repos`/`--repos` flag? can the reads merge into one cell? A one-repo-per-cell loop is forbidden.

## 3. Independent goal assessment and completion

Spawn existing plan-checker with PLAN_PATH/approved snapshot, CONTRACT_PATH, absolute protocol/helper,
scoped deltas/bases, all repo paths, manifests and EVIDENCE_PATHS. No author's persuasive narrative.
Include the whole-repo deliverable manifest and external input bindings with the acceptance evidence.
Save its structured criterion assessment immutably per repo. Missing/partial/unmet/failed/unverified is a
blocker. Reopen only through original implementer (or author session), charge the protocol repair budget,
repeat affected review/gates/checker; no budget reset.

Capture source after checker; transition each repo verified. Invoke helper complete after all repos to
recheck earlier repos/dependencies and produce completion.json. A clean review is not sufficient. Print
only scoped success when mandatory selected items are met and verified; other master-plan work remains.

## 4. Update execution status / archive

Only after valid completion.json update selected execution statuses and evidence/handoff links, not
approved requirements. For future evidence seal a unique run-local immutable handoff/status snapshot,
not solely a mutable document updated after completion (`run_local_seals` makes `complete` reject a certificate
that seals any document outside RUN_DIR/the repos, e.g. the live stage status file); leave historical handoffs/certificates unchanged.
If any other mandatory item is pending/partial/implemented-unverified, leave master
plan in place. Archive only when all mandatory whole-plan requirements independently met and verified.

Read existing completion records and destination first. Preserve relative link targets/fragments by
rebasing against plans/done, preserve nested organization, stop on conflicting destination, no redirect
placeholder. Update actual referring indexes only. Never commit archival or code changes.

Final summary: per-repo code scope, review rounds/fixes, independent gates, acceptance/evidence/blockers,
completion certificate and archive status. Report telemetry completeness; don't promise measured savings
from missing or synthetic usage data. Leave reviewed uncommitted diffs for user inspection.
List every open repo and slice finding with severity, location, scenario and acceptance status. Label a completed
run with open findings as completed with open findings.
