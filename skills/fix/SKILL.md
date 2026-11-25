---
name: fix
description: Fix a bug or small improvement from a brief, with early environment checks, independent review and proof of the original scenario; no approved plan needed.
---

Read `.agents/workflows/ship-fix/HOST-ADAPTERS.md` to map workflow operations to this host's shell, agent-dispatch, continuation, and user-input capabilities. Stop and report unsupported capabilities instead of silently skipping workflow phases.


Fix the bug or improvement described in the user's prompt. If it is missing, ask for it and stop. `--repos a,b` selects repos explicitly.
**Orchestrator MUST:** issue every workflow helper as one direct shell invocation from the project root:
`python3 -I -B .agents/workflows/ship-fix/scripts/<script>.py ...`. No `cd`, assignments, command substitution, heredoc,
pipeline, redirection, shell chaining or unrelated command in the same shell invocation. Use the host's file-writing capability for JSON inputs;
pass explicit paths from run context. If cwd must change, do so with a separate standalone shell invocation first.
Repeat this exact constraint in every agent dispatch and resume prompt. Do not relax the trusted-tools hook.
Before run creation, replacement, contract freezing or any agent dispatch, perform the session capability check in HOST-ADAPTERS.md. Repeat it on resume before continuing. Stop with the capability, consequence and developer action if unsupported or unverified.
`--resume-run RUN_DIR` continues the same contract/history/counters after verifying brief and repo paths;
do not initialize a replacement run to hide blocked work or reset budgets.
Work from the project root. Before any agent dispatch, run
`python3 -I -B .agents/workflows/ship-fix/scripts/agent_models.py check` as a standalone shell invocation.
If it reports drift, stop; regenerate the native agent files and restart the configured host before continuing.
Read `.agents/workflows/ship-fix/PROTOCOL.md` fully first. Resolve it as absolute PROTOCOL_PATH and
`.agents/workflows/ship-fix/scripts/ship_state.py` as absolute HELPER_PATH for agents.
Follow the protocol's canonical helper invocation: `python3 -I -B .agents/workflows/ship-fix/scripts/<script>.py` from the
project root, without variable script paths or unrelated shell operations; include this rule in every
agent dispatch/resume. Shared protocol
owns lifecycle/evidence/gates/budgets/telemetry; this adapter owns brief extraction, locate and fix acceptance.
No worktrees, new agent types or parallel implementers.

## 1. Locate, scope and acceptance

Unless --repos supplied, locate relevant files/functions using local search. When a fresh locate agent
is actually needed, use Explore/general-purpose, pass the project constraints and ask for concrete code
sites, not repo guesses. Confirm located repos before editing; ask on uncertain matches, stop if no match.
No external code search requirement. Order repositories so shared packages are built and synced before their
consumers; if a shared package changes, run its declared build and sync as separate commands before processing
consumers (never version/tag/push — version edits stay uncommitted).

Before creating a run, inspect both `plans/.fix-runs/` and `plans/.ship-runs/` for related work, including
legacy descriptions. Resolve ambiguous matches with the user and continue an existing run where possible.
Give each repo a stable `budget_work_items` identifier in the contract and reuse it for related ship/fix runs;
follow the protocol for continuity and multi-history recovery before dispatch or completion.
Create exclusive RUN_DIR and valid run.json with the protocol's `init-run` helper, then write the v2 contract.
New contracts explicitly include `telemetry_policy: {"repair_phase_required": true}` and `"author_rotation": "capsule"` (`--no-rotation` writes `"resume"`).
There is no plan_path or archival. Extract a small acceptance brief, not a large plan:
- original scenario and observable expected behavior;
- constraints/non-goals and affected repos;
- mandatory criteria with real regression/acceptance assertions;
- environment, independent gates and pre-review/preflight checks per protocol.
Ask only when expected behavior is genuinely ambiguous; don't invent approval questions for obvious fixes.

Use the protocol's `required_at` for staged deliverables and approved STORE roots for external files.

Select acceptance mode reproduced-bug/improvement/unavailable-baseline. Reproduce a bug safely on initial
source when possible and record failure before edits; otherwise document why baseline unavailable.
Both before/after scenario evidence uses the original_scenario string; no fabricated red test.
A new feature/improvement need not fail before implementation. Capture true observed results and logs.

Before freezing scope, include impacted documentation and, for user-, consumer- or operator-visible changes, `CHANGELOG.md` (`## Unreleased`; skip repositories that do not track releases). Confirmed drift discovered outside authorized scope is a blocking gap: seek approval to expand scope rather than silently defer it. Validate each new run with the protocol's `validate-run` helper, initialize source/scope/baselines and execute preflight before implementer. Confirm
actual runtime source/mount, safe test DB, dependencies and permissions. An unavailable mandatory check
blocks before edits. Explicit user-authorized code-only work stays implemented/unverified, never success.
If a frozen setup-only contract needs replacement, follow the protocol's `replace-setup` procedure and carry
every actual predecessor's budget and history into the new contract.
Preserve unrelated dirty WIP; first-touch snapshots isolate same-file pre-existing edits.
Validate the submission with the protocol's `validate-submission` command before dispatching each reviewer.
Pass the selected manifest to pre-review checks, reviewer and assessment. Report pending and stale bindings
from the run digest and follow the protocol's recheck lifecycle.

## 2. Shared implementation / review / gates

Before spawning, issue `ship_artifacts.py init-task` as one direct shell invocation from the project root;
pass explicit paths and use the host's file-writing capability for any JSON inputs. Spawn original implementer with BUG_BRIEF,
CONTRACT_PATH, REPO/REPO_PATH, absolute PROTOCOL_PATH/HELPER_PATH, RUN_DIR, TOUCHED_FILES_OUT,
BASELINE_DIR, EVIDENCE_DIR and TASK_PATH. The spawn/resume prompt MUST repeat that each workflow helper
runs alone as `python3 -I -B .agents/workflows/ship-fix/scripts/<script>.py ...` from project root—never combine it with `cd`,
assignments, command substitution, heredoc, pipeline, redirection, shell chaining or unrelated commands.
Require completion matrix.

Dispatch implementer and fresh reviewer through the host adapter's agent capability with explicit inputs and acceptance criteria. Record the returned agent ID and correlation metadata. Platform configuration selects each model. Do not request worktree isolation: agents must share the source for repair and review.
Stop/report exact spawn error, never substitute a model or launch a nested agent runner. Save the original instance
ID as original_agent_id. Repairs follow the /ship rotation rule: by default a fresh implementer gets the last CAPSULE_PATH and a TASK_PATH from `init-task`, and the request carries `capsule_handoff`; the new capsule is sealed with `capsule --agent-id` after the return; under `resume` the original instance is resumed via the host's continuation capability. Never a context-less fixer.
Wrap all phases/resumes/checkpoints with protocol telemetry; actual IDs only, unknown usage is unknown.
Record actual original_agent_id on implemented. For each newly dispatched repair choose a new unique phase_id:
phase-start before the host's continuation capability to the original instance → wait for the specific actual result (delivery
acknowledgement is not completion) → phase-end with actual passed|failed|blocked|interrupted → normal
result/proof checks. Lost result leaves an open phase. Review-fix/gate-fix are separate; acceptance/plan-gap
uses phase=review-fix, action=plan-gap. Both events carry run/workflow/repo/action/original agent_id,
round/attempt, previous_state_path/previous_state_sha256 and actual session/message/tool_use_id correlation
fields when known, per protocol §6. Store CLI inputs outside telemetry/, and put repair_phase_id in the
current edited-unverified request; never reuse IDs. For capsule rotation both boundaries include `--capsule-path`;
use `--round` for review-fix/plan-gap and `--attempt` for gate-fix. For slice phases pass `--slice-id` on both
boundaries. The start names the current author; the end
names the observed fresh author. The --from-review author_handoff exception in the
shared protocol remains an author handoff, not an invented resumed implementer.
When a slice review-fix stops with partial changes, retain the actual unfinished-finding checkpoint and
bind a partial repair recovery record to the retry as specified in protocol §6.

Follow protocol transitions: implemented → independent pre-review → fresh review → original implementer
fix → fresh review → independent final gates. Prepare helper review packets and pass baseline scope,
contract/brief, current/prior manifests, all prior findings/applied results. Validate coverage, fingerprints,
proof and closed-finding dispositions. Review Suggestions too. Budgets and review numbering never reset.

Before preparing a review-fix, plan-gap or gate-fix repair, run the standalone read-only precheck from the project
root: `python3 -I -B .agents/workflows/ship-fix/scripts/ship_state.py review-budget --contract CONTRACT --repo REPO`.
For an identified repo successor, run `validate-history` on the predecessor before writing the contract. Include
its latest state and history SHA-256 in `budget_continuity` with matching `successor_scopes`. Follow the protocol
for recovery sources and per-slice decisions. Gate-fix requires a positive `gate_fix_budget.remaining_attempts`.
Do this before repair telemetry, `ship_artifacts.py init-task`, capsule/handoff preparation, or author dispatch;
show the reported limit, used passes, and open finding IDs. If already accepted integration repairs account
for the source drift, follow "Recovering a repo review fix after slice acceptance" in the protocol. Record one
repo review-fix and run fresh repo pre-review and independent whole-scope review. At exhaustion, use the
user-approved grant bound to the recovery in the same transition. Resolve other drift before preparing a repair.
Repeat the check before every new repair. After an exhausted-budget approval, apply the grant
against the exact prior state before any repair preparation, then recheck the budget before continuing.
For a slice review cap, pass `--slice SLICE` to `review-budget` and use the slice transition and decision
procedure in the protocol.

When the repo exhausts its standard 3 review-fix/plan-gap passes, do not start a replacement run and never
use gate-fix to repair review findings. Before dispatching more repair work, show the user the repo, used/limit,
all open findings (IDs, severity, concise failure scenarios) or plan-gap criteria, and bounded work remaining.
For review findings record `capped` before asking. Ask the user through the host's user-input capability: grant an explicit positive N additional passes, accept
all shown review findings, or stop. For acceptance follow the protocol's `reviewed-with-findings` transition
and retain every open finding in the final report. No automatic extension or
assumed consent. If approved, write a unique run-local grant JSON recording the exact question/answer, reason,
UTC time, `approved_by: "user"`, repo/run/workflow, purpose, count, exact preceding immutable state path/hash,
contract hash, and current review path/hash/round/finding IDs (or plan-gap criterion IDs). Apply it using a
separate no-source-change `transition` request with `budget_grant_path` before repair preparation or telemetry.
The helper validates and seals the grant. Ask again at each later exhaustion; grants accumulate (e.g. +1 then +1)
and each is bound to the immediately preceding state. Choosing stop leaves the run stopped/capped. A grant is not a
repair or a review round: normal author routing, proof, pre-review, fresh review, gates and acceptance all remain
required.

The completion certificate and final report state standard limit, each grant/count/reason, effective limit,
passes used and any passes above 3. Distinguish granted from consumed. Legacy runs without grants default to 3.
Report historical, current and cumulative gate-fix usage and remaining attempts.

Gate failure uses GATE_FAILURE/GATE_ATTEMPT_DIR and original implementer; read result, return every edit to
pre-review/fresh review/final gates, including previously green affected checks. Required unavailable checks
remain unverified. All final independent gates are executed by orchestrator, not claimed from agent checks.
For unsupported gates follow the runner's independent execution or inspection action and record evidence.
An `--ids` subset has its own exit status and does not pass the complete gates phase.
Multi-repo checkpoint requires human Proceed/Stop/Redo after a clean report; blockers never offer Proceed.
Single-repo skips pause, not validation. Keep scope confined to original brief and necessary regression fixes.

### Batch per-repo fan-outs

Issue each cross-repo-independent operation once, not once per repo: one exec cell with `Promise.all` or one batch flag. Use `ship_state.py digest --contract CONTRACT --all-repos`, `ship_state.py record-evidence --contract CONTRACT --batch FILE`, and `ship_run_checks.py gates --contract CONTRACT --all-repos` (or `--repos a,b --phase-id-template {repo}-gates-1 --scope-dir DIR`). Before validation ask: one repo or a fan-out? is there an `--all-repos`/`--repos` flag? can the reads merge into one cell? A one-repo-per-cell loop is forbidden.

## 3. Prove the original fix goal

Quality gates and a clean review do not prove the original bug is fixed. Mandatory final acceptance is a
structured contract→assertion/evidence assessment. For a small fix the orchestrator independently inspects
and records it, not the author. For complex/cross-repo goals use existing plan-checker with the fix
contract and original brief; no new verifier agent and no automatic checker call on every tiny fix.

Acceptance must exercise original scenario, not a proxy green test. For reproduced bugs validate the
historical before failure and actual after result; for improvements inspect agreed new behavior. Save
assessment JSON per protocol, capture source after assessment and transition verified. Goal gaps reopen
through original implementer and repeat review/gates/assessment within remaining budgets.

Invoke helper complete to revalidate all repos/dependency bindings. Only completion.json authorizes success.
Otherwise report implemented/unverified/blocked/capped/handoff faithfully, even if some checks are green.
For future handoff evidence seal unique run-local immutable snapshots of observed handoff/status bytes,
not solely mutable status documents; never overwrite snapshots or revise old runs/certificates.

Final summary: affected files, findings applied/rounds, independent gates, original scenario proof,
completion/blockers and accounting completeness. No plan archival. Never git add/commit/push, version bump,
release, production/external/destructive action without separate approval. Leave changes uncommitted.
List every open repo and slice finding with severity, location, scenario and acceptance status. Label a completed
run with open findings as completed with open findings.
