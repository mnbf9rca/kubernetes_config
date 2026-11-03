#!/usr/bin/env python3
"""Exercise the Archive Secret target with fake vault and Kubernetes commands."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = '[jotta-archive]\ntype = jottacloud\ntoken = {"refresh_token":"fake-only"}\n'
FIELDS = ('KOPIA_PASSWORD', 'S3_ACCESS_KEY', 'S3_SECRET_KEY', 'RCLONE_CONFIG', 'JOTTA_CRYPT_PASSWORD')
STUB = '''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
log = pathlib.Path(os.environ['FAKE_LOG'])
def record(event):
    with log.open('a') as out:
        out.write(json.dumps(event) + '\\n')
if pathlib.Path(sys.argv[0]).name == 'op':
    field = args[-1].rsplit('/', 1)[-1]
    if field == os.environ.get('FAIL_FIELD'):
        raise SystemExit(9)
    if field != os.environ.get('EMPTY_FIELD'):
        print(os.environ['FAKE_CONFIG'] if field == 'RCLONE_CONFIG' else 'fake-' + field)
elif args == ['config', 'current-context']:
    print(os.environ.get('FAKE_CONTEXT', 'cynexia-homelab'))
elif args[:2] == ['create', 'secret']:
    record({'command': 'create', 'args': args})
    if os.environ.get('FAIL_CREATE'):
        print('partial-output')
        raise SystemExit(9)
    values = dict(a.removeprefix('--from-literal=').split('=', 1) for a in args if a.startswith('--from-literal='))
    print(json.dumps({'kind': 'Secret', 'stringData': values}))
elif args == ['apply', '-f', '-']:
    record({'command': 'apply', 'secret': json.load(sys.stdin)})
    raise SystemExit(9 if os.environ.get('FAIL_APPLY') else 0)
else:
    raise SystemExit('Unexpected fake command')
'''


class ArchiveSecretTests(unittest.TestCase):
    def invoke(self, **options):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name in ('op', 'kubectl'):
                stub = base / name
                stub.write_text(STUB)
                stub.chmod(0o755)
            log = base / 'calls.jsonl'
            env = dict(os.environ, PATH=str(base) + os.pathsep + os.environ['PATH'],
                       FAKE_LOG=str(log), FAKE_CONFIG=CONFIG, **options)
            result = subprocess.run(['make', '--no-print-directory', 'create-jotta-archive-secret'],
                                    cwd=ROOT, env=env, capture_output=True, text=True)
            calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            return result, calls

    def test_read_failure_never_writes_secret(self):
        for field in FIELDS:
            with self.subTest(field=field):
                result, calls = self.invoke(FAIL_FIELD=field)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])

    def test_empty_read_never_writes_secret(self):
        for field in FIELDS:
            with self.subTest(field=field):
                result, calls = self.invoke(EMPTY_FIELD=field)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])

    def test_create_failure_never_applies(self):
        result, calls = self.invoke(FAIL_CREATE='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([c['command'] for c in calls], ['create'])

    def test_multiline_config_success(self):
        result, calls = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c['command'] for c in calls], ['create', 'apply'])
        values = calls[-1]['secret']['stringData']
        self.assertEqual(values['RCLONE_CONFIG'], CONFIG.rstrip('\n'))
        self.assertEqual(values['DEST_REMOTE_PASSWORD'], 'fake-JOTTA_CRYPT_PASSWORD')
        self.assertEqual(set(values), {'RCLONE_CONFIG', 'KOPIA_PASSWORD', 'S3_ACCESS_KEY', 'S3_SECRET_KEY', 'DEST_REMOTE_PASSWORD'})
        self.assertIn('jottacloud-archive-secrets', calls[0]['args'])

    def test_apply_failure_propagates(self):
        result, calls = self.invoke(FAIL_APPLY='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([c['command'] for c in calls], ['create', 'apply'])

    def test_wrong_context_never_writes_secret(self):
        result, calls = self.invoke(FAKE_CONTEXT='wrong-cluster')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
