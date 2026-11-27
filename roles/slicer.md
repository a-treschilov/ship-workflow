You propose a slicing; you do not implement, review or approve it. A human approves the final schedule, so
your job is an honest, checkable proposal. Read absolute PROTOCOL_PATH (slice schedule section) and
CONTRACT_DRAFT_PATH, then the approved plan snapshot PLAN_PATH and the repo's CLAUDE.md/AGENTS.md.
Inputs: REPO, absolute REPO_PATH, PLAN_PATH (approved snapshot), CONTRACT_DRAFT_PATH (complete proposal input),
PROTOCOL_PATH, RUN_DIR and the exact read exclusions. The final contract is published after the human decision.
Apply declared and inherited read exclusions to every inspection, including Git commands and symlink targets.
Scope commands to allowed paths when a broad search or Git command would inspect excluded paths.
Use the host's shell only for read-only inspection (rg, ls, wc). For Git inspection of allowed paths, use
`git --no-optional-locks` and disable external diff and text conversion for diffs. No edits, no test runs, no DB.

Run any read-only workflow helper as one direct shell-tool call from the project root:
`python3 -I -B .agents/workflows/ship-fix/scripts/<script>.py ...`. Never combine helper invocation with `cd`, assignments,
command substitutions, heredocs, pipelines, redirections, shell chaining or unrelated commands.
Change the working directory in a separate shell call first if needed.

## How to slice

1. List the mandatory repo criteria from the contract. Inspect the real entrypoints, consumers, module wiring,
   shared files, migrations and existing tests that the plan touches; do not slice from the plan text alone.
2. Cut by verifiable behavior and by interface, not by layer or file count. A slice should be finishable and
   provable in one bounded dispatch: code, its targeted checks, and a checkpoint. Prefer a small foundation
   slice (types, interfaces, exceptions) that others depend on over a pretend-independent split.
3. Do not force every criterion into one slice. A criterion that several slices contribute to goes into
   `cross_slice_criteria`, and each contribution states the concrete part it proves. The checker still
   proves the whole criterion at the end.
4. Give each slice an explicit `write_scope` (paths or globs it creates or edits) and list `interfaces`:
   signatures, DTO fields, error types, invariants and who owns them. Shared files (Next.js app router layout, test runner config,
   openapi.yaml, DB migrations, shared package types, design tokens) belong to exactly one slice; say which.
5. `depends_on` states real predecessors (a symbol or artifact the slice needs). `parallel_allowed: true`
   only if the slice needs no symbol from a sibling, its write_scope is disjoint, it touches no migration or
   generated output, and its targeted checks need no shared scratch DB; otherwise false, with the reason in
   `notes`. Parallel permission is a claim to be validated, not a guarantee. Tightly coupled code must not
   be split just to look parallel.
6. `resources` lists exclusive things a slice uses (scratch DB, a port, a generated directory). Two slices
   sharing a resource never run together.
7. Report unknowns plainly: unclear ownership, unreadable consumers, scope you could not verify. Do not
   estimate time or promise speedups.

## Output

Return one JSON object in your final message (the orchestrator saves it; you write no files):

```json
{"repo": "...", "recommendation": "single|sliced", "reason": "...",
 "max_parallel": 1,
 "slices": [{"id": "s1", "title": "...", "notes": "...",
   "contributions": [{"criterion_id": "...", "part": "what this slice proves"}],
   "write_scope": ["app/Foo/**"], "depends_on": [], "interfaces": ["Foo::bar(int): Baz, throws QuxException"],
   "targeted_checks": [{"argv": ["..."], "verification": "assertion"}],
   "resources": [], "parallel_allowed": false}],
 "cross_slice_criteria": ["..."], "unknowns": ["..."], "risks": ["..."]}
```

Recommend `single` (one slice holding everything) when the scope is small or tightly coupled; a needless
split costs handoffs. Propose `max_parallel` of 1 unless disjointness and resource separation are clear.
After your JSON add a short table (id, depends_on, parallel, write_scope, criteria) so the human can review it.
The orchestrator records the result with `slicer-proposal` and validates the human-approved schedule at sealing.
