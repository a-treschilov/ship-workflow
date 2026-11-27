"""Evidence-bound qualitative observations for versioned workflow runs."""

import hashlib
import importlib.util
import json
from pathlib import Path


_here = Path(__file__).resolve()
_candidates = (
    _here.parents[3] / '.agents' / 'workflows' / 'ship-fix' / 'scripts' / 'ship_state.py',
    _here.parents[2] / 'workflows' / 'ship-fix' / 'scripts' / 'ship_state.py',
)
_state_path = next(path for path in _candidates if path.is_file())
_state_spec = importlib.util.spec_from_file_location('ship_state_for_insights', _state_path)
_state = importlib.util.module_from_spec(_state_spec)
_state_spec.loader.exec_module(_state)


def read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, UnicodeError):
        return None


def analyze(run_dir, telemetry):
    run_dir = Path(run_dir).resolve()
    contract = read(run_dir / 'contract.json')
    completion = read(run_dir / 'completion.json')
    contract = contract if isinstance(contract, dict) else {}
    completion = completion if isinstance(completion, dict) else {}
    run_id = contract.get('run_id')
    result = {'findings': [], 'open_findings': [], 'review_deferrals': [], 'observations': [], 'handoffs': [], 'limitations': []}
    if not isinstance(run_id, str) or run_id != run_dir.name:
        result['limitations'].append('Contract run identity missing or mismatched; qualitative evidence unavailable.')
        return result
    seals = completion.get('artifact_hashes')
    seals = seals if isinstance(seals, dict) and completion.get('run_id') == run_id else {}
    resolved_seals = {}
    for key, value in seals.items():
        try:
            resolved_seals[str(Path(key).resolve())] = value
        except (OSError, ValueError):
            resolved_seals[key] = value
    seals = resolved_seals

    def evidence(path):
        path = Path(path)
        try:
            resolved = path.resolve()
            resolved.relative_to(run_dir)
            expected = seals.get(str(resolved), seals.get(str(path)))
            if not resolved.is_file():
                return 'missing'
            if not isinstance(expected, str):
                return 'unsealed'
            if hashlib.sha256(resolved.read_bytes()).hexdigest() != expected:
                return 'invalid-seal'
            return 'sealed' if telemetry.get('run_completed') is True else 'hash-matches-unverified-certificate'
        except (OSError, ValueError):
            return 'invalid-seal'

    def proof_refs(value):
        if not isinstance(value, list):
            return []
        return [{'path': item, 'seal': evidence(item)} for item in value if isinstance(item, str)]

    def inspect_proof(path, repo, fid, final_fingerprint):
        result_item = {'path': path, 'seal': evidence(path), 'identity': 'unknown',
                       'claim_match': 'unknown', 'record_status': 'unknown',
                       'manifest_binding': 'unknown', 'embedded_hashes': 'unknown'}
        if result_item['seal'] in {'invalid-seal', 'missing'}:
            return result_item
        record = read(Path(path))
        if not isinstance(record, dict):
            return result_item
        result_item['identity'] = 'match' if record.get('run_id') == run_id and record.get('repo') == repo else 'mismatch'
        claims = record.get('claims')
        ids = [item.get('id') for item in claims if isinstance(item, dict)] if isinstance(claims, list) else []
        result_item['claim_match'] = 'match' if fid in ids or record.get('criterion_id') == fid else 'no-explicit-match'
        result_item['record_status'] = record.get('status') or 'unknown'
        embedded = record.get('artifact_hashes')
        if isinstance(embedded, dict) and embedded:
            valid = True
            for key, expected in embedded.items():
                if not isinstance(key, str) or not isinstance(expected, str):
                    valid = False
                    break
                try:
                    target = Path(key).resolve()
                    target.relative_to(run_dir)
                    if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                        valid = False
                        break
                except (OSError, ValueError):
                    valid = False
                    break
            result_item['embedded_hashes'] = 'match' if valid else 'invalid'
        manifests = [record.get('before_manifest'), record.get('after_manifest')]
        if (all(isinstance(p, str) for p in manifests) and isinstance(final_fingerprint, str)
                and isinstance(embedded, dict) and all(p in embedded for p in manifests)
                and result_item['embedded_hashes'] == 'match'):
            try:
                checked = []
                for item in manifests:
                    target = Path(item).resolve()
                    target.relative_to(run_dir)
                    checked.append(_state.checked_manifest(target))
                repo_paths = contract.get('repo_paths')
                expected_repo = repo_paths.get(repo) if isinstance(repo_paths, dict) else None
                if not isinstance(expected_repo, str):
                    result_item['manifest_binding'] = 'unknown-repo-path'
                else:
                    result_item['manifest_binding'] = ('match-final' if all(
                        m.get('fingerprint') == final_fingerprint and m.get('repo_path') == expected_repo
                        for m in checked) else 'stale-or-foreign')
            except (OSError, ValueError, KeyError, TypeError):
                result_item['manifest_binding'] = 'invalid-manifest'
        return result_item

    def load(path, repo):
        value = read(path)
        if not isinstance(value, dict) or value.get('run_id') != run_id or value.get('repo') != repo:
            result['limitations'].append(f'Invalid or mismatched artifact: {path}')
            return None
        return value

    for repo in contract.get('repos', []):
        if not isinstance(repo, str) or Path(repo).name != repo or repo in {'.', '..'}:
            result['limitations'].append('Invalid repository identity in contract.')
            continue
        base = run_dir / repo
        directories = [base] + sorted(p for p in (base / 'slices').glob('*') if p.is_dir())
        for directory in directories:
            states = []
            for state_path in directory.glob('state-*.json'):
                suffix = state_path.stem.removeprefix('state-')
                if suffix.isdigit():
                    states.append((int(suffix), state_path))
            if not states:
                continue
            current = read(max(states)[1]) or {}
            if not isinstance(current, dict):
                continue
            scope = 'repo' if directory == base else 'slice/' + directory.name
            active_paths = {str(Path(path).resolve()) for path in current.get('review_deferral_paths', [])
                            if isinstance(path, str)}
            for _, state_path in sorted(states):
                event = read(state_path) or {}
                paths = event.get('review_deferral_paths') or []
                if not event.get('deferral_applied') or not paths:
                    continue
                try:
                    decision_target = Path(paths[-1]).resolve()
                    decision_target.relative_to(run_dir)
                    decision = read(decision_target) or {}
                    result['review_deferrals'].append({'repo': repo, 'scope': scope,
                        'finding_id': decision.get('finding_id'), 'reason': decision.get('reason'),
                        'review_round': decision.get('review_round'), 'decision_path': str(decision_target),
                        'active': str(decision_target) in active_paths, 'seal': evidence(decision_target)})
                except (OSError, ValueError, TypeError):
                    continue
            review_path = current.get('review_path')
            if not isinstance(review_path, str):
                continue
            try:
                target = Path(review_path).resolve()
                target.relative_to(run_dir)
                review = read(target) or {}
            except (OSError, ValueError):
                continue
            if not isinstance(review, dict) or not isinstance(review.get('findings'), list):
                continue
            deferrals = {}
            for decision_path in current.get('review_deferral_paths', []):
                try:
                    decision_target = Path(decision_path).resolve()
                    decision_target.relative_to(run_dir)
                    decision = read(decision_target) or {}
                    if decision.get('finding_id') in current.get('deferred_finding_ids', []):
                        deferrals[decision['finding_id']] = {'reason': decision.get('reason'),
                            'review_round': decision.get('review_round'), 'decision_path': str(decision_target),
                            'seal': evidence(decision_target)}
                except (OSError, ValueError, TypeError):
                    continue
            for item in review['findings']:
                if isinstance(item, dict):
                    result['open_findings'].append({**item, 'repo': repo, 'scope': scope,
                        'accepted_by_user': bool(current.get('review_acceptance_path')),
                        'deferral': deferrals.get(item.get('id')),
                        'source': str(target), 'seal': evidence(target)})
        completion_repos = completion.get('repos') if isinstance(completion.get('repos'), dict) else {}
        final_record = completion_repos.get(repo) if isinstance(completion_repos.get(repo), dict) else {}
        final_fingerprint = final_record.get('source_fingerprint')
        paths = sorted(base.glob('rounds-*/round-*-findings.json'))
        paths += sorted(base.glob('slices/*/rounds*/round-*-findings.json'))
        reviews = []
        for path in paths:
            data = load(path, repo)
            if not data or not isinstance(data.get('round'), int) or not isinstance(data.get('findings'), list):
                continue
            scope = 'repo' if path.parent.parent == base else 'slice/' + path.relative_to(base).parts[1]
            reviews.append((path, data, scope))
        occurrences = {}
        for _, data, scope in reviews:
            for finding in data['findings']:
                if isinstance(finding, dict) and isinstance(finding.get('id'), str):
                    key = (scope, finding['id'])
                    occurrences[key] = occurrences.get(key, 0) + 1
        for path, data, scope in reviews:
            for finding in data['findings']:
                if not isinstance(finding, dict) or not isinstance(finding.get('id'), str):
                    continue
                fid = finding['id']
                fixes = []
                ambiguous = occurrences[(scope, fid)] != 1
                # Duplicate source claims cannot safely be joined by ID and round.
                if ambiguous:
                    result['limitations'].append(f'Ambiguous finding identity: {repo} / {scope} / {data["round"]} / {fid}')
                # Applied records bind by repo, round, and ID, never by filename alone.
                parent = path.parent.parent
                for applied_path in ([] if ambiguous else sorted(parent.glob('rounds*/round-*-applied.json'))):
                    applied = read(applied_path)
                    if not isinstance(applied, dict) or applied.get('run_id') != run_id or applied.get('repo') != repo or applied.get('round') != data['round']:
                        continue
                    entries = applied.get('applied')
                    if not isinstance(entries, list):
                        continue
                    for entry in entries:
                        if isinstance(entry, dict) and entry.get('id') == fid:
                            fixes.append({'path': str(applied_path), 'seal': evidence(applied_path),
                                          'claim': entry.get('verified'), 'verification_kind': entry.get('verification_kind'),
                                          'evidence_paths': entry.get('evidence_paths', []), 'proof_refs': proof_refs(entry.get('evidence_paths'))})
                dispositions = []
                for later_path, later, later_scope in reviews:
                    if ambiguous or later_scope != scope or later['round'] <= data['round']:
                        continue
                    entries = later.get('dispositions')
                    if not isinstance(entries, list):
                        continue
                    for entry in entries:
                        if isinstance(entry, dict) and entry.get('id') == fid:
                            dispositions.append({'path': str(later_path), 'seal': evidence(later_path),
                                                 'round': later['round'], 'source_fingerprint': later.get('source_fingerprint'),
                                                 'status': entry.get('status'), 'reason': entry.get('reason'),
                                                 'evidence_paths': entry.get('evidence_paths', []), 'proof_refs': proof_refs(entry.get('evidence_paths'))})
                for item in fixes + dispositions:
                    item['proof_inspection'] = [inspect_proof(p['path'], repo, fid, final_fingerprint)
                                                for p in item['proof_refs']]
                for item in dispositions:
                    fingerprint = item.get('source_fingerprint')
                    item['final_fingerprint_binding'] = ('match' if fingerprint == final_fingerprint else 'mismatch') if (
                        isinstance(fingerprint, str) and isinstance(final_fingerprint, str)) else 'unknown'
                record = {'repo': repo, 'scope': scope, 'round': data['round'], 'id': fid, 'identity_ambiguous': ambiguous,
                          'source': str(path), 'seal': evidence(path), 'source_fingerprint': data.get('source_fingerprint'),
                          'category': finding.get('category'), 'recorded_status': finding.get('status'),
                          'scenario': finding.get('failure_scenario'),
                          'description': finding.get('description'), 'criterion_ids': finding.get('criterion_ids', []),
                          'recommendation': finding.get('recommendation'), 'verification_expectation': finding.get('verification_expectation'),
                          'fixes': fixes, 'dispositions': dispositions}
                result['findings'].append(record)
                text = ' '.join(str(finding.get(k) or '') for k in ('description', 'failure_scenario', 'recommendation')).lower()
                rule = None
                if 'plan' in text and ('producer' in text or 'plan-gap' in text):
                    rule = ('contract-gap', 'Сверяются ли план и семантика producer до реализации?', 'Проверять межкомпонентные инварианты на реальном пути и фиксировать согласование расхождений.')
                elif 'n+1' in text or ('per-instrument' in text and 'quer' in text):
                    rule = ('batch-queries', 'Проверяет ли batch-тест число и аргументы запросов?', 'Добавить проверку ограниченного числа batch-вызовов и сохранения защит до и после чтения.')
                if rule:
                    result['observations'].append({'category': rule[0], 'claim': finding.get('description'),
                        'evidence_paths': [str(path)], 'finding_id': fid, 'evidence_level': evidence(path),
                        'limitation': 'Записанная оценка reviewer; не повторная проверка кода и не измерение экономии.',
                        'question': rule[1], 'action': rule[2]})
    for path in sorted(run_dir.glob('handoff*.md')):
        try:
            path.resolve().relative_to(run_dir)
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeError, ValueError):
            result['limitations'].append(f'Unreadable or unsafe handoff: {path}')
            continue
        lines = []
        for number, line in enumerate(text.splitlines(), 1):
            lower = line.lower()
            if any(term in lower for term in ('releasable', 'не депло', 'не выпуск', 'release blocker',
                                               'post-completion', 'после сертификата', 'пока не выполнен')):
                lines.append({'line': number, 'text': line[:2000]})
        result['handoffs'].append({'path': str(path), 'seal': evidence(path),
                                   'run_identity': 'mentioned' if run_id in text else 'unconfirmed',
                                   'timing': 'unverified', 'release_statements': lines,
                                   'limitation': 'Содержимое handoff — утверждения документа; время создания и исполнение последующих этапов не установлены.'})

    incomplete = [p.get('phase_id') for p in telemetry.get('phases', []) if p.get('phase') in
                  {'implement', 'review', 'review-fix', 'gate-fix', 'checker', 'slicer'}
                  and not p.get('correlation_complete')]
    if incomplete:
        result['observations'].append({'category': 'telemetry', 'claim': 'Не удалось однозначно связать фазы с транскриптами: ' + ', '.join(str(x) for x in incomplete),
            'evidence_paths': [], 'evidence_level': 'telemetry-diagnostic',
            'limitation': 'Отсутствие корреляции не доказывает причину или длительность активной работы.',
            'question': 'Записываются ли identity и границы каждой фактической работы?',
            'action': 'Для будущих запусков обеспечить события и identity фактических исполнителей, не переписывая историю.'})
    if not seals:
        result['limitations'].append('Completion seal map unavailable; evidence cannot be certified.')
    result['limitations'].append('Sealed artifacts describe recorded history, not a fresh review of current code. Slice evidence may be unsealed; matching IDs, manifest fingerprints and hashes do not independently prove the recorded assertion.')
    return result
