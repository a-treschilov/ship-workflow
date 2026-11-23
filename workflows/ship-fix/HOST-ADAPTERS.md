# Host capability adapters

The shared `ship` and `fix` skills define the workflow. This page maps abstract operations to the
interactive coding agent that is running them. The lifecycle, evidence requirements, budgets and artifact
formats are identical on every host.

## Common operations

Map these operations to the host's own capabilities:

| Workflow operation | Required behavior |
| --- | --- |
| Inspect and edit files | Use the host's workspace file tools, with the paths and write scopes supplied by the workflow. |
| Run a helper or declared check | Use the host's shell from the project root. Invoke each workflow helper as one standalone command exactly as specified by `PROTOCOL.md`. Record phase telemetry with the `ship_state.py phase` flag form and let `at`/`session_id` auto-fill; do not build phase JSON with file edits or query the clock. Pass the staged deliverable manifest to pre-review checks. Use `ship_run_checks.py gates` for final gates and `ship_state.py digest` for read-only status and version bindings. |
| Read or validate in parallel | Run independent reads and read-only validations together in one host exec cell; one cell is one request. Keep edits and other mutations as separate invocations. |
| Batch per-repo operations | When one operation is independent across repos, run it once for all of them: `ship_state.py digest --all-repos`, `ship_state.py record-evidence --batch FILE`, or `ship_run_checks.py gates --all-repos` / `--repos a,b`. A per-repo loop (one repo per cell) is forbidden; use the batch flag or one cell with `Promise.all`. |
| Check a repair budget | Before any repair task, phase telemetry or author dispatch, run `ship_state.py review-budget` for that repo. For gate-fix use `gate_fix_budget.remaining_attempts` and stop gate repair if zero. For exhausted review-fix or plan-gap, present the usage and relevant findings or criteria, then follow the user decision rules in the protocol. |
| Recover already accepted slice repairs | Follow the protocol procedure with `ship_state.py slice-gate` and `ship_state.py transition`. At exhaustion, collect explicit approval for a recovery-bound grant or stop at the current repo state. Continue with independent pre-review checks and fresh repo review. |
| Dispatch an agent | Select the matching role (`implementer`, `reviewer`, `plan-checker`, `slicer`); pass the full task handoff and record the actual agent ID and tool/session correlation IDs. Start every fresh subagent with only the explicitly passed package (PLAN_EXCERPT, CONTRACT_PATH, PROTOCOL_PATH, RUN_DIR, capsule/findings/manifests, and any other required inputs) and with no inherited parent-session history. Do not rely on inherited context or phrasing such as "as discussed above". If the host cannot spawn with fresh context, record the phase blocked or handed off under the general adapter rule instead of silently continuing with full history. |
| Continue an agent | Resume or steer the original author when the workflow requests it. A delivery acknowledgement is not completion. Record an actual result before recording phase-end. |
| Wait for an agent result | Make one blocking wait for the actual phase result using the longest timeout the host supports; when the host can wait for several agents at once, pass all active targets in that single wait. Do not poll in short loops (more often than once a minute without need). A timeout is neither failure nor completion: after a timeout one status check is allowed, not a new polling loop. Interrupt an agent or set end_reason only on actual grounds, never on the timeout alone. If the host's dispatch already blocks and returns the result, skip the separate wait step. A delivery acknowledgement is not completion — wait for the concrete phase result. |
| Ask the user | Use the host's interactive user-input mechanism when a workflow requires a decision or explicit approval. Never infer approval from silence. |

A new main (orchestrator) session is opened by the user; the workflow cannot start one with a tool. If the
host has no rotation mechanism, the run continues in the current session — this is a limitation, not a
blocker. On resume, the user confirms that the previous main session is closed.

If a host lacks a required operation, stop that phase and report it as blocked or handed off. Do not skip
independent review, user decisions, telemetry or evidence to make the run appear complete.

## Claude Code

- Custom agent role names map to `.claude/agents/<role>.md`.
- Generated `model` frontmatter uses `inherit` by default, or the Claude alias cached by `ocx_models.py` for a pinned role assignment
  in `.agents/agent-models.yaml`, including `[1m]` when OCX advertises it.
- Dispatch roles with Claude Code's agent tool; continue a running author with its messaging tool; collect
  required decisions through the user-question tool.
- Every agent dispatch starts the native agent with fresh context; do not forward any parent-session history.
- Native agent dispatch is blocking and returns the agent's result, so do not add polling around it. When continuing a running author through the messaging tool, wait once for that resumed task's actual result; a delivery acknowledgement is not completion.
- Run helpers from the project root at `.agents/workflows/ship-fix/scripts/<script>.py`.
- The trusted-command and worktree hooks load through `.claude/settings.json` and resolve to the shared
  implementation under `.agents/workflows/ship-fix/hooks/`.

- New main session: an ordinary new Claude Code chat/session; there is no dedicated rotation command. The
  old session is closed by the user (no programmatic check).

## Codex

- Custom agent role names map to `.codex/agents/<role>.toml`.
- Generated role files omit the `model` field for `inherit`; pinned assignments use the canonical ID in that field.
  The Codex session's configured provider routes the selected model.
- Use Codex's custom subagent dispatch and follow-up controls for role dispatch and continuation. Preserve
  the returned agent ID and session/tool correlation IDs in run telemetry.
- Every `spawn_agent` call must pass the role's `agent_type` and an explicit `fork_turns: "none"`.
- Do not omit `fork_turns`: omitting it inherits the parent session's mode (legacy/full history in cli 0.169).
- `fork_turns: "all"` is forbidden for ship/fix roles.
- A followup_task/resume of an existing author is not a new spawn; repairs use capsule rotation as before.
- If a host version rejects `"none"`, record blocked with the exact error; do not substitute the behavior.
- Wait for agent results with a single `wait_agent` call using `timeout_ms` (orient: implement/review/review-fix 600000 ms; gates/checker and known-long runs 900000 ms; never below 300000) and `targets` holding all active agents (returns on the first to finish). On `timed_out`: true, run one `list_agents` (or one repeated `wait_agent` for a legitimately long phase), never a new polling loop, and do not interrupt on the timeout alone.
- Run helpers from the project root at `.agents/workflows/ship-fix/scripts/<script>.py`.
- The trusted-command hook is configured in `.codex/hooks.json`; its small host adapter calls the same
  policy implementation as Claude Code.
- Record the actual host session id in every phase-start/phase-end: read `CODEX_SESSION_ID`, falling back
  to `CODEX_THREAD_ID`. When neither is set or the value fails `[A-Za-z0-9_-]+`, write an empty string;
  never guess.
- The `phase` helper auto-fills `session_id` from those environment variables (an explicit non-empty
  request `session_id` is preserved); a phase-end must match its phase-start's `session_id`.
- The project root is not a Git repository. Do not rely on discovering its root with `git rev-parse`;
  the active project root is the directory containing `.agents/workflows/ship-fix/`.

- New main session: an ordinary new Codex session/chat; there is no dedicated rotation command. The old
  session is closed by the user (no programmatic check).

Keep model selection, permission settings, hook registration and native agent-file syntax in platform
configuration. Keep role behavior and workflow requirements in the shared sources.
