"""Run creation and setup telemetry without project dependencies."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'ship_state.py'


class RunSetupTests(unittest.TestCase):
    def test_invalid_read_excludes_never_falls_back_to_unrestricted_setup(self):
        with tempfile.TemporaryDirectory(dir=os.environ['SHIP_TEST_TMPDIR']) as directory:
            repo = Path(directory) / 'repo'
            run = repo / 'private/run'
            run.mkdir(parents=True)
            (run / 'run.json').write_text(json.dumps({'schema_version': 2, 'workflow': 'fix',
                                                       'run_id': 'run', 'repos': ['api']}))
            contract = run / 'contract.json'
            contract.write_text(json.dumps({'repo_paths': {'api': str(repo)},
                                            'read_excludes': {'api': ['private'], 'bad': ['hidden']}}))
            result = subprocess.run(['python3', '-I', '-B', str(SCRIPT), 'validate-run',
                                     '--contract', str(contract), '--attempt-id', 'first'],
                                    capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode)
            self.assertFalse((run / 'telemetry').exists())
            self.assertFalse((run / 'setup-seals').exists())

    def test_init_is_valid_exclusive_and_reports_actual_start(self):
        with tempfile.TemporaryDirectory(dir=os.environ['SHIP_TEST_TMPDIR']) as directory:
            root = Path(directory)
            run = root / 'plans/.fix-runs/new-run'
            args = ['python3', '-I', '-B', str(SCRIPT), 'init-run', '--run-dir', str(run),
                    '--workflow', 'fix', '--repo', 'api', '--description', 'Repair a problem', '--session-id', '']
            result = subprocess.run(args, cwd=root, text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr)
            metadata = json.loads((run / 'run.json').read_text())
            self.assertEqual(('new-run', 'fix', ['api'], ''),
                             (metadata['run_id'], metadata['workflow'], metadata['repos'], metadata['session_id']))
            before = (run / 'run.json').read_bytes()
            again = subprocess.run(args, cwd=root, text=True, capture_output=True)
            self.assertNotEqual(0, again.returncode)
            self.assertEqual(before, (run / 'run.json').read_bytes())
            self.assertEqual(['run.json'], [p.name for p in run.iterdir()])
            (run / 'contract.json').write_text('{')
            validation = subprocess.run(['python3', '-I', '-B', str(SCRIPT), 'validate-run',
                                         '--contract', str(run / 'contract.json'), '--attempt-id', 'first'],
                                        cwd=root, text=True, capture_output=True)
            self.assertNotEqual(0, validation.returncode)
            end = json.loads((run / 'telemetry/setup-first-end.json').read_text())
            self.assertEqual(('setup', 'failed'), (end['phase'], end['status']))

    def test_invalid_run_location_or_missing_plan_does_not_create_run(self):
        with tempfile.TemporaryDirectory(dir=os.environ['SHIP_TEST_TMPDIR']) as directory:
            root = Path(directory)
            run = root / 'plans/.ship-runs/new-run'
            args = ['python3', '-I', '-B', str(SCRIPT), 'init-run', '--run-dir', str(run),
                    '--workflow', 'ship', '--repo', 'api', '--description', 'Ship plan']
            result = subprocess.run(args, cwd=root, capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode)
            self.assertFalse(run.exists())
            args[args.index(str(run))] = str(root / 'other-run')
            result = subprocess.run(args, cwd=root, capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode)
            self.assertFalse((root / 'other-run').exists())


if __name__ == '__main__':
    unittest.main()
