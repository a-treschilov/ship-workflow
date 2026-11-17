#!/usr/bin/env python3
"""Extract a per-phase cost and accuracy summary for one /ship run.

Reads Claude Code session transcripts, the run history written by /ship
(plans/.ship-runs/<run-id>/), and — when available — the Claude Code Router
cost database. Emits JSON or a compact evidence report; never prints transcript content.

Strictly read-only. Opens every sqlite database in immutable mode.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[3]
DEFAULT_PROJECT_DIR = Path.home() / ".claude/projects" / str(WORKSPACE).replace("/", "-")
PROJECT_DIR = Path(os.environ.get("SHIP_ANALYZE_TRANSCRIPTS", DEFAULT_PROJECT_DIR)).expanduser()
RUNS_DIR = WORKSPACE / "plans/.ship-runs"
CCR_DB = Path.home() / ".claude-code-router/app-data/request-logs.sqlite"

SHIP_AGENTS = ("implementer", "reviewer", "plan-checker")


def known_repos():
    """Best-effort repo names for legacy reports that did not record REPO."""
    try:
        return tuple(sorted(
            path.name for path in WORKSPACE.iterdir()
            if path.is_dir() and not path.name.startswith(".")
            and ((path / ".git").exists() or (path / "package.json").is_file()
                 or (path / "go.mod").is_file())
        ))
    except OSError:
        return ()
SEVERITIES = ("Critical", "Warning", "Suggestion")

# Gate commands, per ship.md Step 1.3. Order matters: match the longest first so
# `npm run lint` doesn't shadow a more specific command.
GATE_COMMANDS = (
    "npx tsc --noEmit",
    "npm run test:ci",
    "npm run build",
    "npm run lint",
    "npm run format:check",
    "npm run sync",
    "go test ./...",
    "go test ./...",
    "go vet ./...",
)

RE_REVIEW_REPORT = re.compile(
    r"(\d+) findings:\s*(\d+) Critical,\s*(\d+) Warning,\s*(\d+) Suggestion"
)
RE_APPLIED_REPORT = re.compile(r"Applied (\d+)\s*/\s*Failed (\d+)\s*/\s*Skipped (\d+)")
# Orchestrator prompts pass variables as `NAME: value` lines; ship.md writes them
# as `NAME = value`. Accept either.
RE_ROUND_VAR = re.compile(r"\bROUND\s*[:=]\s*(\d+)")
RE_REPO_VAR = re.compile(r"^REPO\s*[:=]\s*(\S+)", re.MULTILINE)


def iter_jsonl(path):
    """Yield parsed records, skipping malformed lines. Streams — never loads the file."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def load_json(path):
    try:
        with Path(path).open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def text_of(content):
    """Flatten a message .content (str or list of blocks) to searchable text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            out.append(block.get("text") or "")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Session discovery
# --------------------------------------------------------------------------- #


def spawned_ship_agents(session_path):
    """True when this session spawned at least one /ship sub-agent."""
    sub_dir = session_path.parent / session_path.stem / "subagents"
    if not sub_dir.is_dir():
        return False
    for meta_path in sub_dir.glob("agent-*.meta.json"):
        if (load_json(meta_path) or {}).get("agentType") in SHIP_AGENTS:
            return True
    return False


def find_ship_sessions():
    """Sessions that actually executed /ship, newest first.

    Mentioning /ship is not the same as running it, so require evidence that a
    ship sub-agent was spawned; fall back to bare mentions only if none exist.
    """
    executed, mentioned = [], []
    for path in PROJECT_DIR.glob("*.jsonl"):
        # A spawned ship sub-agent is the proof that /ship actually ran. Check
        # the sub-agent metadata first: a run interrupted before its agents
        # reported still leaves metas behind, and is still worth analyzing.
        if spawned_ship_agents(path):
            executed.append(path)
            continue
        mentions_ship = any(
            rec.get("type") == "user"
            and "/ship" in text_of((rec.get("message") or {}).get("content"))
            for rec in iter_jsonl(path)
        )
        if mentions_ship:
            mentioned.append(path)
    by_recency = lambda paths: sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)
    return by_recency(executed) or by_recency(mentioned)


def resolve_session(session_arg, run_meta):
    if run_meta and run_meta.get("session_id"):
        candidate = PROJECT_DIR / f"{run_meta['session_id']}.jsonl"
        if candidate.exists():
            return candidate
    if session_arg and session_arg != "latest":
        matches = list(PROJECT_DIR.glob(f"{session_arg}*.jsonl"))
        if not matches:
            sys.exit(f"error: no session transcript matching {session_arg!r}")
        return matches[0]
    sessions = find_ship_sessions()
    if not sessions:
        sys.exit("error: no session transcript containing a /ship invocation")
    return sessions[0]


def _run_recency_key(run_dir):
    """Actual recency of a run, not the lexical order of its directory name.

    A run's directory name embeds the *plan's* slug (e.g. `plan-v6.3-...`), so
    sorting by name puts `v6.4` after `v6.3` even when the v6.3 run finished
    days later. Prefer `run.json`'s `started_at` (set once, at run start, so
    it can't be skewed by a later `/ship-analyze` read); fall back to the
    directory's mtime for runs whose run.json is missing or malformed.
    """
    meta = load_json(run_dir / "run.json") or {}
    started_at = meta.get("started_at")
    if started_at:
        try:
            return datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.fromtimestamp(run_dir.stat().st_mtime, tz=timezone.utc)


def resolve_run(run_arg):
    """Return (run_dir, run_meta) or (None, None) for pre-history runs."""
    if not RUNS_DIR.is_dir():
        if run_arg not in (None, "latest"):
            sys.exit(f"error: no run directory matching {run_arg!r} in {RUNS_DIR}")
        return None, None
    all_runs = [d for d in RUNS_DIR.iterdir() if d.is_dir()]
    if not all_runs:
        if run_arg not in (None, "latest"):
            sys.exit(f"error: no run directory matching {run_arg!r} in {RUNS_DIR}")
        return None, None
    if run_arg in (None, "latest"):
        run_dir = max(all_runs, key=_run_recency_key)
    else:
        matches = [d for d in all_runs if d.name == run_arg]
        if not matches:
            matches = [d for d in all_runs if d.name.startswith(run_arg)]
        if not matches:
            sys.exit(f"error: no run directory matching {run_arg!r} in {RUNS_DIR}")
        if len(matches) > 1:
            sys.exit(f"error: ambiguous run prefix {run_arg!r} in {RUNS_DIR}")
        run_dir = matches[0]
    return run_dir, load_json(run_dir / "run.json") or {}


# --------------------------------------------------------------------------- #
# Pass 1 — main transcript
# --------------------------------------------------------------------------- #


def scan_main_transcript(path):
    """Collect agent spawns/results, gate runs, and checkpoints from the orchestrator."""
    spawns = {}        # tool_use_id -> spawn info
    results = []       # completed agent phases
    gates = []
    bash_results = {}  # tool_use_id -> Bash outcome
    seen_tool_uses = set()
    seen_results = set()
    checkpoints = 0
    first_ts = last_ts = None

    for rec in iter_jsonl(path):
        ts = rec.get("timestamp")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts

        if rec.get("type") == "assistant":
            for block in rec.get("message", {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name, inp = block.get("name"), block.get("input") or {}
                tuid = block.get("id")
                # Transcripts replay assistant records (retries, resumes), so the
                # same tool_use can appear more than once. Count it once.
                if tuid in seen_tool_uses:
                    continue
                seen_tool_uses.add(tuid)

                if name in ("Agent", "Task"):
                    spawns[tuid] = {
                        "agent_type": inp.get("subagent_type"),
                        "description": inp.get("description", ""),
                        "prompt": inp.get("prompt", "") or "",
                        "timestamp": ts,
                    }
                elif name == "SendMessage":
                    # A resumed implementer: fix mode (review or gate).
                    msg = inp.get("message", "") or ""
                    spawns[f"sendmsg:{tuid}"] = {
                        "agent_type": "implementer",
                        "description": inp.get("summary", ""),
                        "prompt": msg,
                        "timestamp": ts,
                        "resumed": True,
                    }
                elif name == "Bash":
                    cmd = inp.get("command", "") or ""
                    for gate in GATE_COMMANDS:
                        if gate in cmd:
                            gates.append({"command": gate, "timestamp": ts, "tool_use_id": tuid})
                            break
                elif name == "AskUserQuestion":
                    # `questions` entries are usually objects but can be bare
                    # strings depending on how the call was constructed.
                    for q in inp.get("questions") or []:
                        text = q.get("question", "") if isinstance(q, dict) else q
                        if "next repo" in str(text or "").lower():
                            checkpoints += 1

        tur = rec.get("toolUseResult")
        if isinstance(tur, dict) and tur.get("agentType"):
            # The result record echoes the spawning tool_use_id — exact linkage,
            # so phases never get mispaired when a spawn yields no result.
            content = (rec.get("message") or {}).get("content")
            tool_use_id = None
            if isinstance(content, list) and content:
                first = content[0]
                if isinstance(first, dict):
                    tool_use_id = first.get("tool_use_id")
            # Same replay caveat as tool_use blocks — double-counting a result
            # would inflate this run's token and duration totals.
            if tool_use_id:
                if tool_use_id in seen_results:
                    continue
                seen_results.add(tool_use_id)
            usage = tur.get("usage") or {}
            stats = tur.get("toolStats") or {}
            results.append({
                "tool_use_id": tool_use_id,
                "agent_type": tur.get("agentType"),
                "description": tur.get("description", ""),
                "agent_id": tur.get("agentId"),
                "resolved_model": tur.get("resolvedModel"),
                "duration_ms": tur.get("totalDurationMs") or 0,
                "tokens": tur.get("totalTokens") or 0,
                "tool_uses": tur.get("totalToolUseCount") or 0,
                "cache_read": usage.get("cache_read_input_tokens") or 0,
                "cache_write": usage.get("cache_creation_input_tokens") or 0,
                "output_tokens": usage.get("output_tokens") or 0,
                "tool_stats": {
                    "reads": stats.get("readCount") or 0,
                    "bash": stats.get("bashCount") or 0,
                    "edits": stats.get("editFileCount") or 0,
                    "lines_added": stats.get("linesAdded") or 0,
                    "lines_removed": stats.get("linesRemoved") or 0,
                },
                "timestamp": rec.get("timestamp"),
            })
        elif isinstance(tur, dict) and "stdout" in tur:
            # Attach the exit status to the exact Bash call it belongs to. The
            # authoritative failure flag is `is_error` on the tool_result block;
            # returnCodeInterpretation is only sometimes present.
            content = (rec.get("message") or {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    result_id = block.get("tool_use_id")
                    if result_id:
                        bash_results[result_id] = {
                            "is_error": block.get("is_error"),
                            "interpretation": tur.get("returnCodeInterpretation"),
                        }

    # Resolve each gate's outcome now that every Bash result is collected.
    # `is_error` is only set on failure; a recorded result without it ran clean.
    # No result at all means unknown — report None rather than guessing "passed".
    for gate in gates:
        res = bash_results.get(gate.get("tool_use_id"))
        if res is None:
            gate["failed"] = None
        elif res.get("is_error") is not None:
            gate["failed"] = bool(res["is_error"])
        elif res.get("interpretation") is not None:
            gate["failed"] = res["interpretation"] != "success"
        else:
            gate["failed"] = False

    return {
        "spawns": spawns,
        "results": results,
        "gates": gates,
        "checkpoints": checkpoints,
        "started_at": first_ts,
        "ended_at": last_ts,
    }


def classify_phase(spawn):
    """Structural phase attribution — ship.md defines no marker strings."""
    agent = (spawn or {}).get("agent_type")
    prompt = (spawn or {}).get("prompt", "")
    if agent == "implementer":
        # A fresh Task spawn is always implement mode; only a SendMessage resume
        # is a fix pass. Older runs inlined the plan instead of passing
        # PLAN_EXCERPT, so the spawn/resume distinction is the reliable signal.
        if not (spawn or {}).get("resumed"):
            return "implement"
        if "FINDINGS_JSON" in prompt:
            return "fix-review"
        return "fix-gate"
    if agent == "reviewer":
        m = RE_ROUND_VAR.search(prompt)
        return f"review-r{m.group(1)}" if m else "review"
    if agent == "plan-checker":
        return "plan-check"
    return agent or "unknown"


# --------------------------------------------------------------------------- #
# Pass 2 — sub-agent transcripts (true model, post-routing)
# --------------------------------------------------------------------------- #


def scan_subagents(session_path):
    """From each sub-agent's own transcript: true models, and its final report line.

    Returns (models_by_agent, reports). `reports` recovers the review ladder for
    runs that predate plans/.ship-runs/ — the reviewer and implementer both end
    with a templated summary line that encodes the round's outcome.
    """
    sub_dir = session_path.parent / session_path.stem / "subagents"
    if not sub_dir.is_dir():
        return {}, []

    by_agent = defaultdict(lambda: defaultdict(int))
    reports = []
    for meta_path in sorted(sub_dir.glob("agent-*.meta.json")):
        meta = load_json(meta_path) or {}
        agent_type = meta.get("agentType")
        if not agent_type:
            continue
        transcript = meta_path.with_name(meta_path.name.replace(".meta.json", ".jsonl"))
        last_text = ""
        for rec in iter_jsonl(transcript):
            if rec.get("type") == "assistant":
                msg = rec.get("message") or {}
                model = msg.get("model")
                if model and model != "<synthetic>":
                    by_agent[agent_type][model] += 1
                text = text_of(msg.get("content"))
                if text.strip():
                    last_text = text

        entry = {
            "agent_type": agent_type,
            "description": meta.get("description", ""),
            # Lets the caller join back to the spawn prompt for the authoritative
            # REPO / ROUND, rather than guessing them from the description prose.
            "tool_use_id": meta.get("toolUseId"),
        }
        m = RE_REVIEW_REPORT.search(last_text)
        if m:
            entry["findings"] = {
                "total": int(m.group(1)),
                "by_severity": dict(zip(SEVERITIES, (int(m.group(2)), int(m.group(3)), int(m.group(4))))),
            }
        m = RE_APPLIED_REPORT.search(last_text)
        if m:
            entry["applied"] = {
                "applied": int(m.group(1)),
                "failed": int(m.group(2)),
                "skipped": int(m.group(3)),
            }
        if "ALL PLAN ITEMS MET" in last_text:
            entry["plan_verdict"] = "ALL PLAN ITEMS MET"
        if len(entry) > 3:
            reports.append(entry)

    return {k: dict(v) for k, v in by_agent.items()}, reports


# --------------------------------------------------------------------------- #
# Pass 3 — run history (the review ladder)
# --------------------------------------------------------------------------- #


def scan_run_history(run_dir):
    """Reconstruct the per-repo round ladder from the append-only run directory."""
    if not run_dir or not run_dir.is_dir():
        return {}

    repos = {}
    for repo_dir in sorted(d for d in run_dir.iterdir() if d.is_dir()):
        rounds = {}
        for f in sorted(repo_dir.glob("round-*-findings.json")):
            m = re.search(r"round-(\d+)-findings", f.name)
            data = load_json(f)
            if not m:
                continue
            if (not isinstance(data, dict) or not isinstance(data.get("findings"), list)
                    or not all(isinstance(f, dict) for f in data["findings"])):
                rounds.setdefault(int(m.group(1)), {})["findings"] = {
                    "total": None, "by_severity": {}, "invalid": True}
                continue
            findings = data["findings"]
            if data.get("schema_version") == 2:
                rounds.setdefault(int(m.group(1)), {})["accepted"] = data.get("accepted")
            rounds.setdefault(int(m.group(1)), {})["findings"] = {
                "total": len(findings),
                "by_severity": {s: sum(1 for x in findings if x.get("severity") == s)
                                for s in SEVERITIES},
                "by_status": dict(_count(x.get("status", "unspecified") for x in findings)),
                "by_category": dict(_count(x.get("category", "uncategorized") for x in findings)),
                "ids": [x.get("id") for x in findings if x.get("id")],
                "source_fingerprint": data.get("source_fingerprint"),
            }
        for f in sorted(repo_dir.glob("round-*-meta.json")):
            m = re.fullmatch(r"round-(\d+)-meta\.json", f.name)
            data = load_json(f)
            if m and data is not None:
                rounds.setdefault(int(m.group(1)), {})["accepted"] = data.get("accepted")
        for f in sorted(repo_dir.glob("round-*-applied.json")):
            m = re.search(r"round-(\d+)-applied", f.name)
            data = load_json(f)
            if not m or data is None:
                continue
            rounds.setdefault(int(m.group(1)), {})["applied"] = {
                "applied": len(data.get("applied") or []),
                "failed": len(data.get("failed") or []),
                "skipped": len(data.get("skipped") or []),
                "failed_detail": data.get("failed") or [],
                "skipped_detail": data.get("skipped") or [],
            }
        if rounds:
            states = []
            for f in repo_dir.glob("state-*.json"):
                m = re.fullmatch(r"state-(\d+)\.json", f.name)
                data = load_json(f)
                if m and data is not None:
                    states.append((int(m.group(1)), data))
            latest_state = max(states, key=lambda entry: entry[0])[1] if states else None
            repos[repo_dir.name] = analyze_ladder(rounds, latest_state)
    return repos


def normalize_model(name):
    """Strip decorations that don't change which model ran.

    `claude-opus-5[1m]` is opus-5 with a 1-hour cache TTL, and provider-qualified
    ids like `DeepSeek/deepseek-v4-flash` name the same model as `deepseek-v4-flash`.
    Comparing raw strings would report a mismatch where none exists.
    """
    if not name:
        return name
    name = re.sub(r"\[[^\]]*\]$", "", name)
    return name.rsplit("/", 1)[-1].strip()


def _count(iterable):
    counts = defaultdict(int)
    for item in iterable:
        counts[item] += 1
    return counts


def analyze_ladder(rounds, latest_state=None):
    """Judge convergence and churn, respecting version-bound results when present."""
    ordered = [rounds[k] for k in sorted(rounds)]
    last = ordered[-1]
    last_findings = last.get("findings") or {}
    sev = last_findings.get("by_severity") or {}

    converged = bool(last_findings) and last_findings.get("total") == 0 and last.get("accepted") is not False
    if latest_state:
        converged = converged and latest_state.get("status") in {"clean", "verified"}
        fingerprint = last_findings.get("source_fingerprint")
        if fingerprint:
            converged = converged and fingerprint == latest_state.get("source_fingerprint")
        if latest_state.get("latest_review_round") is not None:
            converged = converged and latest_state["latest_review_round"] == max(rounds)
    # Severity counts describe the findings; zero Critical does not make later review dispensable.
    later_criticals = sum(
        (r.get("findings") or {}).get("by_severity", {}).get("Critical", 0)
        for r in ordered[1:]
    )
    churn = sum(
        (r.get("findings") or {}).get("by_status", {}).get(s, 0)
        for r in ordered for s in ("recurring", "regressed")
    )
    unapplied = sum(
        (r.get("applied") or {}).get("failed", 0) + (r.get("applied") or {}).get("skipped", 0)
        for r in ordered
    )

    return {
        "rounds_used": len(ordered),
        "converged": converged,
        "review_certified": converged and last.get("accepted") is True and latest_state is not None,
        "goals_verified": bool(latest_state and latest_state.get("status") == "verified"),
        "certification": "versioned" if last.get("accepted") is True else "legacy/uncertified",
        "final_state": latest_state.get("status") if latest_state else None,
        "final_review_accepted": last.get("accepted"),
        "final_open": last_findings.get("total", 0),
        "final_severity": sev,
        "criticals_after_round_1": later_criticals,
        "churn": churn,
        "unapplied": unapplied,
        "rounds": {str(k): rounds[k] for k in sorted(rounds)},
    }


# --------------------------------------------------------------------------- #
# Pass 4 — legacy CCR cost enrichment (optional; absent for ocx-routed runs)
# --------------------------------------------------------------------------- #


def scan_cost(session_id):
    """Join real USD cost from the legacy CCR log. Runs routed through opencodex have no row here."""
    if not CCR_DB.exists():
        return {"available": False, "reason": "legacy CCR cost database not present (opencodex keeps no USD cost)"}
    try:
        conn = sqlite3.connect(f"file:{CCR_DB}?immutable=1", uri=True)
    except sqlite3.Error as exc:
        return {"available": False, "reason": f"cannot open cost database: {exc}"}

    try:
        cur = conn.execute(
            """
            SELECT model,
                   COUNT(*),
                   SUM(COALESCE(cost_usd, 0)),
                   SUM(cache_read_tokens),
                   SUM(cache_write_tokens),
                   SUM(input_tokens),
                   SUM(status_code = 429)
              FROM request_logs
             WHERE json_extract(request_headers, '$."x-claude-code-session-id"') = ?
             GROUP BY model
            """,
            (session_id,),
        )
        rows = cur.fetchall()
    except sqlite3.Error as exc:
        conn.close()
        return {"available": False, "reason": f"cost query failed: {exc}"}
    conn.close()

    if not rows:
        return {"available": False, "reason": "session not in the legacy CCR log (routed through opencodex, or predates logging)"}

    by_model, total, rate_limited = {}, 0.0, 0
    for model, n, cost, c_read, c_write, fresh_in, n429 in rows:
        cost = cost or 0.0
        total += cost
        rate_limited += n429 or 0
        denom = (c_read or 0) + (c_write or 0) + (fresh_in or 0)
        by_model[model] = {
            "requests": n,
            "cost_usd": round(cost, 4),
            "rate_limited_429": n429 or 0,
            "cache_hit_pct": round(100.0 * (c_read or 0) / denom, 1) if denom else None,
        }
    return {
        "available": True,
        "total_cost_usd": round(total, 4),
        "rate_limited_429": rate_limited,
        "by_model": by_model,
    }


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def build_phases(main, subagent_models):
    """Pair each agent result with its spawn (by tool_use_id) into an ordered phase list."""
    spawns = main["spawns"]
    # Fallback for results whose tool_use_id didn't resolve: consume spawns of the
    # same agent type in order, so a missing id degrades instead of mispairing.
    leftovers = defaultdict(list)
    for key, spawn in spawns.items():
        if spawn.get("agent_type"):
            leftovers[spawn["agent_type"]].append(spawn)

    phases = []
    used = defaultdict(int)
    for res in main["results"]:
        agent = res["agent_type"]
        spawn = spawns.get(res.get("tool_use_id"))
        if spawn is None:
            queue = leftovers.get(agent) or []
            idx = used[agent]
            spawn = queue[idx] if idx < len(queue) else None
            used[agent] += 1

        true_models = subagent_models.get(agent) or {}
        true_model = next(iter(true_models)) if len(true_models) == 1 else None
        phases.append({
            "phase": classify_phase(spawn) if spawn else agent,
            "agent_type": agent,
            "description": res["description"] or (spawn or {}).get("description", ""),
            "resolved_model": res["resolved_model"],
            "true_model": true_model,
            "model_attribution": "legacy agent-type aggregate, not phase-certified",
            "model_mismatch": bool(
                true_model
                and res["resolved_model"]
                and normalize_model(true_model) != normalize_model(res["resolved_model"])
            ),
            "tokens": res["tokens"],
            "output_tokens": res["output_tokens"],
            "cache_read": res["cache_read"],
            "cache_write": res["cache_write"],
            "tool_uses": res["tool_uses"],
            "duration_ms": res["duration_ms"],
            "tool_stats": res["tool_stats"],
        })
    return phases


def enrich_reports(reports, spawns):
    """Attach the authoritative REPO/ROUND from each report's spawn prompt.

    Only needed for runs predating the history directory; afterwards the run
    directory already groups rounds by repo.
    """
    for entry in reports:
        spawn = spawns.get(entry.get("tool_use_id"))
        prompt = (spawn or {}).get("prompt", "")
        haystack = f"{prompt}\n{entry.get('description', '')}"

        m = RE_REPO_VAR.search(prompt)
        if m:
            entry["repo"] = m.group(1)
        else:
            # Some spawns describe the target in prose instead of REPO: vars.
            # Match the longest repo path/name mentioned, so `..._client` never
            # loses to the analytics prefix it contains.
            hits = [r for r in known_repos() if r in haystack]
            if hits:
                entry["repo"] = max(hits, key=len)

        m = RE_ROUND_VAR.search(prompt)
        if m:
            entry["round"] = int(m.group(1))
        else:
            m = re.search(r"round[- ]?(\d+)|round\s+(\d+)", haystack, re.I)
            if m:
                entry["round"] = int(m.group(1) or m.group(2))
    # Order by repo then round so the ladder reads top-to-bottom. Entries that
    # resolved neither sort last rather than interleaving misleadingly.
    return sorted(
        reports,
        key=lambda e: (e.get("repo") or "~", e.get("round") or 99),
    )


def build_suggestions(phases, ladders, cost, reported_rounds):
    """Every suggestion cites the number from THIS run that triggered it."""
    out = []

    # Pre-history runs: judge convergence from the reviewers' own report lines,
    # grouped per repo (a run reviews each repo on its own ladder).
    if not ladders and reported_rounds:
        by_repo = defaultdict(list)
        for r in reported_rounds:
            if "findings" in r:
                by_repo[r.get("repo") or "unknown-repo"].append(r)
        for repo, rounds in by_repo.items():
            rounds.sort(key=lambda r: r.get("round") or 0)
            last = rounds[-1]["findings"]
            later = rounds[1:]
            if later and sum(r["findings"]["by_severity"].get("Critical", 0) for r in later) == 0:
                out.append(
                    f"{repo}: {len(rounds)} review rounds ran and rounds 2-{len(rounds)} produced "
                    f"0 Critical findings. Inspect Warning/Suggestion substance and fix evidence; "
                    f"severity counts alone do not justify skipping independent verification."
                )
            if last["total"] > 0:
                sev = last["by_severity"]
                out.append(
                    f"{repo}: the final round still reported {last['total']} open findings "
                    f"({sev['Critical']}C {sev['Warning']}W {sev['Suggestion']}S) — the loop ended "
                    f"on its round cap, not on convergence."
                )

    for repo, lad in ladders.items():
        if not lad["converged"]:
            if lad.get("final_state") or lad.get("final_review_accepted") is False:
                out.append(
                    f"{repo}: {lad['rounds_used']} reviews, final state {lad.get('final_state')}, "
                    f"review accepted={lad.get('final_review_accepted')}, "
                    f"{lad['final_open']} open findings — not independently clean on the final source."
                )
            elif lad["rounds_used"] >= 3:
                out.append(
                    f"{repo}: {lad['rounds_used']} rounds and {lad['final_open']} open findings "
                    f"({lad['final_severity']}) — not converged; legacy history has no explicit cap state."
                )
        if lad["rounds_used"] > 1 and lad["criticals_after_round_1"] == 0:
            out.append(
                f"{repo}: rounds 2-{lad['rounds_used']} produced 0 Critical findings. "
                f"Check Warning/Suggestion substance and verification rigor before tuning speed; "
                f"do not replace a clean final review with a severity-based early exit."
            )
        if lad["churn"]:
            out.append(
                f"{repo}: {lad['churn']} finding(s) marked recurring/regressed — the reviewer is "
                f"re-raising settled ground, or fixes are not landing. Check the implementer's "
                f"fix quality before blaming the reviewer."
            )
        if lad["unapplied"]:
            out.append(
                f"{repo}: {lad['unapplied']} finding(s) failed or were skipped by the implementer. "
                f"Inspect recorded reasons/evidence: an environment blocker is not automatically a vague skip."
            )

    for ph in phases:
        if ph["model_mismatch"]:
            out.append(
                f"{ph['agent_type']}: resolvedModel={ph['resolved_model']} but the sub-agent "
                f"transcript shows {ph['true_model']}. resolvedModel is pre-routing intent — "
                f"verify the pin is doing what you think."
            )
            break

    if cost.get("available"):
        if cost["rate_limited_429"] > 20:
            out.append(
                f"{cost['rate_limited_429']} × HTTP 429 in this session — sub-agent spawns are "
                f"hitting the rate limit, which costs wall-clock time and retries."
            )
        for model, m in cost["by_model"].items():
            if m["cache_hit_pct"] is not None and m["cache_hit_pct"] < 85 and m["requests"] > 20:
                out.append(
                    f"{model}: only {m['cache_hit_pct']}% of input tokens were cache reads across "
                    f"{m['requests']} requests — prompt prefix is churning between calls."
                )

    # Rank the biggest token consumer, which is where tuning pays off most.
    if phases:
        top = max(phases, key=lambda p: p["tokens"])
        total_tokens = sum(p["tokens"] for p in phases) or 1
        share = 100.0 * top["tokens"] / total_tokens
        if share > 40:
            out.append(
                f"{top['phase']} ({top['agent_type']}) alone is {share:.0f}% of this run's "
                f"{total_tokens:,} tokens — tune that phase first."
            )
    return out


def report_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ship_report", Path(__file__).with_name("report.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def emit_report(report, output_format):
    if output_format == "compact":
        sys.stdout.write(report_module().compact(report))
    else:
        json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")


def main():
    ap = argparse.ArgumentParser(description="Analyze a /ship or /fix run for accounting and verification.")
    ap.add_argument("--run", default="latest", help="run-id prefix under plans/.ship-runs, or 'latest'")
    ap.add_argument("--workspace", help="project root containing plans and installed workflow")
    ap.add_argument("--session", help="session-id prefix; overrides the run's recorded session")
    ap.add_argument("--no-cost", action="store_true", help="skip legacy CCR cost enrichment")
    formats = ap.add_mutually_exclusive_group()
    formats.add_argument("--json", action="store_true", help="emit JSON (default; kept for clarity)")
    formats.add_argument("--format", choices=("json", "compact"), default="json", help="output presentation")
    ap.add_argument("--workflow", choices=("ship", "fix"), default="ship", help="run namespace")
    args = ap.parse_args()
    global WORKSPACE, RUNS_DIR, PROJECT_DIR
    if args.workspace:
        WORKSPACE = Path(args.workspace).expanduser().resolve()
        if not WORKSPACE.is_dir():
            ap.error("workspace must be an existing directory")
        PROJECT_DIR = Path(os.environ.get("SHIP_ANALYZE_TRANSCRIPTS", Path.home() / ".claude/projects" / str(WORKSPACE).replace("/", "-"))).expanduser()
    RUNS_DIR = WORKSPACE / "plans" / (".fix-runs" if args.workflow == "fix" else ".ship-runs")

    run_dir, run_meta = resolve_run(args.run)
    if run_dir and (load_json(run_dir / "contract.json") or {}).get("schema_version") == 2:
        import importlib.util
        spec = importlib.util.spec_from_file_location("run_telemetry", Path(__file__).with_name("telemetry.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if args.session:
            # Explicit override is local to analysis, never rewrites run history.
            session_path = resolve_session(args.session, None)
            project = session_path.parent
        else:
            project = PROJECT_DIR
        telemetry = module.analyze(run_dir, project, session_path.stem if args.session else None, WORKSPACE)
        insight_spec = importlib.util.spec_from_file_location("run_insights", Path(__file__).with_name("insights.py"))
        insight_module = importlib.util.module_from_spec(insight_spec)
        insight_spec.loader.exec_module(insight_module)
        insights = insight_module.analyze(run_dir, telemetry)
        report = {
            "run_id": run_meta.get("run_id"), "run_dir": str(run_dir), "workflow": args.workflow,
            "plan_path": run_meta.get("plan_path"), "schema_version": 2,
            "history_available": True, "accuracy": report_module().reconcile_accuracy(scan_run_history(run_dir), telemetry),
            "telemetry": telemetry, "phases": telemetry["phases"], "insights": insights,
            "cost": {"available": False, "reason": "No request-level USD attribution; session cost is not run cost"},
            "suggestions": telemetry["warnings"],
        }
        emit_report(report, args.format)
        return
    # An explicit --session wins over the run's recorded one.
    session_path = resolve_session(args.session, None if args.session else run_meta)
    session_id = session_path.stem

    main_scan = scan_main_transcript(session_path)
    subagent_models, subagent_reports = scan_subagents(session_path)
    phases = build_phases(main_scan, subagent_models)
    ladders = scan_run_history(run_dir)
    if not ladders:
        subagent_reports = enrich_reports(subagent_reports, main_scan["spawns"])
    cost = {"available": False, "reason": "skipped"} if args.no_cost else scan_cost(session_id)

    ship_phases = [p for p in phases if p["agent_type"] in SHIP_AGENTS]
    # A session can mention /ship without executing it (planning, a question
    # about the command). Say so explicitly — an empty report otherwise reads
    # like the analyzer failed.
    spawned_ship = spawned_ship_agents(session_path)
    no_ship_note = None
    if not ship_phases:
        if spawned_ship:
            # Agents ran but never reported — usually an interrupted session.
            # Accuracy is still recoverable from their transcripts; cost is not.
            no_ship_note = (
                "This session spawned /ship sub-agents but recorded no completed results — it was "
                "likely interrupted. Per-phase token and duration figures are unavailable; the "
                "review ladder below comes from the sub-agents' own transcripts."
            )
        else:
            other = sorted({p["agent_type"] for p in phases if p["agent_type"]})
            no_ship_note = (
                "This session ran no /ship sub-agents"
                + (f" (it spawned: {', '.join(other)})" if other else "")
                + " — it references /ship without executing it. Pick a different session."
            )
    gates = [
        {"command": g["command"], "failed": g.get("failed")}
        for g in main_scan["gates"]
    ]

    report = {
        "run_id": run_meta.get("run_id") if run_meta else None,
        "run_dir": str(run_dir) if run_dir else None,
        "plan_path": run_meta.get("plan_path") if run_meta else None,
        "session_id": session_id,
        "started_at": main_scan["started_at"],
        "ended_at": main_scan["ended_at"],
        "ship_run": bool(ship_phases) or spawned_ship,
        "no_ship_note": no_ship_note,
        "history_available": bool(ladders),
        "history_note": None if ladders else (
            "No per-round history for this run — it predates plans/.ship-runs/. Accuracy is "
            "reconstructed from sub-agent report lines, so recurring/regressed churn is not "
            "available (only runs after the history change record it)."
        ),
        # Populated only when the run predates the history directory.
        "reported_rounds": [] if ladders else subagent_reports,
        "totals": {
            "agent_phases": len(ship_phases),
            "tokens": sum(p["tokens"] for p in ship_phases),
            "tool_uses": sum(p["tool_uses"] for p in ship_phases),
            "duration_min": round(sum(p["duration_ms"] for p in ship_phases) / 60000.0, 1),
        },
        "phases": phases,
        "accuracy": ladders,
        "gates": gates,
        "gate_failures": sum(1 for g in gates if g.get("failed")),
        "checkpoints": main_scan["checkpoints"],
        "cost": {**cost, "attribution": "legacy session scope, not certified run cost"},
        "accounting_complete": False,
        "accounting_note": "Legacy session aggregates may include other runs and omit resumed fixes; tokens/time are partial, not billable run totals.",
        "suggestions": build_suggestions([], ladders, cost, subagent_reports),
    }
    emit_report(report, args.format)


if __name__ == "__main__":
    main()
