---
name: fix
description: Fix a bug or small improvement from a brief, with early environment checks, independent review and proof of the original scenario; no approved plan needed.
---

Read `.agents/workflows/ship-fix/HOST-ADAPTERS.md` to map workflow operations to this host's shell, agent-dispatch, continuation, and user-input capabilities. Stop and report unsupported capabilities instead of silently skipping workflow phases.


Fix the bug or improvement described in the user's prompt. If it is missing, ask for it and stop. `--repos a,b` selects repos explicitly.
**Orchestrator MUST:** issue every workflow helper as one direct shell invocation from the umbrella root:
`python3 -I -B .agents/workflows/ship-fix/scripts/<script>.py ...`. No `cd`, assignments, command substitution, heredoc,
pipeline, redirection, shell chaining or unrelated command in the same shell invocation. Use the host's file-writing capability for JSON inputs;
pass explicit paths from run context. If cwd must change, do so with a separate standalone shell invocation first.
Repeat this exact constraint in every agent dispatch and resume prompt. Do not relax the trusted-tools hook.
`--resume-run RUN_DIR` continues the same contract/history/counters after verifying brief and repo paths;
do not initialize a replacement run to hide blocked work or reset budgets.
Work from the umbrella root. Before any agent dispatch, run
`python3 -I -B .agents/workflows/ship-fix/scripts/agent_models.py check` as a standalone shell invocation.
If it reports drift, stop; regenerate the native agent files and restart the configured host before continuing.
Read `.agents/workflows/ship-fix/PROTOCOL.md` fully first. Resolve it as absolute PROTOCOL_PATH and
`.agents/workflows/ship-fix/scripts/ship_state.py` as absolute HELPER_PATH for agents.
Follow the protocol's canonical helper invocation: `python3 -I -B .agents/workflows/ship-fix/scripts/<script>.py` from the
umbrella root, without variable script paths or unrelated shell operations; include this rule in every
agent dispatch/resume. Shared protocol
owns lifecycle/evidence/gates/budgets/telemetry; this adapter owns brief extraction, locate and fix acceptance.
No worktrees, new agent types or parallel implementers.

## 1. Locate, scope and acceptance

Unless --repos supplied, locate relevant files/functions using local search. When a fresh locate agent
is actually needed, use Explore/general-purpose, pass the umbrella constraints and ask for concrete code
sites, not repo guesses. Confirm located repos before editing; ask on uncertain matches, stop if no match.
No external code search requirement. Order `shared` → `api` → {`landing`, `app`, `mobile`} when multiple
repos match; if `shared` changes, run `npm --prefix shared run build` + `npm --prefix shared run sync`
before processing consumers (never `npm version`/tag/push — version edits stay uncommitted).

Write exclusive RUN_DIR under plans/.fix-runs, run.json (description/session/start/repos) and v2 contract.
New contracts explicitly include `telemetry_policy: {"repair_phase_required": true}` and `"author_rotation": "capsule"` (`--no-rotation` writes `"resume"`).
There is no plan_path or archival. Extract a small acceptance brief, not a large plan:
- original scenario and observable expected behavior;
- constraints/non-goals and affected repos;
- mandatory criteria with real regression/acceptance assertions;
- environment, independent gates and pre-review/preflight checks per protocol.
Ask only when expected behavior is genuinely ambiguous; don't invent approval questions for obvious fixes.

Select acceptance mode reproduced-bug/improvement/unavailable-baseline. Reproduce a bug safely on initial
source when possible and record failure before edits; otherwise document why baseline unavailable.
Both before/after scenario evidence uses the original_scenario string; no fabricated red test.
A new feature/improvement need not fail before implementation. Capture true observed results and logs.

Before freezing scope, include impacted documentation and, for user-, consumer- or operator-visible changes, `CHANGELOG.md` (`## Unreleased`; infra excluded). Confirmed drift discovered outside authorized scope is a blocking gap: seek approval to expand scope rather than silently defer it. Validate contract, initialize source/scope/baselines and execute preflight before implementer. Confirm
actual runtime source/mount, safe test DB, dependencies and permissions. An unavailable mandatory check
blocks before edits. Explicit user-authorized code-only work stays implemented/unverified, never success.
Preserve unrelated dirty WIP; first-touch snapshots isolate same-file pre-existing edits.

## 2. Shared implementation / review / gates

Before spawning, issue `ship_artifacts.py init-task` as one direct shell invocation from the umbrella root;
pass explicit paths and use the host's file-writing capability for any JSON inputs. Spawn original crumb-implementer with BUG_BRIEF,
CONTRACT_PATH, REPO/REPO_PATH, absolute PROTOCOL_PATH/HELPER_PATH, RUN_DIR, TOUCHED_FILES_OUT,
BASELINE_DIR, EVIDENCE_DIR and TASK_PATH. The spawn/resume prompt MUST repeat that each workflow helper
runs alone as `python3 -I -B .agents/workflows/ship-fix/scripts/<script>.py ...` from umbrella root—never combine it with `cd`,
assignments, command substitution, heredoc, pipeline, redirection, shell chaining or unrelated commands.
Require completion matrix.

Dispatch crumb-implementer and fresh crumb-reviewer through the host adapter's agent capability with explicit inputs and acceptance criteria. Record the returned agent ID and correlation metadata. Platform configuration selects each model. Do not request worktree isolation: agents must share the source for repair and review.
Stop/report exact spawn error, never substitute a model or launch a nested agent runner. Save the original instance
ID as original_agent_id. Repairs follow the /ship rotation rule: by default a fresh crumb-implementer gets the last CAPSULE_PATH and a TASK_PATH from `init-task`, and the request carries `capsule_handoff`; the new capsule is sealed with `capsule --agent-id` after the return; under `resume` the original instance is resumed via the host's continuation capability. Never a context-less fixer.
Wrap all phases/resumes/checkpoints with protocol telemetry; actual IDs only, unknown usage is unknown.
Record actual original_agent_id on implemented. For each repair choose a new unique phase_id:
phase-start before the host's continuation capability to the original instance → wait for the specific actual result (delivery
acknowledgement is not completion) → phase-end with actual passed|failed|blocked|interrupted → normal
result/proof checks. Lost result leaves an open phase. Review-fix/gate-fix are separate; acceptance/plan-gap
uses phase=review-fix, action=plan-gap. Both events carry run/workflow/repo/action/original agent_id,
round/attempt, previous_state_path/previous_state_sha256 and actual session/message/tool_use_id correlation
fields when known, per protocol §6. Store CLI inputs outside telemetry/, and put repair_phase_id in the
current edited-unverified request; never reuse IDs. The --from-review author_handoff exception in the
shared protocol remains an author handoff, not an invented resumed implementer.

Follow protocol transitions: implemented → independent pre-review → fresh review → original implementer
fix → fresh review → independent final gates. Prepare helper review packets and pass baseline scope,
contract/brief, current/prior manifests, all prior findings/applied results. Validate coverage, fingerprints,
proof and closed-finding dispositions. Review Suggestions too. Budgets and review numbering never reset.

Before any review-fix/plan-gap repair preparation, run the standalone read-only precheck from the umbrella
root: `python3 -I -B .agents/workflows/ship-fix/scripts/ship_state.py review-budget --contract CONTRACT --repo REPO`.
Do this before repair telemetry, `ship_artifacts.py init-task`, capsule/handoff preparation, or author dispatch;
show the reported limit, used passes, and open finding IDs. If source does not match the latest state, stop and
resolve that drift. Repeat the check before every repair. After an exhausted-budget approval, apply the grant
against the exact prior state before any repair preparation, then recheck the budget before continuing.

When the repo exhausts its standard 3 review-fix/plan-gap passes, do not start a replacement run and never
use gate-fix to repair review findings. Before dispatching more repair work, show the user the repo, used/limit,
all open findings (IDs, severity, concise failure scenarios) or plan-gap criteria, and bounded work remaining.
Ask the user through the host's user-input capability: grant an explicit positive N additional passes or stop; no automatic extension or
assumed consent. If approved, write a unique run-local grant JSON recording the exact question/answer, reason,
UTC time, `approved_by: "user"`, repo/run/workflow, purpose, count, exact preceding immutable state path/hash,
contract hash, and current review path/hash/round/finding IDs (or plan-gap criterion IDs). Apply it using a
separate no-source-change `transition` request with `budget_grant_path` before repair preparation or telemetry.
The helper validates and seals the grant. Ask again at each later exhaustion; grants accumulate (e.g. +1 then +1)
and each is bound to the immediately preceding state. Declining leaves the run stopped/capped. A grant is not a
repair or a review round: normal author routing, proof, pre-review, fresh review, gates and acceptance all remain
required. Gate-fix remains limited to 2 attempts.

The completion certificate and final report state standard limit, each grant/count/reason, effective limit,
passes used and any passes above 3; distinguish granted from consumed. Legacy runs without grants default to 3.

Gate failure uses GATE_FAILURE/GATE_ATTEMPT_DIR and original implementer; read result, return every edit to
pre-review/fresh review/final gates, including previously green affected checks. Required unavailable checks
remain unverified. All final independent gates are executed by orchestrator, not claimed from agent checks.
Multi-repo checkpoint requires human Proceed/Stop/Redo after a clean report; blockers never offer Proceed.
Single-repo skips pause, not validation. Keep scope confined to original brief and necessary regression fixes.

### Batch per-repo fan-outs

Issue each cross-repo-independent operation once, not once per repo: one exec cell with `Promise.all` or one batch flag. Use `ship_state.py digest --contract CONTRACT --all-repos`, `ship_state.py record-evidence --contract CONTRACT --batch FILE`, and `ship_run_checks.py gates --contract CONTRACT --all-repos` (or `--repos a,b --phase-id-template {repo}-gates-1 --scope-dir DIR`). Before validation ask: one repo or a fan-out? is there an `--all-repos`/`--repos` flag? can the reads merge into one cell? A one-repo-per-cell loop is forbidden.

## 3. Prove the original fix goal

Quality gates and a clean review do not prove the original bug is fixed. Mandatory final acceptance is a
structured contract→assertion/evidence assessment. For a small fix the orchestrator independently inspects
and records it, not the author. For complex/cross-repo goals use existing crumb-plan-checker with the fix
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
