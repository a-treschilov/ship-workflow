"""Qualitative findings remain tied to recorded identity and explicit review decisions."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


HERE = Path(__file__).resolve()
INSTALLED = (HERE.parents[4] / ".agents/skills/ship-analyze").is_dir()
WORKSPACE = HERE.parents[5] if INSTALLED else HERE.parents[4]
SKILLS = WORKSPACE / (".agents/skills" if INSTALLED else "skills")
PATH = SKILLS / 'ship-analyze/insights.py'
spec = importlib.util.spec_from_file_location('insights', PATH)
insights = importlib.util.module_from_spec(spec)
spec.loader.exec_module(insights)


class InsightsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['SHIP_TEST_TMPDIR'])
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name) / 'run1'
        self.run.mkdir()
        self.write('contract.json', {'run_id': 'run1', 'repos': ['api']})

    def write(self, name, value):
        path = self.run / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def test_explicit_identity_disposition_and_seal(self):
        first = self.write('api/rounds-1/round-1-findings.json', {'run_id': 'run1', 'repo': 'api', 'round': 1,
            'source_fingerprint': 'old', 'findings': [{'id': 'f1', 'description': 'plan and producer disagree'}]})
        self.write('api/rounds-1/round-1-applied.json', {'run_id': 'run1', 'repo': 'api', 'round': 1,
            'applied': [{'id': 'f1', 'verified': 'fixed'}]})
        self.write('api/rounds-2/round-2-findings.json', {'run_id': 'run1', 'repo': 'api', 'round': 2,
            'source_fingerprint': 'new', 'accepted': True, 'findings': [], 'dispositions': [{'id': 'f1', 'status': 'resolved'}]})
        self.write('completion.json', {'run_id': 'run1', 'artifact_hashes': {str(first): hashlib.sha256(first.read_bytes()).hexdigest()}})
        result = insights.analyze(self.run, {'run_completed': True})
        finding = result['findings'][0]
        self.assertEqual('sealed', finding['seal'])
        self.assertEqual('unsealed', finding['fixes'][0]['seal'])
        self.assertEqual('resolved', finding['dispositions'][0]['status'])
        self.assertEqual('contract-gap', result['observations'][0]['category'])
        first.write_text('{}')
        self.assertNotEqual('sealed', insights.analyze(self.run, {'run_completed': False})['findings'][0]['seal'] if insights.analyze(self.run, {'run_completed': False})['findings'] else 'invalid')

    def test_bound_proof_and_tampering(self):
        self.write('contract.json', {'run_id': 'run1', 'repos': ['api'], 'repo_paths': {'api': '/repo'}})
        payload = {'repo_path': '/repo', 'head': 'abc', 'files': {}}
        fingerprint = insights._state.digest(payload)
        manifest = self.write('api/source.json', {**payload, 'fingerprint': fingerprint})
        proof = self.write('api/proof.json', {'run_id': 'run1', 'repo': 'api', 'status': 'passed',
            'claims': [{'id': 'f1'}], 'before_manifest': str(manifest), 'after_manifest': str(manifest),
            'artifact_hashes': {str(manifest): hashlib.sha256(manifest.read_bytes()).hexdigest()}})
        self.write('api/rounds-1/round-1-findings.json', {'run_id': 'run1', 'repo': 'api', 'round': 1,
            'findings': [{'id': 'f1'}]})
        self.write('api/rounds-2/round-2-findings.json', {'run_id': 'run1', 'repo': 'api', 'round': 2,
            'source_fingerprint': fingerprint, 'findings': [], 'dispositions': [{'id': 'f1', 'status': 'resolved',
            'evidence_paths': [str(proof)]}]})
        self.write('completion.json', {'run_id': 'run1', 'repos': {'api': {'source_fingerprint': fingerprint}},
            'artifact_hashes': {str(proof): hashlib.sha256(proof.read_bytes()).hexdigest()}})
        item = insights.analyze(self.run, {'run_completed': True})['findings'][0]['dispositions'][0]
        self.assertEqual('match', item['final_fingerprint_binding'])
        self.assertEqual('match-final', item['proof_inspection'][0]['manifest_binding'])
        self.assertEqual('match', item['proof_inspection'][0]['claim_match'])
        manifest.write_text('{}')
        item = insights.analyze(self.run, {'run_completed': True})['findings'][0]['dispositions'][0]
        self.assertEqual('invalid', item['proof_inspection'][0]['embedded_hashes'])
        self.assertNotEqual('match-final', item['proof_inspection'][0]['manifest_binding'])

    def test_duplicate_identity_is_not_joined_and_handoff_is_unverified(self):
        for folder in ('rounds-1', 'rounds-1b'):
            self.write(f'api/{folder}/round-1-findings.json', {'run_id': 'run1', 'repo': 'api', 'round': 1,
                'findings': [{'id': 'f1'}]})
        self.write('api/rounds-2/round-2-findings.json', {'run_id': 'run1', 'repo': 'api', 'round': 2,
            'findings': [], 'dispositions': [{'id': 'f1', 'status': 'resolved'}]})
        (self.run / 'handoff-note.md').write_text('run1: не деплоить backend отдельно\n')
        result = insights.analyze(self.run, {})
        self.assertTrue(all(item['identity_ambiguous'] and not item['dispositions'] for item in result['findings']))
        self.assertEqual('unverified', result['handoffs'][0]['timing'])
        self.assertEqual('unsealed', result['handoffs'][0]['seal'])
        self.assertEqual(1, result['handoffs'][0]['release_statements'][0]['line'])

    def test_accepted_review_does_not_imply_closure(self):
        self.write('api/rounds-1/round-1-findings.json', {'run_id': 'run1', 'repo': 'api', 'round': 1,
            'findings': [{'id': 'f1', 'description': 'ordinary issue'}]})
        self.write('api/rounds-2/round-2-findings.json', {'run_id': 'run1', 'repo': 'api', 'round': 2,
            'accepted': True, 'findings': [], 'dispositions': []})
        result = insights.analyze(self.run, {})
        self.assertEqual([], result['findings'][0]['dispositions'])
        self.assertEqual([], result['observations'])

    def test_foreign_run_and_invalid_contract(self):
        self.write('api/rounds-1/round-1-findings.json', {'run_id': 'other', 'repo': 'api', 'round': 1, 'findings': []})
        result = insights.analyze(self.run, {})
        self.assertTrue(any('mismatched' in s for s in result['limitations']))
        self.write('contract.json', {'run_id': 'other', 'repos': ['api']})
        self.assertEqual([], insights.analyze(self.run, {})['findings'])

    def test_mismatching_hash_and_unverified_certificate(self):
        path = self.write('api/rounds-1/round-1-findings.json', {'run_id': 'run1', 'repo': 'api', 'round': 1,
            'findings': [{'id': 'f1'}]})
        self.write('completion.json', {'run_id': 'run1', 'artifact_hashes': {str(path): '0' * 64}})
        self.assertEqual('invalid-seal', insights.analyze(self.run, {'run_completed': True})['findings'][0]['seal'])
        self.write('completion.json', {'run_id': 'run1', 'artifact_hashes': {str(path): hashlib.sha256(path.read_bytes()).hexdigest()}})
        self.assertEqual('hash-matches-unverified-certificate', insights.analyze(self.run, {'run_completed': False})['findings'][0]['seal'])


if __name__ == '__main__':
    unittest.main()
