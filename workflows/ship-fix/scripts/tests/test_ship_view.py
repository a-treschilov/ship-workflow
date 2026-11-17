"""Viewer uses recorded boundaries and never writes run artifacts."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

SCRIPT = Path(__file__).resolve().parents[1] / 'ship_view.py'
spec = importlib.util.spec_from_file_location('ship_view', SCRIPT)
view = importlib.util.module_from_spec(spec)
spec.loader.exec_module(view)


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['SHIP_TEST_TMPDIR'])
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.agents').mkdir()
        self.run = self.root / 'plans/.ship-runs/example'
        self.run.mkdir(parents=True)
        self.save('run.json', {'run_id': 'example', 'goal': 'Example'})
        self.save('contract.json', {'schema_version': 2, 'repos': ['api']})
        self.save('telemetry/start.json', {'event': 'phase-start', 'phase_id': 'p1', 'repo': 'api', 'phase': 'implement', 'at': '2026-01-01T00:00:00Z'})
        self.save('api/state-2.json', {'status': 'blocked'})
        self.viewer = view.Viewer(self.root)

    def save(self, name, value):
        p = self.run / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value))

    def test_catalog_and_open_phase_and_state(self):
        before = {p: p.read_bytes() for p in self.run.rglob('*') if p.is_file()}
        self.assertEqual(['blocked'], self.viewer.runs()[0]['statuses'])
        data = self.viewer.snapshot('ship', 'example')
        self.assertEqual('p1', data['phases'][0]['id'])
        self.assertIsNone(data['phases'][0]['end'])
        self.assertEqual('blocked', data['states'][0]['data']['status'])
        self.assertEqual(before, {p: p.read_bytes() for p in self.run.rglob('*') if p.is_file()})

    def test_summary_uses_latest_state_and_tracks_slices(self):
        self.save('api/state-1.json', {'status': 'ready'})
        self.save('api/slices/part/state-1.json', {'status': 'accepted'})
        data = self.viewer.snapshot('ship', 'example')
        self.assertEqual('blocked', data['summary']['repos'][0]['state']['status'])
        self.assertEqual('accepted', data['summary']['slices'][0]['state']['status'])

    def test_partial_json_retains_previous_file_then_recovers(self):
        first = self.viewer.snapshot('ship', 'example')
        path = self.run / 'telemetry/start.json'
        path.write_text('{')
        partial = self.viewer.snapshot('ship', 'example')
        self.assertEqual(first['phases'], partial['phases'])
        self.assertIn('last readable version', partial['warnings'][0])
        self.save('telemetry/end.json', {'event': 'phase-end', 'phase_id': 'p1', 'status': 'passed'})
        partial = self.viewer.snapshot('ship', 'example')
        self.assertEqual('passed', partial['phases'][0]['end']['status'])
        self.save('telemetry/start.json', {'event': 'phase-start', 'phase_id': 'p1', 'phase': 'implement'})
        self.assertEqual('passed', self.viewer.snapshot('ship', 'example')['phases'][0]['end']['status'])

    def test_initially_invalid_run_metadata_does_not_freeze_other_artifacts(self):
        (self.run / 'run.json').write_text('{')
        first = self.viewer.snapshot('ship', 'example')
        self.assertIn('data unavailable', first['warnings'][0])
        self.save('telemetry/end.json', {'event': 'phase-end', 'phase_id': 'p1', 'status': 'passed'})
        second = self.viewer.snapshot('ship', 'example')
        self.assertEqual('passed', second['phases'][0]['end']['status'])
        self.assertNotEqual(first['signature'], second['signature'])

    def test_http_rejects_external_artifacts_and_serves_static(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), view.handler(self.viewer))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        self.assertIn(b'Run Explorer', urlopen(base + '/').read())
        query = urlencode({'workflow': 'ship', 'id': 'example', 'path': '../../secret'})
        with self.assertRaises(HTTPError):
            urlopen(base + '/api/artifact?' + query)
        (self.run / 'link').symlink_to(self.root / '.agents', target_is_directory=True)
        with self.assertRaises(ValueError):
            self.viewer.artifact('ship', 'example', 'link/anything')

    def test_legacy_run_without_contract_updates(self):
        (self.run / 'contract.json').unlink()
        self.viewer.snapshot('ship', 'example')
        self.save('telemetry/end.json', {'event': 'phase-end', 'phase_id': 'p1', 'status': 'passed'})
        self.assertEqual('passed', self.viewer.snapshot('ship', 'example')['phases'][0]['end']['status'])


if __name__ == '__main__':
    unittest.main()
