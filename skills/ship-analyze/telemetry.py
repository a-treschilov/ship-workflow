"""Read-only, run-scoped phase accounting. Unknown usage is never a zero estimate."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re

USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
AGENT_PHASES = {"implement", "review-fix", "gate-fix", "review", "checker"}


def read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def at(text):
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return value if value.tzinfo else None
    except (ValueError, AttributeError):
        return None


def records(path):
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                    yield value if isinstance(value, dict) else {"_invalid": True}
                except ValueError:
                    if line.strip():
                        yield {"_invalid": True}
    except OSError:
        yield {"_invalid": True}


def agent_transcript(project, session_id, start, end):
    directory = project / session_id / "subagents"
    identifier = end.get("agent_id") or start.get("agent_id")
    if identifier and re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
        path = directory / f"agent-{identifier}.jsonl"
        if path.is_file():
            return path, identifier
    correlation = end.get("tool_use_id") or start.get("tool_use_id")
    if correlation:
        hits = []
        for path in directory.glob("agent-*.meta.json"):
            meta = read(path) or {}
            if meta.get("toolUseId") == correlation:
                hits.append(path.with_name(path.name.replace(".meta.json", ".jsonl")))
        if len(hits) == 1 and hits[0].is_file():
            return hits[0], hits[0].stem.removeprefix("agent-")
    return None, identifier


def collect_requests(path):
    """Collapse replays/chunks before attribution; never infer an owner from timing alone."""
    entries, reasons = {}, []
    for record in records(path):
        if record.get("_invalid"):
            reasons.append("Malformed transcript record")
            continue
        if record.get("type") != "assistant":
            continue
        msg = record.get("message")
        if not isinstance(msg, dict):
            reasons.append("Malformed assistant message")
            continue
        if msg.get("model") == "<synthetic>":
            continue
        identifier = record.get("requestId") or msg.get("id") or record.get("uuid")
        if not isinstance(identifier, str) or not identifier:
            reasons.append("Assistant request has no identifier")
            continue
        entry = entries.setdefault(identifier, {"id": identifier, "timestamps": set(), "models": set(), "usage": {}, "invalid": set(), "unknown_time": False})
        moment = at(record.get("timestamp"))
        if moment is None:
            entry["unknown_time"] = True
        else:
            entry["timestamps"].add(moment)
        if isinstance(msg.get("model"), str) and msg["model"]:
            entry["models"].add(msg["model"])
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        for field in USAGE_FIELDS:
            if field not in usage:
                continue
            number = usage[field]
            if type(number) is int and number >= 0:
                entry["usage"][field] = max(number, entry["usage"].get(field, 0))
            else:
                entry["invalid"].add(field)
    for entry in entries.values():
        entry["timestamp"] = min(entry["timestamps"], default=None)
        for field in entry["invalid"]:
            entry["usage"].pop(field, None)
    return entries, sorted(set(reasons))


def transcript_sums(entries, uncertain=False):
    entries = list(entries)
    sums = {f: sum(e["usage"][f] for e in entries) if entries and not uncertain and all(f in e["usage"] for e in entries) else None for f in USAGE_FIELDS}
    models = sorted({m for e in entries for m in e["models"]})
    complete = not uncertain and all(v is not None for v in sums.values()) and all(len(e["models"]) == 1 for e in entries)
    return sums, models, len(entries), complete


def request_usage(path, start_time, end_time, owner=None, seen=None):
    """Compatibility helper; callers must resolve phase ownership before summing."""
    entries, reasons = collect_requests(path)
    selected = [e for e in entries.values() if e["timestamp"] is not None and start_time <= e["timestamp"] <= end_time]
    return transcript_sums(selected, bool(reasons) or any(e["unknown_time"] for e in entries.values()))


OCX_LOG = Path.home() / ".opencodex/usage.jsonl"
RE_OCX_ID = re.compile(r"ocx-[0-9a-f]+")


def transcript_requests(path):
    """requestId -> first assistant timestamp for requests routed through ocx."""
    found = {}
    for record in records(path):
        rid = record.get("requestId")
        if record.get("type") != "assistant" or not isinstance(rid, str) or not RE_OCX_ID.fullmatch(rid):
            continue
        moment = at(record.get("timestamp"))
        if rid not in found or (moment is not None and (found[rid] is None or moment < found[rid])):
            found[rid] = moment
    return found


def read_ocx_log(wanted, window):
    """One streaming pass: rows for exactly `wanted` ids plus every row inside `window` (ms)."""
    by_id, in_window = {}, {}
    try:
        with OCX_LOG.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                rid = row.get("requestId") if isinstance(row, dict) else None
                if not isinstance(rid, str):
                    continue
                if rid in wanted:
                    if rid in by_id and by_id[rid] != row:
                        by_id[rid] = {**row, "_ambiguous": True}
                    else:
                        by_id[rid] = row
                stamp = row.get("timestamp")
                if window and type(stamp) is int and window[0] <= stamp <= window[1]:
                    in_window[rid] = row
    except OSError:
        return None, None
    return by_id, in_window


def ocx_sums(rows, expected_ids=None):
    """Strict normalized counters; OCX input includes both cache counters."""
    rows = list(rows)
    expected = set(expected_ids) if expected_ids is not None else None
    found = {r.get("requestId") for r in rows}
    missing = sorted(expected - found) if expected is not None else []
    reasons = [f"Missing OCX row: {rid}" for rid in missing]
    fields = ("fresh_input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    values = {f: [] for f in fields}
    models = {}
    for row in rows:
        usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
        reported = row.get("usageStatus") == "reported" and not row.get("_ambiguous")
        counters = [usage.get(k) for k in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")]
        counters = [v if reported and type(v) is int and v >= 0 else None for v in counters]
        inp, out, cache, write = counters
        fresh = inp - cache - write if all(v is not None for v in (inp, cache, write)) else None
        if fresh is not None and fresh < 0:
            fresh = None
            reasons.append(f"Inconsistent OCX input/cache counters: {row.get('requestId')}")
        for field, value in zip(fields, (fresh, out, cache, write)):
            values[field].append(value)
            if value is None:
                reasons.append(f"Unknown/invalid OCX {field}: {row.get('requestId')}")
        model = row.get("model")
        if isinstance(model, str) and model:
            models[model] = models.get(model, 0) + 1
        else:
            reasons.append(f"Unknown actual OCX model: {row.get('requestId')}")
    count = len(expected) if expected is not None else len(rows)
    sums = {f: sum(values[f]) if not missing and all(v is not None for v in values[f]) else None for f in fields}
    sums.update(requests=count, expected_requests=count, matched_requests=len(rows),
                missing_in_log=len(missing), missing_request_ids=missing,
                known_subtotal={f: sum(v for v in values[f] if v is not None) for f in fields},
                models=dict(sorted(models.items())), complete=not reasons, reasons=sorted(set(reasons)))
    return sums


def union_seconds(intervals):
    total = 0.0
    previous_start = previous_end = None
    for start, end in sorted(intervals):
        if previous_end is None:
            previous_start, previous_end = start, end
        elif start <= previous_end:
            previous_end = max(previous_end, end)
        else:
            total += (previous_end - previous_start).total_seconds()
            previous_start, previous_end = start, end
    if previous_end is not None:
        total += (previous_end - previous_start).total_seconds()
    return total


def ocx_join(project, sessions, jobs, wall_start, wall_end):
    """Resolve deduplicated ownership globally, then select one canonical usage source."""
    orchestrator_paths = {project / f"{s}.jsonl" for s in sessions}
    # Session membership alone cannot correlate a subagent to this run. Only
    # canonical phase jobs prove the agent/run relationship; the orchestrator
    # is separately and explicitly scoped to its session's observation window.
    paths = sorted(orchestrator_paths | {job[1] for job in jobs})
    collected = {p: collect_requests(p) for p in paths if p.is_file()}
    # The same request in multiple transcript files is not independent billable work.
    owners = {}
    for path, (entries, _) in collected.items():
        for identifier in entries:
            owners.setdefault(identifier, set()).add(path)
    wanted = {i for i in owners if RE_OCX_ID.fullmatch(i)}
    window = (int(wall_start.timestamp() * 1000), int(wall_end.timestamp() * 1000)) if wall_start and wall_end and wall_end >= wall_start else None
    by_id, in_window = read_ocx_log(wanted, window)
    available = by_id is not None
    by_id, in_window = by_id or {}, in_window or {}
    rows_of = lambda ids: [by_id[i] for i in sorted(ids) if i in by_id]
    selected = {p["phase_id"]: [] for p, _, _, _ in jobs}
    blocked = {p["phase_id"]: [] for p, _, _, _ in jobs}
    buckets = {k: set() for k in ("in_phase", "in_run_outside_phases", "outside_run", "unknown_time", "ambiguous")}
    path_buckets = {}
    for path, (entries, errors) in collected.items():
        local = {k: set() for k in buckets}
        related = [j for j in jobs if j[1] == path]
        for identifier, entry in entries.items():
            stamp = entry["timestamp"]
            matches = [j for j in related if any(j[2] <= m <= j[3] for m in entry["timestamps"])]
            ambiguous = len(owners[identifier]) > 1 or len(matches) > 1
            if entry["unknown_time"] or stamp is None:
                bucket = "unknown_time"
            elif (wall_start and stamp < wall_start) or (wall_end and stamp > wall_end):
                bucket = "outside_run"
            elif ambiguous:
                bucket = "ambiguous"
            elif len(matches) == 1:
                bucket = "in_phase"
                selected[matches[0][0]["phase_id"]].append(entry)
            elif wall_start and wall_end and wall_start <= stamp <= wall_end:
                bucket = "in_run_outside_phases"
            else:
                bucket = "outside_run"
            local[bucket].add(identifier)
            buckets[bucket].add(identifier)
            if bucket in {"ambiguous", "unknown_time"}:
                for phase, _, _, _ in (matches or related):
                    blocked[phase["phase_id"]].append(f"{bucket} request: {identifier}")
        path_buckets[path] = local
        for phase, _, _, _ in related:
            blocked[phase["phase_id"]].extend(errors)
    for phase, path, _, _ in jobs:
        entries = selected[phase["phase_id"]]
        reasons = sorted(set(blocked[phase["phase_id"]]))
        if not entries:
            reasons.append("No observed requests in phase; absence is not proof of zero usage")
        usage, models, count, complete = transcript_sums(entries, bool(reasons))
        transcript = {"usage": usage, "models": models, "requests": count, "complete": complete,
                      "reasons": reasons or ([] if complete else ["Incomplete transcript usage/model counters"])}
        phase["transcript"] = transcript
        ids = {e["id"] for e in entries if RE_OCX_ID.fullmatch(e["id"])}
        sums = ocx_sums(rows_of(ids), ids)
        if len(ids) != count or reasons or not available:
            sums["complete"] = False
            sums["reasons"] += reasons + (["Some transcript requests have no OCX requestId"] if len(ids) != count else []) + ([] if available else ["OCX log unreadable"])
        normalized = {"input_tokens": sums["fresh_input_tokens"], **{f: sums[f] for f in USAGE_FIELDS if f != "input_tokens"}}
        sums["vs_transcript"] = {"requests_equal": count == len(ids), "transcript_output_tokens": usage["output_tokens"],
                                  "output_tokens_equal": usage["output_tokens"] == sums["output_tokens"],
                                  "disagreements": {f: {"transcript": usage[f], "ocx": normalized[f]} for f in USAGE_FIELDS if usage[f] != normalized[f]}}
        phase["ocx"] = sums
        ocx_partial = (available and not complete and count > 0 and len(ids) == count and
                       not sums["missing_request_ids"] and not reasons)
        choose_ocx = sums["complete"] or ocx_partial
        source = "ocx exact requestId join" if choose_ocx else "deduplicated transcript messages"
        phase.update(usage=normalized if choose_ocx else usage,
                     models=sorted(sums["models"]) if choose_ocx else models,
                     requests=count, usage_source=source, usage_complete=sums["complete"] or complete,
                     usage_reasons=[] if sums["complete"] else sums["reasons"],
                     correlation_complete=not reasons, accounting_complete=phase.get("event_valid", True) and not reasons and (sums["complete"] or complete))
    agents = {}
    for path, local in path_buckets.items():
        if not path.name.startswith("agent-"):
            continue
        own = set(collected[path][0]) & wanted
        label = path.parent.parent.name + "/" + path.stem.removeprefix("agent-")
        scoped = (local["in_phase"] | local["in_run_outside_phases"]) & own
        outside = local["in_run_outside_phases"] & own
        agents[label] = {"total": ocx_sums(rows_of(own), own), "transcript_wide_total": ocx_sums(rows_of(own), own),
                         "in_run": ocx_sums(rows_of(scoped), scoped),
                         "outside_recorded_phases": ocx_sums(rows_of(outside), outside),
                         "buckets": {k: len(v) for k, v in local.items()},
                         **{k: ocx_sums(rows_of(local[k] & own), local[k] & own) for k in ("outside_run", "unknown_time", "ambiguous")}}
    orchestrator_ids = set().union(*(local["in_run_outside_phases"] for p, local in path_buckets.items() if p in orchestrator_paths))
    orchestrator_ids &= wanted
    stray = {i: row for i, row in in_window.items() if i not in wanted}
    other, uncorrelated = {}, {}
    for path in list(project.glob("*.jsonl")) + list(project.glob("*/subagents/agent-*.jsonl")):
        if path in collected:
            continue
        hits = set(transcript_requests(path)) & set(stray)
        if hits:
            label = path.stem if path.parent == project else path.parent.parent.name
            target = uncorrelated if path.parent != project and label in sessions else other
            key = label + "/" + path.stem.removeprefix("agent-") if target is uncorrelated else label
            target.setdefault(key, set()).update(hits)
    other_ids = set().union(*other.values(), *uncorrelated.values())
    unattributed = [row for i, row in stray.items() if i not in other_ids]
    conversations = {r.get("conversationId") for r in rows_of(wanted) if r.get("conversationId")}
    return {"available": available, "reason": None if available else f"ocx usage log unreadable: {OCX_LOG}",
            "source": str(OCX_LOG), "join": "transcript requestId == ocx requestId (exact)",
            "agents": agents, "orchestrator": ocx_sums(rows_of(orchestrator_ids), orchestrator_ids),
            "coverage": {k: {"requests": len(v), "request_ids": sorted(v)} for k, v in buckets.items()},
            "other_sessions_in_window": {o: ocx_sums([stray[i] for i in ids], ids) for o, ids in other.items()},
            "uncorrelated_agents_in_window": {o: ocx_sums([stray[i] for i in ids], ids) for o, ids in uncorrelated.items()},
            "unattributed_in_window": {**ocx_sums(unattributed), "sharing_conversation_with_run": sum(r.get("conversationId") in conversations for r in unattributed),
                                       "note": "Not added to run totals; conversationId is not proof"},
            "notes": [f"{len(buckets['in_run_outside_phases'])} requests inside observation window outside recorded phases"],
            "scope": "tokens only (USD unavailable); agent total is transcript-wide; in_run and outside_recorded_phases are window-scoped"}

CODEX_SESSION_ROOTS = (Path.home() / ".codex" / "sessions", Path.home() / ".codex-personal" / "sessions")
CODEX_ARCHIVED_ROOT = Path.home() / ".codex-personal" / "archived_sessions"
CODEX_ENV = "SHIP_ANALYZE_CODEX_TRANSCRIPTS"


def codex_roots():
    """(dated_roots, flat_roots) for Codex rollout transcripts; env override replaces defaults."""
    override = os.environ.get(CODEX_ENV)
    if override:
        flat = [Path(part).expanduser() for part in override.split(":") if part.strip()]
        return [], flat
    dated = [Path.home() / ".codex" / "sessions", Path.home() / ".codex-personal" / "sessions"]
    flat = [Path.home() / ".codex-personal" / "archived_sessions"]
    return dated, flat


def codex_candidate_files(wall_start, wall_end):
    """Rollout files inside the run window (date dirs for dated roots; flat roots unfiltered)."""
    dated, flat = codex_roots()
    seen = set()

    def unique(path):
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            return None
        seen.add(key)
        return path

    if wall_start:
        start = (wall_start - timedelta(days=1)).date()
        upper = (wall_end if wall_end else datetime.now(timezone.utc))
        end = upper.date() + timedelta(days=1)
        day = start
        while day <= end:
            rel = f"{day.year:04d}/{day.month:02d}/{day.day:02d}"
            for root in dated:
                if not root.is_dir():
                    continue
                for path in sorted((root / rel).glob("rollout-*.jsonl")):
                    item = unique(path)
                    if item:
                        yield item
            day += timedelta(days=1)
    for root in flat:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("rollout-*.jsonl")):
            item = unique(path)
            if item:
                yield item


def codex_session_meta(path):
    """First session_meta payload of a rollout file; None if absent/malformed."""
    for record in records(path):
        if isinstance(record, dict) and record.get("type") == "session_meta":
            payload = record.get("payload")
            return payload if isinstance(payload, dict) else None
    return None


def build_codex_index(wall_start, wall_end):
    """One streaming pass: thread metadata, per-request token_count events, and turn contexts."""
    threads = []
    for path in codex_candidate_files(wall_start, wall_end):
        meta = codex_session_meta(path)
        if not meta:
            continue
        thread_id = meta.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            continue
        start = at(meta.get("timestamp"))
        if wall_end and start and start > wall_end:
            continue
        token_events = []
        turn_contexts = []
        context_samples = []
        for record in records(path):
            if not isinstance(record, dict) or record.get("_invalid"):
                continue
            rtype = record.get("type")
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            if rtype == "turn_context":
                model = payload.get("model")
                if isinstance(model, str) and model:
                    turn_contexts.append((at(record.get("timestamp")), model))
            elif rtype == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                total = info.get("total_token_usage")
                if not isinstance(total, dict):
                    continue
                last = info.get("last_token_usage")
                window = info.get("model_context_window")
                used = last.get("input_tokens") if isinstance(last, dict) else None
                moment = at(record.get("timestamp"))
                if moment and type(window) is int and window > 0 and type(used) is int and 0 <= used <= window:
                    context_samples.append({"at": moment.isoformat(), "input_tokens": used, "window_tokens": window})
                token_events.append((at(record.get("timestamp")),
                                     last if isinstance(last, dict) else None, total))
        threads.append({"path": path, "thread_id": thread_id, "session_id": meta.get("session_id"),
                        "parent_thread_id": meta.get("parent_thread_id"),
                        "forked_from_id": meta.get("forked_from_id"),
                        "history_mode": meta.get("history_mode"), "agent_path": meta.get("agent_path"),
                        "cwd": meta.get("cwd"), "thread_source": meta.get("thread_source"), "start": start,
                        "token_events": token_events, "turn_contexts": turn_contexts, "context_samples": context_samples})
    return threads


def codex_usage_sums(rows, models):
    """Map Codex per-request usage to USAGE_FIELDS; missing/invalid counters are null."""
    values = {f: [] for f in USAGE_FIELDS}
    reasons = []
    if not rows:
        reasons.append("No per-request usage records for thread")
    for response_id, moment, usage in rows:
        if not isinstance(usage, dict):
            reasons.append(f"Missing usage record: {response_id}")
            for f in USAGE_FIELDS:
                values[f].append(None)
            continue
        inp = usage.get("input_tokens")
        cached = usage.get("cached_input_tokens")
        write = usage.get("cache_write_input_tokens")
        out = usage.get("output_tokens")
        fresh = None
        if type(inp) is int and inp >= 0 and type(cached) is int and cached >= 0:
            fresh = max(0, inp - cached)
        else:
            if type(inp) is not int or inp < 0:
                reasons.append(f"Invalid/missing input_tokens: {response_id}")
            if type(cached) is not int or cached < 0:
                reasons.append(f"Invalid/missing cached_input_tokens: {response_id}")
        out_val = out if type(out) is int and out >= 0 else None
        if out_val is None:
            reasons.append(f"Invalid/missing output_tokens: {response_id}")
        cached_val = cached if type(cached) is int and cached >= 0 else None
        write_val = write if type(write) is int and write >= 0 else None
        if write_val is None:
            reasons.append(f"Invalid/missing cache_write_input_tokens: {response_id}")
        values["input_tokens"].append(fresh)
        values["output_tokens"].append(out_val)
        values["cache_read_input_tokens"].append(cached_val)
        values["cache_creation_input_tokens"].append(write_val)
    if len(models) == 0:
        reasons.append("Unknown model (no turn_context record)")
    sums = {f: sum(values[f]) if values[f] and all(v is not None for v in values[f]) else None for f in USAGE_FIELDS}
    complete = all(v is not None for v in sums.values()) and not reasons
    return sums, sorted(models), len(rows), complete, sorted(set(reasons))


def codex_parent_cumulative(parent, fork_ts):
    """Parent's largest cumulative total_tokens at or before the child's fork."""
    best = None
    for ts, _last, total in parent["token_events"]:
        value = total.get("total_tokens")
        if type(value) is not int or value < 0:
            continue
        if fork_ts is not None and ts is not None and ts > fork_ts:
            continue
        best = value if best is None else max(best, value)
    return best


def codex_earliest_batch_max(events):
    """Fallback fork baseline: the largest cumulative total_tokens in the earliest timestamp batch."""
    timed = [(ts, total.get("total_tokens")) for ts, _last, total in events
             if ts is not None and type(total.get("total_tokens")) is int and total["total_tokens"] >= 0]
    if not timed:
        return None
    earliest = min(ts for ts, _ in timed)
    return max(value for ts, value in timed if ts == earliest)


def codex_thread_baseline(thread, threads_by_id):
    """(baseline, reason). A fork replays its parent's cumulative history; roots and fresh threads start at zero."""
    parent_id = thread.get("parent_thread_id")
    if not parent_id:
        return 0, None
    is_fork = thread.get("history_mode") == "legacy" or bool(thread.get("forked_from_id"))
    if not is_fork:
        return 0, None
    parent = threads_by_id.get(parent_id)
    if parent is None:
        fallback = codex_earliest_batch_max(thread["token_events"])
        if fallback is None:
            return None, f"No parent rollout {parent_id!r} and no cumulative token_count to derive a fork baseline"
        return fallback, None
    baseline = codex_parent_cumulative(parent, thread.get("start"))
    if baseline is None:
        return 0, None
    return baseline, None


def codex_thread_stream(thread, baseline):
    """Real (post-baseline, advancing) requests plus a segment-aware cumulative cross-check."""
    real, reasons = [], []
    per_request = 0
    cumulative_delta = 0
    valid = True
    floor = baseline
    prev = None
    last_real = None
    last_cum = None
    for index, (moment, last, total) in enumerate(thread["token_events"]):
        value = total.get("total_tokens")
        if type(value) is not int or value < 0:
            continue
        last_cum = value
        if prev is not None and value < prev:
            floor = 0
            last_real = None
        prev = value
        if value <= floor:
            continue
        if last_real is None:
            cumulative_delta += value - floor
            real.append((index, moment, last, total))
            last_real = value
        elif value > last_real:
            cumulative_delta += value - last_real
            real.append((index, moment, last, total))
            last_real = value
    for _index, _moment, last, _total in real:
        total_value = last.get("total_tokens") if isinstance(last, dict) else None
        if type(total_value) is int and total_value >= 0:
            per_request += total_value
        else:
            valid = False
    if last_cum is None:
        reasons.append("No cumulative token_count total for thread")
    if not real:
        reasons.append("No post-baseline token_count requests for thread")
    equal = valid and per_request == cumulative_delta
    reason = None if equal else f"Per-request total {per_request if valid else 'unknown'} != cumulative delta {cumulative_delta}"
    if not equal:
        reasons.append(reason)
    return {"baseline": baseline, "real": real, "reasons": reasons,
            "crosscheck": {"available": True, "equal": equal,
                           "per_request_total": per_request if valid else None,
                           "cumulative_delta": cumulative_delta, "baseline": baseline,
                           "reason": reason}}


def codex_model_at(moment, contexts):
    """Model from the last turn_context at or before a request timestamp."""
    if moment is None:
        return None
    chosen = None
    for ts, model in contexts:
        if ts is None or ts > moment:
            break
        chosen = model
    return chosen


def codex_resolve_tool_call(path, tool_use_id):
    """Return the child agent_path linked to a spawn_agent/followup_task tool call, or None."""
    call_id = None
    for record in records(path):
        if not isinstance(record, dict) or record.get("_invalid"):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or record.get("type") != "response_item":
            continue
        if payload.get("type") == "function_call" and payload.get("name") in ("spawn_agent", "followup_task"):
            if payload.get("call_id") == tool_use_id or payload.get("id") == tool_use_id:
                call_id = payload.get("call_id") or payload.get("id")
                break
    if not call_id:
        return None
    for record in records(path):
        if not isinstance(record, dict) or record.get("_invalid"):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or record.get("type") != "response_item":
            continue
        if payload.get("type") == "function_call_output" and payload.get("call_id") == call_id:
            output = payload.get("output")
            if isinstance(output, str):
                try:
                    parsed = json.loads(output)
                except ValueError:
                    return None
                if isinstance(parsed, dict) and isinstance(parsed.get("task_name"), str):
                    return parsed["task_name"]
    return None


def codex_join(project, phases, wall_start, wall_end, meta, tool_use_by_phase=None, workspace=None):
    """Codex rollout join: exact thread attribution, per-request usage, fork baseline, window split."""
    here = Path(__file__).resolve()
    workspace = workspace or (here.parents[3] if (here.parents[3] / ".agents").is_dir() else here.parents[2])
    tool_use_by_phase = tool_use_by_phase or {}
    declared = list(meta.get("session_ids") or ([meta.get("session_id")] if meta.get("session_id") else []))
    for phase in phases:
        session_id = phase.get("session_id")
        if isinstance(session_id, str) and session_id:
            declared.append(session_id)
    declared = sorted({s for s in declared if isinstance(s, str) and re.fullmatch(r"[A-Za-z0-9_-]+", s)})
    threads = build_codex_index(wall_start, wall_end)
    threads_by_id = {}
    for t in threads:
        threads_by_id.setdefault(t["thread_id"], t)
    available = bool(threads)
    children = {}
    all_roots = []
    root_seen = set()
    for t in threads:
        ap = t["agent_path"]
        parent = t["parent_thread_id"]
        if ap:
            children.setdefault((ap, parent), []).append(t)
        elif t["thread_source"] == "user" and t["cwd"] == str(workspace):
            if t["thread_id"] not in root_seen:
                root_seen.add(t["thread_id"])
                all_roots.append(t)

    def subtree_of(root):
        tree = {root["thread_id"]}
        frontier = [root["thread_id"]]
        while frontier:
            nxt = []
            for parent in frontier:
                for (_ap, par), lst in children.items():
                    if par == parent:
                        for child in lst:
                            if child["thread_id"] not in tree:
                                tree.add(child["thread_id"])
                                nxt.append(child["thread_id"])
            frontier = nxt
        return tree

    all_subtrees = {root["thread_id"]: subtree_of(root) for root in all_roots}
    declared_roots = [t for t in all_roots
                      if t["thread_id"] in declared or t.get("session_id") in declared]
    notes = []
    if declared and not declared_roots:
        notes.append("No Codex root conversation matches declared session ids in run window")
    phase_rows = {}
    for phase in [p for p in phases if p["phase"] in AGENT_PHASES]:
        aid = phase.get("agent_id")
        session_id = phase.get("session_id")
        session_id = session_id if isinstance(session_id, str) and re.fullmatch(r"[A-Za-z0-9_-]+", session_id) else ""
        subtrees = all_subtrees
        if session_id:
            matched = [r for r in all_roots
                       if r["thread_id"] == session_id or r.get("session_id") == session_id]
            if len(matched) == 1:
                subtrees = {matched[0]["thread_id"]: all_subtrees[matched[0]["thread_id"]]}
            elif len(matched) > 1:
                phase["usage_reasons"] = [f"Ambiguous Codex root match for session_id: {session_id}"]
                continue
            else:
                notes.append(f"Phase {phase['phase_id']} session_id {session_id!r} matches no Codex root; searched all roots")
        candidates = []
        if isinstance(aid, str) and aid:
            for subtree in subtrees.values():
                for parent in subtree:
                    candidates.extend(children.get((aid, parent), []))
        if not candidates and tool_use_by_phase.get(phase["phase_id"]):
            resolved_paths = []
            for root in all_roots:
                resolved_path = codex_resolve_tool_call(root["path"], tool_use_by_phase[phase["phase_id"]])
                if resolved_path and resolved_path not in resolved_paths:
                    resolved_paths.append(resolved_path)
            if len(resolved_paths) == 1:
                for subtree in all_subtrees.values():
                    for parent in subtree:
                        candidates.extend(children.get((resolved_paths[0], parent), []))
            elif len(resolved_paths) > 1:
                phase["usage_reasons"] = [f"Ambiguous Codex agent_path match: {aid}"]
                continue
        if len(candidates) == 1:
            phase_rows[phase["phase_id"]] = candidates[0]
        elif len(candidates) > 1:
            phase["usage_reasons"] = [f"Ambiguous Codex agent_path match: {aid}"]
        elif not all_roots:
            phase["usage_reasons"] = ["No Codex orchestrator identified; cannot attribute phase usage"]
        else:
            phase["usage_reasons"] = [f"No Codex child transcript matches agent_id {aid!r} in run roots"]
    stream = {}
    for t in threads:
        baseline, base_reason = codex_thread_baseline(t, threads_by_id)
        contexts = sorted([(ts, m) for ts, m in t["turn_contexts"] if ts is not None], key=lambda item: item[0])
        if baseline is None:
            state = {"baseline": None, "real": [], "reasons": [base_reason] if base_reason else [],
                     "crosscheck": {"available": False, "equal": None, "reason": base_reason, "baseline": None}}
        else:
            state = codex_thread_stream(t, baseline)
        state["contexts"] = contexts
        stream[t["thread_id"]] = state
    matched_agent_threads = {t["thread_id"] for t in phase_rows.values()}
    phases_of_thread = {}
    for phase in [p for p in phases if p["phase"] in AGENT_PHASES]:
        thread = phase_rows.get(phase["phase_id"])
        if thread is None:
            continue
        first, last = at(phase.get("start_at")), at(phase.get("end_at"))
        if first is None or last is None:
            continue
        phases_of_thread.setdefault(thread["thread_id"], []).append((phase, first, last))
    phase_events = {p["phase_id"]: [] for p in phases if p["phase"] in AGENT_PHASES}
    for tid, plist in phases_of_thread.items():
        for event in stream.get(tid, {}).get("real", []):
            moment = event[1]
            matches = [p for (p, first, last) in plist if moment is not None and first <= moment <= last]
            if len(matches) == 1:
                phase_events[matches[0]["phase_id"]].append(event)
    for phase in [p for p in phases if p["phase"] in AGENT_PHASES]:
        thread = phase_rows.get(phase["phase_id"])
        if thread is None:
            if not phase["usage_reasons"]:
                phase["usage_reasons"] = ["Phase not correlated to a Codex thread"]
            continue
        state = stream[thread["thread_id"]]
        events = phase_events.get(phase["phase_id"], [])
        reasons = list(state["reasons"])
        if not events:
            sums = {field: None for field in USAGE_FIELDS}
            models, count, complete = [], 0, False
            reasons.append("No token_count requests inside phase window; absence is not zero usage")
        else:
            resolved = [codex_model_at(event[1], state["contexts"]) for event in events]
            models = sorted({m for m in resolved if m})
            if any(m is None for m in resolved):
                reasons.append("Unknown model for some requests (no turn_context at or before request)")
            rows = [(f"{thread['thread_id']}#{event[0]}", event[1], event[2]) for event in events]
            sums, models, count, complete, usage_reasons = codex_usage_sums(rows, models)
            reasons.extend(usage_reasons)
            if any(m is None for m in resolved):
                complete = False
            if not state["crosscheck"].get("equal", False):
                complete = False
        phase["transcript"] = {"usage": sums, "models": models, "requests": count,
                               "complete": complete, "reasons": reasons or []}
        phase["ocx"] = {"source": "codex per-request usage (token_count last_usage; fork-baseline; window attribution)",
                        "usage": sums, "models": models, "requests": count, "complete": complete,
                        "reasons": reasons, "thread_id": thread["thread_id"], "agent_path": thread["agent_path"],
                        "baseline": state["baseline"],
                        "vs_transcript": {"requests_equal": True, "transcript_output_tokens": sums["output_tokens"],
                                          "output_tokens_equal": True, "disagreements": {}},
                        "crosscheck": state["crosscheck"]}
        phase.update(usage=sums, models=models, requests=count,
                     usage_source="codex per-request usage (token_count last_usage; fork-baseline; window attribution)",
                     usage_complete=complete, usage_reasons=reasons,
                     correlation_complete=True,
                     accounting_complete=phase.get("event_valid", True) and complete)
    buckets = {k: set() for k in ("in_phase", "in_run_outside_phases", "outside_run", "unknown_time", "ambiguous")}
    thread_buckets = {t["thread_id"]: {k: set() for k in buckets} for t in threads}
    for t in threads:
        tid = t["thread_id"]
        plist = phases_of_thread.get(tid, [])
        for event in stream.get(tid, {}).get("real", []):
            event_id = f"{tid}#{event[0]}"
            moment = event[1]
            if moment is None:
                bucket = "unknown_time"
            elif (wall_start and moment < wall_start) or (wall_end and moment > wall_end):
                bucket = "outside_run"
            else:
                matches = [p for (p, first, last) in plist if first <= moment <= last]
                if len(matches) == 1:
                    bucket = "in_phase"
                elif len(matches) > 1:
                    bucket = "ambiguous"
                else:
                    bucket = "in_run_outside_phases"
            buckets[bucket].add(event_id)
            thread_buckets[tid][bucket].add(event_id)
    coverage = {k: {"requests": len(v), "request_ids": sorted(v)} for k, v in buckets.items()}
    agents = {}
    for t in threads:
        tid = t["thread_id"]
        if tid not in matched_agent_threads:
            continue
        state = stream[tid]
        real = state["real"]
        in_window = [e for e in real if wall_start and wall_end and e[1] is not None and wall_start <= e[1] <= wall_end]
        outside_phases = [e for e in in_window
                          if not any(first <= e[1] <= last for (_p, first, last) in phases_of_thread.get(tid, []))]
        resolved = [codex_model_at(e[1], state["contexts"]) for e in real]
        models = sorted({m for m in resolved if m})
        total_sums, total_models, total_count, total_complete, total_reasons = codex_usage_sums(
            [(f"{tid}#{e[0]}", e[1], e[2]) for e in real], models)
        in_sums, _, in_count, in_complete, in_reasons = codex_usage_sums(
            [(f"{tid}#{e[0]}", e[1], e[2]) for e in in_window], models)
        out_sums, _, out_count, out_complete, out_reasons = codex_usage_sums(
            [(f"{tid}#{e[0]}", e[1], e[2]) for e in outside_phases], models)
        tb = thread_buckets.get(tid, {k: set() for k in buckets})
        label = t["agent_path"] or tid
        agents[label] = {"thread_id": tid, "agent_path": t["agent_path"], "parent_thread_id": t["parent_thread_id"],
                         "total": {**total_sums, "requests": total_count, "models": total_models,
                                   "complete": total_complete, "reasons": total_reasons},
                         "transcript_wide_total": {**total_sums, "requests": total_count, "models": total_models,
                                                   "complete": total_complete, "reasons": total_reasons},
                         "in_run": {**in_sums, "requests": in_count, "models": models,
                                    "complete": in_complete, "reasons": in_reasons},
                         "outside_recorded_phases": {**out_sums, "requests": out_count, "models": models,
                                                     "complete": out_complete, "reasons": out_reasons},
                         "buckets": {k: len(v) for k, v in tb.items()},
                         "outside_run": {"requests": len(tb.get("outside_run", set())), "complete": in_complete, "reasons": []},
                         "unknown_time": {"requests": len(tb.get("unknown_time", set())), "complete": in_complete, "reasons": []},
                         "ambiguous": {"requests": len(tb.get("ambiguous", set())), "complete": in_complete, "reasons": []}}
    phase_bound_roots = [root for root in all_roots
                         if all_subtrees[root["thread_id"]] & matched_agent_threads]
    run_roots = []
    run_root_seen = set()
    for root in declared_roots + phase_bound_roots:
        if root["thread_id"] not in run_root_seen:
            run_root_seen.add(root["thread_id"])
            run_roots.append(root)
    run_roots.sort(key=lambda root: root["thread_id"])
    run_root_ids = {root["thread_id"] for root in run_roots}
    run_subtrees = set().union(*(all_subtrees[tid] for tid in run_root_ids)) if run_root_ids else set()
    orchestrator_usage = None
    if run_roots:
        per_root = []
        for root in run_roots:
            state = stream[root["thread_id"]]
            rows_in_window = [e for e in state["real"]
                              if wall_start and wall_end and e[1] is not None and wall_start <= e[1] <= wall_end]
            resolved = [codex_model_at(e[1], state["contexts"]) for e in rows_in_window]
            models = sorted({m for m in resolved if m})
            sums, rmodels, rcount, rcomplete, rreasons = codex_usage_sums(
                [(f"{root['thread_id']}#{e[0]}", e[1], e[2]) for e in rows_in_window], models)
            if any(m is None for m in resolved):
                rreasons = sorted(set(rreasons) | {"Unknown model for some requests (no turn_context at or before request)"})
                rcomplete = False
            per_root.append((root, sums, rmodels, rcount, rcomplete, rreasons))
        sums = {}
        for field in USAGE_FIELDS:
            values = [item[1][field] for item in per_root]
            sums[field] = sum(values) if values and all(v is not None for v in values) else None
        orchestrator_usage = {
            **sums,
            "requests": sum(item[3] for item in per_root),
            "models": sorted({m for item in per_root for m in item[2]}),
            "complete": all(item[4] for item in per_root),
            "reasons": sorted({reason for item in per_root for reason in item[5]}),
            "roots": [{"thread_id": root["thread_id"], "session_id": root.get("session_id"),
                       **root_sums, "requests": root_count, "models": root_models,
                       "complete": root_complete, "reasons": root_reasons,
                       "context": next((sample for sample in reversed(root.get("context_samples", []))
                                        if wall_start and at(sample["at"]) >= wall_start
                                        and (not wall_end or at(sample["at"]) <= wall_end)), None)}
                      for root, root_sums, root_models, root_count, root_complete, root_reasons in per_root],
        }
    other_sessions = {}
    uncorrelated = {}
    for t in threads:
        tid = t["thread_id"]
        if tid in matched_agent_threads or tid in run_root_ids:
            continue
        state = stream[tid]
        rows_in_window = [e for e in state["real"]
                          if wall_start and wall_end and e[1] is not None and wall_start <= e[1] <= wall_end]
        if not rows_in_window:
            continue
        resolved = [codex_model_at(e[1], state["contexts"]) for e in rows_in_window]
        models = sorted({m for m in resolved if m})
        sums, smodels, count, complete, reasons = codex_usage_sums(
            [(f"{tid}#{e[0]}", e[1], e[2]) for e in rows_in_window], models)
        bucket = {**sums, "requests": count, "models": smodels, "complete": complete, "reasons": reasons}
        if run_roots and t.get("agent_path") and t.get("parent_thread_id") in run_subtrees:
            uncorrelated[t["agent_path"] or tid] = bucket
        elif t["thread_source"] == "user" and t["cwd"] != str(workspace):
            other_sessions[t["session_id"] or tid] = bucket
    notes = list(dict.fromkeys(notes))
    return {"available": available,
            "reason": None if available else f"no Codex rollout transcripts found in {os.environ.get(CODEX_ENV) or 'default dirs'}",
            "source": (os.environ.get(CODEX_ENV) or "~/.codex/sessions, ~/.codex-personal/sessions, ~/.codex-personal/archived_sessions"),
            "join": "codex thread/agent exact join (agent_path + parent_thread_id)",
            "agents": agents, "orchestrator": orchestrator_usage,
            "coverage": coverage,
            "other_sessions_in_window": other_sessions,
            "uncorrelated_agents_in_window": uncorrelated,
            "unattributed_in_window": {"requests": 0, "complete": True, "reasons": [], "note": "Every Codex request belongs to a rollout thread"},
            "notes": notes,
            "scope": "tokens only (USD unavailable); phase usage is per-request and window-scoped, orchestrator/coverage are window-scoped"}


def seal_audit(seals):
    """Read-only audit, keeping missing, unreadable, malformed and mismatched distinct."""
    result = {"reference_count": len(seals) if isinstance(seals, dict) else 0,
              "missing": [], "unreadable": [], "hash_mismatch": [], "malformed": []}
    if not isinstance(seals, dict) or not seals:
        result["malformed"].append({"path": None, "reason": "Missing or malformed seal map"})
    else:
        for path, expected in seals.items():
            item = {"path": path, "expected_sha256": expected, "actual_sha256": None}
            if not isinstance(path, str) or not Path(path).is_absolute() or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                result["malformed"].append(item)
                continue
            try:
                item["actual_sha256"] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            except FileNotFoundError:
                result["missing"].append(item)
            except OSError as exc:
                result["unreadable"].append({**item, "reason": type(exc).__name__})
            else:
                if item["actual_sha256"] != expected:
                    result["hash_mismatch"].append(item)
    result["intact"] = not any(result[k] for k in ("missing", "unreadable", "hash_mismatch", "malformed"))
    return result


def phase_inputs(path):
    """Only recognized unversioned CLI inputs are excluded, not an entire prefix."""
    value = read(path)
    suffix = "-start.json" if path.name.endswith("-start.json") else "-end.json"
    return (isinstance(value, dict) and path.name.startswith("in-") and
            value.get("schema_version") is None and value.get("event") in {"phase-start", "phase-end"} and
            isinstance(value.get("phase_id"), str) and path.name != value["phase_id"] + suffix and
            isinstance(value.get("run_id"), str) and isinstance(value.get("workflow"), str))


def analyze(run_dir, project, session_override=None, workspace=None):
    meta = read(run_dir / "run.json") or {}
    run_id = meta.get("run_id", run_dir.name)
    contract = read(run_dir / "contract.json") or {}
    directory = run_dir / "telemetry"
    if contract.get("schema_version") != 2:
        return None
    warnings, phases, intervals = [], [], []
    observed_ends = []
    ocx_jobs = []
    tool_use_by_phase = {}
    sessions = [session_override] if session_override else (meta.get("session_ids") or [meta.get("session_id")])
    sessions = [s for s in sessions if isinstance(s, str) and re.fullmatch(r"[A-Za-z0-9_-]+", s)]
    starts = [p for p in directory.glob("*-start.json") if not phase_inputs(p)]
    event_errors = []
    completion = read(run_dir / "completion.json") or {}
    declared_start, declared_end = at(meta.get("started_at")), at(completion.get("at"))
    for path in sorted(starts, key=lambda p: p.name):
        start = read(path)
        if not isinstance(start, dict):
            event_errors.append(f"Malformed phase start: {path.name}")
            continue
        phase_id = start.get("phase_id")
        if not isinstance(phase_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", phase_id) or phase_id in {".", ".."}:
            event_errors.append(f"Invalid phase ID: {path.name}")
            continue
        if path.name != phase_id + "-start.json":
            event_errors.append(f"Noncanonical phase filename: {path.name}")
            continue
        end_path = directory / f"{phase_id}-end.json"
        end = read(end_path)
        end = end if isinstance(end, dict) else {}
        first, last = at(start.get("at")), at(end.get("at"))
        valid = first is not None and last is not None and last >= first
        valid = valid and all(start.get(k) == end.get(k) for k in ("run_id", "workflow", "phase", "repo", "phase_id"))
        valid = valid and start.get("schema_version") == 2 and end.get("schema_version") == 2
        valid = valid and start.get("event") == "phase-start" and end.get("event") == "phase-end"
        valid = valid and end.get("status") in {"passed", "failed", "blocked", "interrupted"}
        valid = valid and start.get("run_id") == run_id and start.get("workflow") == contract.get("workflow")
        valid = valid and end.get("start_sha256") == hashlib.sha256(path.read_bytes()).hexdigest()
        correlated_pair = valid
        if correlated_pair:
            observed_ends.append(last)
        if valid and ((declared_start and first < declared_start) or (declared_end and last > declared_end)):
            valid = False
            event_errors.append(f"Phase envelope outside declared run boundaries: {phase_id}")
        if not valid:
            event_errors.append(f"Incomplete/invalid phase: {phase_id}")
        seconds = (last - first).total_seconds() if valid else None
        if valid:
            intervals.append((first, last))
        phase = {"phase_id": phase_id, "phase": start.get("phase"), "repo": start.get("repo"),
                 "agent_id": end.get("agent_id") or start.get("agent_id"), "round": start.get("round"),
                 "session_id": end.get("session_id") or start.get("session_id"),
                 "status": end.get("status") if valid else "unknown", "duration_seconds": seconds,
                 "end_reason": end.get("end_reason") if valid else None,
                 "checkpoint_path": end.get("checkpoint_path") if valid else None,
                 "usage": {field: None for field in USAGE_FIELDS}, "models": [], "requests": None,
                 "accounting_complete": False, "usage_source": None, "event_valid": valid,
                 "correlation_complete": False, "usage_complete": False, "usage_reasons": [],
                 "start_at": start.get("at"), "end_at": end.get("at") if valid else None}
        tool_use_by_phase[phase_id] = end.get("tool_use_id") or start.get("tool_use_id")
        candidates = []
        requested_session = end.get("session_id") or start.get("session_id")
        for session in ([requested_session] if requested_session in sessions else sessions):
            transcript, identifier = agent_transcript(project, session, start, end)
            if transcript:
                candidates.append((transcript, identifier))
        if correlated_pair and len(candidates) == 1:
            transcript, identifier = candidates[0]
            phase.update(agent_id=identifier, correlation_complete=True)
            ocx_jobs.append((phase, transcript, first, last))
        elif start.get("phase") not in AGENT_PHASES:
            # Command and user-wait phases have time, not artificial zero LLM usage.
            phase["accounting_complete"] = valid
        phases.append(phase)
    for path in directory.glob("*-end.json"):
        if phase_inputs(path):
            continue
        value = read(path)
        if not isinstance(value, dict) or path.name != str(value.get("phase_id")) + "-end.json":
            event_errors.append(f"Malformed/noncanonical phase end: {path.name}")
        if not path.with_name(path.name.removesuffix("-end.json") + "-start.json").is_file():
            event_errors.append(f"Orphan phase end: {path.name}")
    if not starts:
        event_errors.append("Versioned run has no phase telemetry")
    warnings.extend(event_errors)
    phases.sort(key=lambda p: (p["start_at"] or "", p["phase_id"]))
    llm_phases = [p for p in phases if p["phase"] in AGENT_PHASES]
    wall_start = declared_start or min((x[0] for x in intervals), default=None)
    wall_end = at(completion.get("at")) or max(observed_ends, default=None)
    elapsed = (wall_end - wall_start).total_seconds() if wall_start and wall_end and wall_end >= wall_start else None
    covered = union_seconds(intervals)
    summed = sum(p["duration_seconds"] or 0 for p in phases)
    repo_status = {}
    for repo in contract.get("repos", []):
        states = []
        for path in (run_dir / repo).glob("state-*.json"):
            match = re.fullmatch(r"state-(\d+)\.json", path.name)
            item = read(path)
            if match and isinstance(item, dict):
                states.append((int(match[1]), item))
        latest = max(states, default=(0, {}))[1]
        audit = seal_audit(latest.get("artifact_hashes"))
        intact = audit["intact"]
        repo_status[repo] = {"status": latest.get("status"), "recorded_status": latest.get("status"),
                            "recorded_goals_verified": latest.get("status") == "verified",
                            "recorded_review_clean": latest.get("status") in {"reviewed", "gates-passed", "verified"},
                            "artifacts_intact": intact, "seal_audit": audit,
                            "goals_verified": intact and latest.get("status") == "verified",
                            "review_clean": intact and latest.get("status") in {"reviewed", "gates-passed", "verified"}}
    completion_audit = seal_audit(completion.get("artifact_hashes"))
    completion_valid = completion_audit["intact"] and completion.get("run_id") == run_id and completion.get("status") == "completed"
    if completion and not completion_valid:
        warnings.append("Completion certificate missing or altered artifacts")
    if not ocx_jobs and not any((project / f"{session}.jsonl").is_file() for session in sessions):
        ocx = codex_join(project, phases, wall_start, wall_end, meta, tool_use_by_phase, workspace)
    else:
        ocx = ocx_join(project, sessions, ocx_jobs, wall_start, wall_end)
    for phase in llm_phases:
        if not phase.get("correlation_complete"):
            warnings.append(f"Usage unavailable or ambiguous: {phase['phase_id']}")
    totals = {f: sum(p["usage"][f] for p in llm_phases) if llm_phases and all(p["usage"][f] is not None for p in llm_phases) else None for f in USAGE_FIELDS}
    event_complete = bool(phases) and not event_errors
    correlation_complete = bool(llm_phases) and all(p["correlation_complete"] for p in llm_phases)
    usage_complete = bool(llm_phases) and all(p["usage_complete"] for p in llm_phases)
    coverage = ocx["coverage"]
    coverage_reasons = [f"{coverage[k]['requests']} {k} requests" for k in ("in_run_outside_phases", "unknown_time", "ambiguous") if coverage[k]["requests"]]
    if not at(completion.get("at")):
        coverage_reasons.append("Run end unknown; only a bounded observation window is available")
    completeness = {
        "events": {"complete": event_complete, "reasons": event_errors},
        "correlation": {"complete": correlation_complete, "reasons": [p["phase_id"] for p in llm_phases if not p["correlation_complete"]]},
        "usage": {"complete": usage_complete, "reasons": [p["phase_id"] for p in llm_phases if not p["usage_complete"]]},
        "coverage": {"complete": False, "reasons": coverage_reasons + ["Recorded-phase accounting does not prove the whole API bill"]}}
    wall_end_known = at(completion.get("at")) is not None
    return {
        "ocx": ocx,
        "source": "versioned run phase events", "phases": phases, "usage": totals,
        "accounting_complete": event_complete and correlation_complete and usage_complete,
        "completeness": completeness, "coverage": coverage,
        "window": {"start": wall_start.isoformat() if wall_start else None,
                   "end": wall_end.isoformat() if wall_end else None, "complete": wall_end_known,
                   "kind": "completed-run" if wall_end_known else "bounded-observation"},
        "recorded_completion": {"status": completion.get("status"), "at": completion.get("at")},
        "integrity": {"completion": completion_audit},
        "accounting_scope": "recorded agent phases; main orchestrator requests are not attributed",
        "run_accounting_complete": False,
        "run_completed": completion_valid, "repo_status": repo_status,
        "timing": {"elapsed_seconds": elapsed if wall_end_known else None,
                   "observation_seconds": elapsed, "elapsed_complete": wall_end_known,
                   "envelope_sum_seconds": sum(p["duration_seconds"] or 0 for p in llm_phases),
                   "agent_service_semantics": "dispatch-to-result envelopes, not pure model execution",
                   "agent_service_seconds": sum(p["duration_seconds"] or 0 for p in llm_phases),
                   "gate_seconds": sum(p["duration_seconds"] or 0 for p in phases if p["phase"] in {"gates", "pre-review"}),
                   "user_wait_seconds": sum(p["duration_seconds"] or 0 for p in phases if p["phase"] == "checkpoint-wait"),
                   "covered_seconds": covered, "overlap_seconds": max(0, summed - covered),
                   "unclassified_seconds": max(0, elapsed - covered) if elapsed is not None else None},
        "warnings": warnings,
    }
