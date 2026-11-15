"""Compact presentation of analyzer evidence, without changing its conclusions."""


def number(value):
    if value is None:
        return "неизвестно"
    if isinstance(value, float):
        return f"{value:,.1f}"
    return f"{value:,}" if type(value) is int else str(value)


def cell(value):
    return str(value if value is not None else "—").replace("|", "\\|").replace("\n", " ")


def flag(value):
    return "да" if value is True else "нет" if value is False else "неизвестно"


def reconcile_accuracy(accuracy, telemetry):
    """Keep recorded outcomes, but qualify certification with current seal evidence."""
    result = {}
    for repo, assessment in accuracy.items():
        item = dict(assessment)
        audited = telemetry.get("repo_status", {}).get(repo, {})
        item["recorded_review_certified"] = item.get("review_certified")
        item["recorded_goals_verified"] = item.get("goals_verified")
        item["assessment_scope"] = "recorded history; certification qualified by current sealed references"
        item["artifacts_intact"] = audited.get("artifacts_intact")
        item["review_certified"] = item.get("review_certified") is True and audited.get("review_clean") is True
        item["goals_verified"] = item.get("goals_verified") is True and audited.get("goals_verified") is True
        result[repo] = item
    return result


def compact(report):
    lines = [f"# Анализ {cell(report.get('run_id') or report.get('session_id'))}"]
    telemetry = report.get("telemetry")
    if not isinstance(telemetry, dict):
        lines += ["", "Legacy / uncertified: частичные агрегаты сессии, не точный учёт запуска.",
                  report.get("accounting_note") or "Метрики могут включать другие запуски и пропускать resumed-работу."]
        if report.get("no_ship_note"):
            lines.append(report["no_ship_note"])
        for repo, assessment in report.get("accuracy", {}).items():
            lines.append(f"- {cell(repo)}: recorded convergence={flag(assessment.get('converged'))}; не сертификат completion.")
        lines += ["", "USD запуска недоступен: legacy session cost не является стоимостью этого запуска."]
        return "\n".join(lines) + "\n"

    lines += ["", f"Текущая валидность completion: **{flag(telemetry.get('run_completed'))}**.",
              "Проверены ссылки записанного сертификата, не актуальный код заново."]
    recorded = telemetry.get("recorded_completion")
    if isinstance(recorded, dict):
        lines.append(f"Записанный outcome: {cell(recorded.get('status'))}; at={cell(recorded.get('at'))}.")
    elif telemetry.get("recorded_completed") is not None:
        lines.append(f"Записанное completion: {flag(telemetry['recorded_completed'])}.")
    for repo, status in telemetry.get("repo_status", {}).items():
        lines.append(f"- {cell(repo)}: recorded={cell(status.get('recorded_status', status.get('status')))}, "
                     f"artifacts intact={flag(status.get('artifacts_intact'))}, "
                     f"recorded goals + valid seals={flag(status.get('goals_verified'))}.")

    insights = report.get("insights") or {}
    handoffs = insights.get("handoffs") or []
    if handoffs:
        lines += ["", "## Handoff и ограничения релиза", ""]
        for handoff in handoffs:
            lines.append(f"- Источник: {cell(handoff.get('path'))}; seal={cell(handoff.get('seal'))}; "
                         f"run ID={cell(handoff.get('run_identity'))}; время={cell(handoff.get('timing'))}.")
            for statement in handoff.get('release_statements', []):
                lines.append(f"  - Строка {cell(statement.get('line'))}: {cell(statement.get('text'))}")
            lines.append(f"  - {cell(handoff.get('limitation'))}")
    findings = insights.get("findings") or []
    if findings:
        lines += ["", "## История находок", ""]
        visible = [f for f in findings if f.get("scope") == "repo"]
        slice_findings = [f for f in findings if f.get("scope") != "repo"]
        if slice_findings:
            lines.append(f"- Slice-находок в JSON: {len(slice_findings)}; они перечислены отдельно от repo review.")
        for finding in visible:
            dispositions = finding.get("dispositions") or []
            status = ", ".join(str(item.get("status")) for item in dispositions) or "нет явного решения"
            lines.append(f"- {cell(finding.get('scope'))} / раунд {cell(finding.get('round'))} / {cell(finding.get('id'))}: "
                         f"{cell(finding.get('scenario') or finding.get('description') or 'сценарий не записан')} "
                         f"[источник: {cell(finding.get('source'))}; {cell(finding.get('seal'))}; решение: {cell(status)}].")
            for fix in finding.get("fixes", []):
                lines.append(f"  - Заявленное исправление ({cell(fix.get('seal'))}): {cell(fix.get('claim') or 'не описано')}; {cell(fix.get('path'))}.")
            for disposition in dispositions:
                lines.append(f"  - Решение reviewer ({cell(disposition.get('seal'))}; финальный fingerprint: {cell(disposition.get('final_fingerprint_binding'))}): {cell(disposition.get('reason') or disposition.get('status'))}; {cell(disposition.get('path'))}.")
                for proof in disposition.get('proof_inspection', []):
                    lines.append(f"    - Proof {cell(proof.get('path'))}: seal={cell(proof.get('seal'))}, identity={cell(proof.get('identity'))}, "
                                 f"claim={cell(proof.get('claim_match'))}, manifests={cell(proof.get('manifest_binding'))}, "
                                 f"hashes={cell(proof.get('embedded_hashes'))}, status={cell(proof.get('record_status'))}.")
    observations = insights.get("observations") or []
    if observations:
        lines += ["", "## Вопросы и следующие действия", ""]
        for item in observations[:8]:
            lines.append(f"- **{cell(item.get('category'))}**: {cell(item.get('question'))} {cell(item.get('action'))}")
            lines.append(f"  - Основание: {cell(item.get('finding_id') or item.get('claim'))}; "
                         f"{cell(item.get('evidence_level'))}; {cell(item.get('limitation'))}")
        if len(observations) > 8:
            lines.append(f"- Ещё {len(observations) - 8} наблюдений доступны в JSON.")
    for limitation in insights.get("limitations", []):
        lines.append(f"- Ограничение анализа: {cell(limitation)}")

    lines += ["", "## Фазы", "",
              "| Фаза / repo / раунд | Статус | Actual model | Источник | Fresh in | Output | Cache read | Cache write | Envelope, с | Usage полон | Учёт фазы полон |",
              "|---|---|---|---|---:|---:|---:|---:|---:|---|---|"]
    phase_notes = []
    for phase in telemetry.get("phases", []):
        usage = phase.get("usage") or {}
        label = " / ".join(str(x) for x in (phase.get("phase"), phase.get("repo"), phase.get("round")) if x is not None)
        applicable = phase.get("phase") in {"implement", "review-fix", "gate-fix", "review", "checker"}
        metric = lambda key: number(usage.get(key)) if applicable else "н/п"
        values = [label, phase.get("status"), (", ".join(phase.get("models") or []) or "неизвестно") if applicable else "—",
                  (phase.get("usage_source") or "неизвестно") if applicable else "не LLM-фаза", metric("input_tokens"),
                  metric("output_tokens"), metric("cache_read_input_tokens"), metric("cache_creation_input_tokens"),
                  number(phase.get("duration_seconds")),
                  flag(phase.get("usage_complete", phase.get("accounting_complete"))) if applicable else "н/п",
                  flag(phase.get("accounting_complete"))]
        lines.append("| " + " | ".join(cell(x) for x in values) + " |")
        if phase.get("status") in {"failed", "interrupted", "blocked"}:
            # Absence of a recorded reason is unknown, never inferred from duration.
            phase_notes.append(f"- {cell(phase.get('phase_id'))}: статус {cell(phase.get('status'))}, "
                               f"причина остановки: {cell(phase.get('end_reason') or 'не записана')}"
                               + (f", checkpoint: {cell(phase.get('checkpoint_path'))}" if phase.get("checkpoint_path") else "") + ".")
        reasons = phase.get("usage_reasons") or []
        if reasons:
            phase_notes.append(f"- {cell(phase.get('phase_id'))}: " + "; ".join(cell(r) for r in reasons) + ".")
        disagreement = (phase.get("ocx") or {}).get("vs_transcript") or {}
        if disagreement.get("output_tokens_equal") is False:
            phase_notes.append(f"- {cell(phase.get('phase_id'))}: output transcript={number(disagreement.get('transcript_output_tokens'))}, "
                               f"канонический output={number(usage.get('output_tokens'))}; источники расходятся.")
    lines += [""] + phase_notes

    lines += ["", "## Полнота и время"]
    for dimension, data in telemetry.get("completeness", {}).items():
        if isinstance(data, dict):
            reasons = "; ".join(cell(r) for r in data.get("reasons", []))
            lines.append(f"- {cell(dimension)}: {flag(data.get('complete'))}" + (f" — {reasons}" if reasons else ""))
    lines.append(f"- Recorded-phase accounting: {flag(telemetry.get('accounting_complete'))}; "
                 f"whole-run accounting: {flag(telemetry.get('run_accounting_complete'))}.")
    timing = telemetry.get("timing") or {}
    labels = {"elapsed_seconds": "Elapsed wall", "agent_service_seconds": "Сумма agent envelopes (не чистое время модели)",
              "gate_seconds": "Recorded gate/pre-review envelopes", "user_wait_seconds": "Recorded user-wait envelopes",
              "covered_seconds": "Покрыто interval union", "overlap_seconds": "Overlap",
              "unclassified_seconds": "Unclassified gaps"}
    for key, label in labels.items():
        lines.append(f"- {label}: {number(timing.get(key))} с.")
    if timing.get("observation_seconds") is not None and timing.get("elapsed_seconds") is None:
        lines.append(f"- Окно наблюдения (не длительность завершённого запуска): {number(timing['observation_seconds'])} с.")
    window = telemetry.get("window") or {}
    if window:
        lines.append(f"- Окно: {cell(window.get('kind'))}; {cell(window.get('start'))} → {cell(window.get('end'))}.")
    lines.append(f"- Область учёта: {telemetry.get('accounting_scope') or 'recorded phases only'}.")

    ocx = telemetry.get("ocx") or {}
    lines += ["", "## Пробелы и доказательства"]
    for name, bucket in telemetry.get("coverage", {}).items():
        if isinstance(bucket, dict):
            lines.append(f"- Coverage / {cell(name)}: {number(bucket.get('requests'))} requests.")
    orchestrator = ocx.get("orchestrator")
    if isinstance(orchestrator, dict):
        lines.append(f"- Оркестратор в окне запуска: {number(orchestrator.get('requests'))} requests; "
                     f"полнота usage={flag(orchestrator.get('complete'))}; отдельно от agent phases.")
        for reason in orchestrator.get("reasons", []):
            lines.append(f"  - {cell(reason)}")
    other = ocx.get("other_sessions_in_window", {})
    if other:
        lines.append(f"- Другие доказанные сессии: {number(sum(item.get('requests', 0) for item in other.values()))} requests; исключены из учёта запуска.")
    uncorrelated = ocx.get("uncorrelated_agents_in_window", {})
    if uncorrelated:
        lines.append(f"- Агенты той же сессии без связи с запуском: {number(sum(item.get('requests', 0) for item in uncorrelated.values()))} requests; исключены из учёта запуска.")
    unattributed = ocx.get("unattributed_in_window")
    if isinstance(unattributed, dict):
        lines.append(f"- Неатрибутированные запросы в окне: {number(unattributed.get('requests'))}; исключены, даже при общем conversationId.")
    for agent, data in ocx.get("agents", {}).items():
        for key in ("outside_recorded_phases", "outside_run", "unknown_time", "ambiguous"):
            bucket = data.get(key)
            if isinstance(bucket, dict) and bucket.get("requests"):
                lines.append(f"- {cell(agent)} / {key}: {number(bucket.get('requests'))} requests; "
                             f"полнота usage={flag(bucket.get('complete'))}. Не доказанная fix-only работа.")
    audits = [("completion", (telemetry.get("integrity") or {}).get("completion"))]
    audits += [(repo, data.get("seal_audit")) for repo, data in telemetry.get("repo_status", {}).items()]
    seen = set()
    for owner, audit in audits:
        if not isinstance(audit, dict):
            continue
        for kind in ("missing", "unreadable", "hash_mismatch", "hash_mismatches", "malformed"):
            for issue in audit.get(kind, []):
                if isinstance(issue, dict):
                    path = issue.get("path") or issue.get("artifact") or "неизвестно"
                    text = f"{kind}: {path}"
                    if "expected_sha256" in issue:
                        text += f"; expected={issue['expected_sha256']}; actual={issue.get('actual_sha256')}"
                else:
                    text = f"{kind}: {issue}"
                if text not in seen:
                    lines.append(f"- {cell(text)}")
                    seen.add(text)
    for warning in telemetry.get("warnings", []):
        if warning not in seen:
            lines.append(f"- {cell(warning)}")
            seen.add(warning)
    lines += ["", "USD недоступен: нет request-level USD источника. Известные токены не равны всему API bill."]
    return "\n".join(lines) + "\n"
