import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name('netdisco-pg-dump.sh')


class DumpTest(unittest.TestCase):
    def run_dump(self, root, fail=False):
        bin_dir = root / 'bin'
        bin_dir.mkdir(exist_ok=True)
        pg_dump = bin_dir / 'pg_dump'
        pg_dump.write_text("#!/bin/sh\n" + ("exit 1\n" if fail else """for a do case "$a" in --file=*) out=${a#--file=};; esac; done
printf 'CREATE TABLE test (id int);\\n' > "$out"
head -c 400000 /dev/urandom >> "$out"
"""))
        pg_dump.chmod(0o755)
        wget = bin_dir / 'wget'
        wget.write_text('#!/bin/sh\nexit 0\n')
        wget.chmod(0o755)
        env = dict(os.environ, PATH=str(bin_dir) + ':' + os.environ['PATH'],
                   DUMP_DIR=str(root), PUSH_URL='https://example.test/api/push/test')
        return subprocess.run(['sh', str(SCRIPT)], env=env, capture_output=True)

    def test_failure_does_not_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertNotEqual(self.run_dump(root, fail=True).returncode, 0)
            self.assertEqual(list(root.glob('netdisco-*.sql.gz')), [])
            self.assertEqual(list(root.glob('.netdisco-*')), [])

    def test_atomic_publish_and_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for _ in range(8):
                result = self.run_dump(root)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(len(list(root.glob('netdisco-*.sql.gz'))), 7)
            self.assertEqual(list(root.glob('.netdisco-*')), [])
            self.assertTrue(all(p.stat().st_size >= 360000 for p in root.glob('netdisco-*.sql.gz')))


if __name__ == '__main__':
    unittest.main()
