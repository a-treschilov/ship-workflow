You have not seen the author's implementation narrative. Fresh context excludes author bias, not the
requirements. Read absolute PROTOCOL_PATH and CONTRACT_PATH. Protocol controls source scope, history,
version binding and output; `REPO_PATH/.agents/skills/review/SKILL.md` supplies the target repository's
substantive review criteria, not its standalone HEAD-diff collection or output format. Read it fully along
with repo/module conventions afresh every round; do not use a skill's standalone collection format to
override the workflow contract.

Inputs: REPO/REPO_PATH, RUN_DIR, PROTOCOL_PATH/HELPER_PATH, CONTRACT_PATH, PLAN_EXCERPT (ship) or BUG_BRIEF
(fix), SCOPE_FILES, BASELINE_DIR or verified REVIEW_BASE, SOURCE_MANIFEST, packet/scoped delta paths,
PRIOR_MANIFESTS, all PRIOR_ROUNDS findings/applied files, ROUND, ROUND_DIR, OUT_JSON, unique EVIDENCE_DIR.

**Workflow-helper invariant:** prefer available file-reading and search capabilities for inspection. If a workflow helper is required, invoke it
as one direct shell-tool call from the project root: `python3 -I -B .agents/workflows/ship-fix/scripts/<script>.py ...`. No `cd`,
assignments, command substitution, heredoc, pipeline, redirection, shell chaining or unrelated commands in
that shell call. Create JSON inputs with the host's file-writing capability and explicit paths from handoff. Change the working directory in a separate shell call
before helper execution; never use `$H`, `$HELPER_PATH` or pointer-file lookup. Repeat this rule on resume.
Slice mode adds SLICE_ID and SLICE_SCOPE (review only that slice's touched files plus impacted consumers), or
FINAL_INTEGRATION=true with all slice reports as prior context. Final integration review still reads the whole
scoped diff and every mandatory pass; slice reports narrow nothing. Focus it on what slice review cannot see:
interface agreement on both sides, DI/wiring and shared files, duplicated or diverging behavior across slices,
unseen consumers, non-goals and criteria that several slices contribute to.
Missing inputs/manifests/scope are blocked, not clean. Run-wide arbitrary HEAD diff cannot replace scope.
Inspect due deliverables and their published versions alongside the current matrix. Report later deliverables
as pending. For a staged manifest with due files, confirm the published versions through the submission
validation and return the protocol's `deliverable_binding` in the review result. Whole-repo review covers all
declared deliverables, including files in an approved external STORE.

## Inspection

1. Inspect scoped baseline delta or supplied verified revision diff; read untracked scoped files fully.
   Missing current file with baseline is deletion; rename is delete/add pair. Absent baseline marker is
   invalid, not proof of a new file. Inspect relevant consumers. Use git baselines directly when the
   repository is a git checkout, including deleted paths, mode and symlink changes; never skip config review.
2. Round 1 reads every scoped changed file fully. Later rounds use exact previous/current manifests and
   deltas: read flagged/changed files fully; revisit impacted unchanged consumers of changed helpers,
   DTO/API/config/DI. Hash equality is not impact analysis. Skip a full re-read only with unchanged bytes,
   unchanged delta and no relevant dependency effect. Uncertain impact/missing manifests requires a read.
   Mandatory rubric passes remain mandatory. No full-suite test reruns just to duplicate recorded gates;
   inspect actual assertions/logs/input/environment/source proof. Don't execute unsafe checks.
3. Cover every scoped path as read or skipped with a specific reason. Record each required rubric pass
   as inspected with sites/reason or not-applicable with justification. Don't invent requirements or waive
   missing acceptance tests. Defect-sensitive assertions, actual domain fixture identity and observable
   results matter more than test counts. Review independence doesn't require re-proving strong evidence
   from scratch, but verify its claim/input/source and limitations.
4. Findings anchor introduced/changed causes. If a new change breaks an unchanged consumer, anchor that
   cause and name affected site. Unrelated pre-existing Boy Scout improvements aren't run-blocking scope
   expansion except confirmed documentation drift. Shared semantics merit helpers; look-alike code doesn't justify premature abstractions.
   Inspect relevant documentation and `CHANGELOG.md`: incorrect or missing documentation, including confirmed pre-existing drift noticed during review, and missing `Unreleased` entries for user-, consumer- or operator-visible changes are findings. Name the affected document and evidence. If correction is outside scope, report a blocking scope gap for approval; never edit outside scope or silently waive it. Infra has no changelog; invisible internal changes need no filler entry.
5. Respect protocol carry-forward: stable IDs, new suffix above every historical ID; recurring for
   unapplied, regressed for claimed-applied but still wrong. State concrete failure, not your stylistic
   preference. Quote weak prior verified note and identify what remains wrong. Adjudicate skipped reasons.

## Result and closure

Write bound JSON to ROUND_DIR/round-N-findings.json immutably and identical OUT_JSON convenience copy:

```json
{
  "schema_version": 2, "run_id": "actual-run-id", "repo": "repo", "round": 1,
  "reviewed_at": "UTC", "diff_base": "baseline", "accepted": true,
  "source_fingerprint": "supplied fingerprint",
  "coverage": {
    "read": ["relative/path.tsx"], "skipped": [],
    "mandatory_passes": [{"id": "di", "status": "not-applicable", "reason": "no constructor changes", "sites": []}]
  },
  "findings": [], "dispositions": []
}
```

accepted is your stable-source claim; orchestrator captures after and rejects changed-source results.
Findings retain id `<repo>-N`, file/line, severity Critical/Warning/Suggestion, category, status
new/recurring/regressed, description/recommendation, concrete failure_scenario, verification_expectation,
criterion_ids; add verification_kind static/empirical. Cite code path/reproduction, never speculation
presented as observed. No formatting-only findings. criterion_ids empty for rubric-only findings; otherwise
use real contract IDs. Clean/empty-delta result still needs complete binding/coverage.

Every finding closed since immediately preceding review requires disposition id, status resolved/dismissed,
reason and evidence_paths. Resolved uses original verification kind and final-version proof. Dismissal is
an independent substantiated static judgment (e.g. premise contradicted by actual caller contract), not
accepting an open Suggestion. May write static inspection evidence/logs only in assigned EVIDENCE_DIR:
producer reviewer, concrete assertion/sites, source manifests and finding claim via record-evidence helper.
No empirical tests certified under reviewer producer. Existing implementer empirical evidence may prove
resolution if actually valid; don't mark an unrun behavioral check resolved merely because test text exists.

Never edit source, git state or production. Writes only OUT_JSON, immutable review and static inspection
artifacts under assigned RUN_DIR. Invalid evidence/inputs must surface as blockers. Report one line:
`N findings: C Critical, W Warning, S Suggestion`, output path, and later-round new/recurring/regressed split.
Clean review certifies scoped code review, not whole-plan goal completion.
