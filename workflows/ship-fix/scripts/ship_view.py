#!/usr/bin/env python3
"""Read-only local browser for installed ship/fix run artifacts."""
import argparse
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import webbrowser

ASSETS = Path(__file__).resolve().parent.parent / 'view'


def read_json(path, warnings, cache=None):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('expected object')
        if cache is not None:
            cache[path] = data
        return data
    except (OSError, ValueError) as error:
        cached = cache is not None and path in cache and path.is_file()
        suffix = ' (showing last readable version of this file)' if cached else ' (data unavailable)'
        warnings.append(f'{path.name}: {error}{suffix}')
        if cached:
            return cache[path]
        return {}


class Viewer:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.json_cache = {}
        self.analyses = {}
        self.lock = threading.Lock()

    def runs(self):
        result = []
        for workflow in ('ship', 'fix'):
            root = self.workspace / 'plans' / f'.{workflow}-runs'
            if not root.is_dir():
                continue
            for run in root.iterdir():
                if not run.is_dir() or run.is_symlink():
                    continue
                warnings = []
                meta = read_json(run / 'run.json', warnings)
                contract = read_json(run / 'contract.json', warnings)
                times = [p.stat().st_mtime for p in run.rglob('*') if p.is_file() and not p.is_symlink()]
                latest = {}
                for path in run.glob('*/state-*.json'):
                    match = re.fullmatch(r'state-(\d+)\.json', path.name)
                    if match and not path.is_symlink():
                        repo = path.parent.name
                        if repo not in latest or int(match[1]) > latest[repo][0]:
                            latest[repo] = (int(match[1]), read_json(path, []).get('status'))
                result.append({'workflow': workflow, 'id': run.name, 'run_id': meta.get('run_id', run.name),
                               'goal': meta.get('goal') or meta.get('description') or meta.get('plan_path') or contract.get('goal') or contract.get('objective') or run.name,
                               'statuses': [item[1] for item in latest.values() if item[1]],
                               'updated': max(times, default=run.stat().st_mtime), 'warnings': warnings})
        return sorted(result, key=lambda x: x['updated'], reverse=True)

    def resolve(self, workflow, name):
        if workflow not in ('ship', 'fix') or not name or name in ('.', '..') or '/' in name or '\\' in name:
            raise ValueError('Invalid run')
        root = (self.workspace / 'plans' / f'.{workflow}-runs').resolve()
        path = root / name
        if path.is_symlink() or not path.is_dir() or not path.resolve().is_relative_to(root):
            raise ValueError('Run not found')
        return path.resolve()

    def snapshot(self, workflow, name):
        run = self.resolve(workflow, name)
        warnings = []
        meta = read_json(run / 'run.json', warnings, self.json_cache)
        contract = read_json(run / 'contract.json', warnings, self.json_cache)
        phases, states, artifacts, schedules = {}, [], [], []
        files = sorted(p for p in run.rglob('*') if p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(run))
        for path in files:
            rel = path.relative_to(run).as_posix()
            stat = path.stat()
            artifacts.append({'id': rel, 'size': stat.st_size})
            if path.parent.name == 'telemetry' and path.suffix == '.json':
                event = read_json(path, warnings, self.json_cache)
                phase_id = event.get('phase_id')
                if isinstance(phase_id, str) and event.get('event') in ('phase-start', 'phase-end'):
                    phases.setdefault(phase_id, {})[event['event']] = event
            elif re.fullmatch(r'state-\d+\.json', path.name):
                state = read_json(path, warnings, self.json_cache)
                states.append({'path': rel, 'sequence': int(path.stem.split('-')[-1]), 'data': state})
            elif path.name == 'slices-approved.json':
                schedule = read_json(path, warnings, self.json_cache)
                schedules.append({'path': rel, 'data': schedule})
        bindings = contract.get('slice_schedules')
        for binding in (bindings if isinstance(bindings, dict) else {}).values():
            if not isinstance(binding, dict) or not isinstance(binding.get('path'), str):
                continue
            path = Path(binding['path'])
            if not path.is_absolute():
                path = run / path
            resolved = path.resolve()
            known = next((item for item in files if item.resolve() == resolved), None)
            if known is not None and all(item['path'] != known.relative_to(run).as_posix() for item in schedules):
                schedules.append({'path': known.relative_to(run).as_posix(), 'data': read_json(known, warnings, self.json_cache)})
        timeline = []
        for phase_id, events in phases.items():
            start, end = events.get('phase-start'), events.get('phase-end')
            source = start or end
            timeline.append({'id': phase_id, 'phase': source.get('phase'), 'repo': source.get('repo'),
                             'slice_id': source.get('slice_id'), 'start': start, 'end': end})
        timeline.sort(key=lambda p: ((p['start'] or p['end']).get('at') or '', p['id']))
        states.sort(key=lambda s: (s['path'].rsplit('/', 1)[0], s['sequence']))
        latest = {}
        for item in states:
            latest[item['path'].rsplit('/', 1)[0]] = item['data']
        repos = contract.get('repos') or meta.get('repos') or []
        if not isinstance(repos, list):
            repos = []
        summary = {'repos': [{'name': repo, 'state': latest.get(repo, {})}
                             for repo in repos if isinstance(repo, str)],
                   'slices': [{'path': path, 'state': value} for path, value in latest.items()
                              if '/slices/' in path]}
        signature = hashlib.sha256(json.dumps([(p.relative_to(run).as_posix(), p.stat().st_mtime_ns, p.stat().st_size) for p in files]).encode()).hexdigest()
        result = {'workflow': workflow, 'id': name, 'meta': meta, 'contract': contract, 'phases': timeline,
                  'states': states, 'schedules': schedules, 'artifacts': artifacts, 'warnings': warnings, 'signature': signature, 'summary': summary,
                  'completion': read_json(run / 'completion.json', warnings, self.json_cache) if (run / 'completion.json').is_file() else None}
        return result

    def analysis(self, workflow, name, signature):
        self.resolve(workflow, name)
        key = (workflow, name)
        with self.lock:
            entry = self.analyses.get(key, {})
            now = time.time()
            if not entry.get('running') and (not entry or now - entry.get('started', 0) >= 10):
                entry = dict(entry, running=True, started=now)
                self.analyses[key] = entry
                threading.Thread(target=self._analyze, args=(key, signature), daemon=True).start()
            return dict(entry, stale=entry.get('signature') != signature)

    def _analyze(self, key, signature):
        workflow, name = key
        analyzer = self.workspace / '.agents/skills/ship-analyze/analyze.py'
        try:
            proc = subprocess.run([sys.executable, '-I', '-B', str(analyzer), '--workspace', str(self.workspace),
                                   '--workflow', workflow, '--run', name, '--no-cost'], capture_output=True, text=True, timeout=120)
            if proc.returncode:
                raise ValueError(proc.stderr.strip() or 'Analysis failed')
            data = json.loads(proc.stdout)
            if Path(data.get('run_dir', '')).resolve() != self.resolve(workflow, name):
                raise ValueError('Analyzer resolved a different run')
            result = {'data': data, 'error': None}
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            result = {'data': None, 'error': str(error)}
        with self.lock:
            self.analyses[key].update(result, running=False, signature=signature, computed_at=time.time())

    def artifact(self, workflow, name, rel):
        run = self.resolve(workflow, name)
        candidate = run / rel
        if not rel or candidate.is_absolute() and not candidate.resolve().is_relative_to(run):
            raise ValueError('Invalid artifact')
        if not candidate.resolve().is_relative_to(run) or any(p.is_symlink() for p in [candidate, *candidate.parents] if p != run and p.is_relative_to(run)):
            raise ValueError('Invalid artifact')
        if not candidate.is_file():
            raise ValueError('Artifact not found')
        if candidate.stat().st_size > 2_000_000:
            raise ValueError('Artifact exceeds 2 MB')
        return candidate.read_text(encoding='utf-8', errors='replace')


def handler(viewer):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            def arg(key):
                return query.get(key, [''])[0]
            try:
                if parsed.path == '/api/runs':
                    self.send_json(viewer.runs())
                elif parsed.path == '/api/run':
                    self.send_json(viewer.snapshot(arg('workflow'), arg('id')))
                elif parsed.path == '/api/analysis':
                    self.send_json(viewer.analysis(arg('workflow'), arg('id'), arg('signature')))
                elif parsed.path == '/api/artifact':
                    self.send_json({'content': viewer.artifact(arg('workflow'), arg('id'), arg('path'))})
                elif parsed.path in ('/', '/app.js', '/style.css'):
                    path = ASSETS / ('index.html' if parsed.path == '/' else parsed.path[1:])
                    body = path.read_bytes()
                    self.send_response(200)
                    self.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'text/plain')
                    self.send_header('Content-Length', str(len(body)))
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)
            except (ValueError, OSError) as error:
                self.send_json({'error': str(error)}, 400)

        def send_json(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', default=os.environ.get('SHIP_ROOT') or os.getcwd())
    parser.add_argument('--port', type=int)
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args()
    workspace = Path(args.workspace).expanduser().resolve()
    if not (workspace / '.agents').is_dir():
        parser.error('project root must contain .agents')
    viewer = Viewer(workspace)
    try:
        server = ThreadingHTTPServer(('127.0.0.1', args.port if args.port is not None else 8765), handler(viewer))
    except OSError:
        if args.port is not None:
            raise
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(viewer))
    url = f'http://127.0.0.1:{server.server_port}/'
    print(f'Run Explorer: {url} (Ctrl+C to stop)', flush=True)
    if not args.no_open:
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
