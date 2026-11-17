---
name: ship-analyze
description: Read-only post-mortem of a ship or fix run: goal verification, review churn and run-scoped phase time/token accounting, with explicit missing-data limits.
---

# ship-analyze

Analyze actual run artifacts, not impressions about model price or number of rounds.

```bash
python3 -I -B .agents/skills/ship-analyze/analyze.py --run latest --format compact
python3 -I -B .agents/skills/ship-analyze/analyze.py --workflow fix --run latest --format compact
```

Run commands from the project root, or use the absolute script path when working elsewhere. Use
`--workspace /absolute/project/path` when using an analyzer installed in another project or the package source.
Exact run names take precedence; other run prefixes must identify exactly one run. Missing and ambiguous prefixes are errors.
Set `SHIP_ANALYZE_TRANSCRIPTS` to a directory of compatible session transcripts when they are stored outside
the default Claude Code project directory; transcript accounting is optional when only run artifacts exist.
--run accepts run-ID prefix/latest; --workflow ship/fix selects namespace; --session overrides transcript
session for analysis only; --no-cost skips legacy CCR query.
`--format compact` renders one human-readable phase table and diagnostics; default output and `--json`
remain detailed JSON. Do not truncate JSON with head: parse the fields needed, or use compact output.
Strictly read-only; sqlite uses immutable mode. Do not open raw transcripts directly in the model context;
the script streams them and emits compact metrics. Do not rewrite history to improve a report.

## v2 runs

Contract and append-only telemetry identify exact run, repo, phase, agent instance and start/end boundaries.
Phases include setup validation, resumed review-fix/gate-fix, gates and checkpoint waits. Setup success only
validates run metadata and contract; it does not prove preflight or project checks passed. Script joins actual agent transcripts
by agent_id/tool_use_id, deduplicates request/message chunks/replays and reads usage per phase, not cumulative
agent totals. Actual model is per phase/instance; mixed phases expose a models list. Missing metrics are
null, not zero. Reports without reliable correlation/completion are partial, not free work.

Render a compact phase table: phase/repo/round, actual models, input/output/cache usage, time, status and
accounting completeness. Distinguish elapsed wall time, summed agent/service time, gate time, user waits,
overlap and unclassified gaps; don't add overlapping durations as wall time. `accounting_complete: false`
means no exact total cost or bottleneck percentage claim. Even complete LLM phase accounting doesn't include
unclassified orchestration requests; name the scope of accounting, not the whole API bill.

Accuracy separates clean review, verified goals and completion certificate. A checker says met without
empirical proof isn't success. Missing/stale/failed criteria or edited-unverified/capped/blocked/handoff
remain blockers. Completion certificate has hashed artifact references; an altered certificate/artifact
is reported invalid. A historical completed certificate describes the recorded state, not a new live
certification of subsequently edited code.

ocx token join: `telemetry.ocx` joins transcript `requestId` to `~/.opencodex/usage.jsonl` exactly (never by time
alone, since parallel sessions share the log). Per-phase `ocx` = tokens with cache read/write. Agent `in_run`
and `transcript_wide_total` have different scopes; `outside_recorded_phases` exposes in-window uncovered
requests, not proven fix-only work. `uncorrelated_agents_in_window` = same-session agents without
run identity evidence, excluded. `other_sessions_in_window` = proven parallel sessions; `unattributed_in_window` = in no transcript, excluded from totals (shared conversationId is a hint,
not proof). `vs_transcript` flags transcript/ocx disagreement. A complete, unambiguous exact OCX join
sets the canonical phase usage and actual models; otherwise a complete transcript fallback is labelled.
If neither is complete, usage stays partial/unknown. Input means fresh input, separate from cache read/write.
Missing counters/log rows are not zeros; known subtotals are not complete totals. Tokens only.

codex token join: for Codex-hosted runs, `telemetry.ocx` uses the "codex thread/agent exact join
(agent_path + parent_thread_id)". Transcripts are read lazily from `~/.codex/sessions`,
`~/.codex-personal/sessions` and `~/.codex-personal/archived_sessions` (override with
`SHIP_ANALYZE_CODEX_TRANSCRIPTS`, a colon-separated list of flat directories). Every non-subagent rollout with `cwd == workspace` in the run window is a root candidate. Declared
session ids from run.json `session_id`/`session_ids` plus every non-empty phase `session_id` identify
declared roots; those seed orchestrator aggregation but never restrict phase search. A phase with a
`session_id` binds to the root whose `thread_id`/`session_id` equals it; when no root matches, it falls
back to all roots with a note, and multiple matches stay ambiguous. A phase without a `session_id` binds
by a unique `agent_id == agent_path` plus `parent_thread_id` across all roots; multiple matches stay
null/ambiguous (never chosen by time), and the `tool_use_id` fallback also searches all roots. The
orchestrator aggregates usage over every root participating in the run (declared roots plus roots with a
bound phase), exposing a per-root `roots` list; tokens and requests are summed, models are unioned, and
usage is complete only when every root's usage is valid. Several models within one root are normal and
are listed in `models`; they do not by themselves make usage incomplete.
Per-request usage comes from `event_msg.token_count` `last_token_usage`, not `token_usage_record`: one
cumulative-advancing token_count is one request; duplicate/plateau re-emissions are skipped and a decreasing
cumulative starts a new segment. A legacy/fork thread (history_mode=legacy or forked_from_id) replays its
parent's history, so its fork baseline is the parent's cumulative total at the child's session_meta time;
replayed requests at or below that baseline are excluded from both usage and coverage, and the per-request
total is cross-checked against the cumulative delta (a mismatch sets `usage_complete=false`). Fresh input =
max(0, input_tokens - cached_input_tokens), cache read = cached_input_tokens, cache write =
cache_write_input_tokens, output = output_tokens; missing/invalid fields are null, not zero. Actual models
come from the last turn_context at or before each real request, so inherited turn contexts do not create a
false multi-model phase. Phase usage sums the real requests inside `[start_at, end_at]`; a thread serving
several phases assigns each request to at most one, overlapping windows are ambiguous, and a phase with no
request stays incomplete with a reason. Orchestrator and coverage are window-scoped over real (post-baseline)
requests. Tokens only, no USD.

Event validity, correlation, usage completeness, request coverage and artifact integrity are independent.
Read their reasons, not just a single success flag. Current seal validity qualifies certification;
recorded historical outcomes remain separately visible. Agent run-window and transcript-wide totals
have different scope. Uncovered requests are not automatically review-fix work; ambiguous overlaps
cannot be assigned by traversal order. Phase durations are dispatch-to-result envelopes, not pure model time.

No request-level USD source is currently available through ocx. Keep cost unavailable rather than invent
prices or time-join unrelated usage. Session-wide legacy USD is not certified cost of one run.
When Codex provides a context window and input count for a correlated orchestrator session, its root entry
includes the latest timestamped context snapshot in the observation window. This is not cumulative token usage.

## Legacy runs

Existing run history and session summaries remain readable. `converged` describes legacy empty final
findings only; `legacy/uncertified` is not retroactive completion. Missing findings is not empty findings.
Versioned review acceptance/latest source state further constrain convergence when available.

Legacy token/duration totals are partial session aggregates: may include other runs, omit resumed fixes,
and report cumulative counters. Actual model attribution may be agent-type aggregate, not phase-certified.
Say these limitations before using figures. Timestamp range is session range, not proven active run time.
Missing gates/outcomes aren't passing gates. ship_run=false means no executed ship evidence, not an error.

## Interpret with quality first

1. Goal gaps/environment blockers before tuning speed. Isolated DB unavailable is a concrete blocker,
   not automatically a vague skip. Keep implementation, independent review and verification distinct.
2. Recurring/regressed findings: inspect original claim, fix and proof; don't assume reviewer churn merely
   from counts. New rounds may catch real assertion defects.
3. Optimize demonstrated repeated work only with accounting scope/completeness stated. Safe batching:
   coherent edits, shared targeted runs with distinct claim assertions, deterministic review packets.
4. Never recommend severity-based early exit, skipped Suggestions, weaker assertions or fewer independent
   checks. Model/budget changes require evidence and user approval; this skill never edits workflow files.

For v2, `insights.open_findings` lists the latest open review findings by repo and slice, including recorded
user acceptance. The compact report lists those findings and counts by severity.

Inspect `insights` for reviewer claims, explicit finding-ID dispositions and applied records,
separated by repo/slice. The compact output summarizes repo findings; JSON carries slice details. Each
artifact's seal label is independent: an unsealed statement or matching hash with an unverified
certificate is not certified evidence. Duplicate IDs in a scope are ambiguous and never automatically
joined. Proof inspection separately reports identity, explicit claim ID, record status, embedded hashes,
and whether validated before/after manifests match the recorded completion fingerprint and repo path;
matching metadata is not independent proof of the assertion or current code. Handoff release statements
are attributed to the run-local document, with seal and timing explicitly qualified; they are not current
release certification. Findings and fixes are recorded claims, not a fresh code review;
missing links are unknown, not closure. Observations offer questions and actions only for detected evidence;
verify their context before recommending process changes. Do not treat counts as proof of churn.

End with a short actionable recommendation and explicit unavailable measurements. No promise of savings
from fixture tests or an incomplete report. Unknown is a legitimate result.

## Per-agent and per-task accounting

`agents.py --subagents DIR [--ocx PATH] [--agent-type implementer] [--json OUT]` splits each agent
transcript at real prompts (the first prompt and each later coordinator message) and reports tasks with request
counts, exact OCX tokens joined by `requestId`, input growth (first to last request), output tokens and tool calls
by category with result sizes in characters. A request without an exact OCX row is counted as unmatched and its
tokens stay null. The tool reads transcripts by streaming and prints a table and one JSON line per agent.
