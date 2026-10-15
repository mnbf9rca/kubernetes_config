"""Run on the VM under its selected PM runtime; uses only disposable profile files."""
import ast
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from agent.secret_scope import load_env_file
from hermes_cli.config import _strip_managed_keys_for_save
from hermes_constants import get_hermes_home_override, set_hermes_home_override
from tools.terminal_scope import build_profile_terminal_scope
from utils import atomic_roundtrip_yaml_save, load_yaml_file_readonly


class ProfilePolicyTests(unittest.TestCase):
    def test_bulk_save_keeps_terminal_in_desktop_sandbox(self):
        helper = Path(__file__).with_name('hermes-profile-docker-setup.sh')
        outer = ast.parse(helper.read_text().split("<<'PYTHON'\n", 1)[1].split('\nPYTHON', 1)[0])
        code = next(n.value for n in ast.walk(outer) if isinstance(n, ast.Constant)
                    and isinstance(n.value, str) and 'managed = load_yaml_file_readonly' in n.value)
        previous = get_hermes_home_override()
        try:
            for old_env in ('', 'TERMINAL_ENV=docker\nTERMINAL_DOCKER_IMAGE=old-image\n'):
                with self.subTest(old_env=bool(old_env)), tempfile.TemporaryDirectory() as scratch:
                    home = Path(scratch)
                    config = home / 'config.yaml'
                    config.write_text('# keep comment\n_config_version: 51\nterminal:\n  docker_volumes: []\n')
                    env = home / '.env'
                    env.write_text('# keep reference\nUNRELATED=op://example/item/field\n' + old_env)
                    env.chmod(0o600)
                    set_hermes_home_override(home)
                    exec(code, {'home': str(home), 'policy_only': True})
                    with contextlib.redirect_stderr(io.StringIO()):
                        stripped = _strip_managed_keys_for_save(load_yaml_file_readonly(config))
                    atomic_roundtrip_yaml_save(config, stripped)
                    scope = build_profile_terminal_scope(home)
                    self.assertEqual(scope['TERMINAL_ENV'], 'docker')
                    self.assertEqual(scope['TERMINAL_DOCKER_IMAGE'], 'nousresearch/hermes-sandbox:desktop')
                    self.assertEqual(scope['TERMINAL_DOCKER_RUN_AS_HOST_USER'].lower(), 'true')
                    self.assertEqual(scope['TERMINAL_CWD'], '/workspace')
                    self.assertIn('# keep comment', config.read_text())
                    self.assertIn('# keep reference', env.read_text())
                    self.assertIn('UNRELATED=op://example/item/field', env.read_text())
                    self.assertEqual(env.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(load_yaml_file_readonly(config)['terminal']['docker_volumes'], [])
        finally:
            set_hermes_home_override(previous)


    def test_mounts_survive_webui_parser_and_repeat_setup(self):
        helper = Path(__file__).with_name('hermes-profile-docker-setup.sh')
        outer = ast.parse(helper.read_text().split("<<'PYTHON'\n", 1)[1].split('\nPYTHON', 1)[0])
        code = next(n.value for n in ast.walk(outer) if isinstance(n, ast.Constant)
                    and isinstance(n.value, str) and 'managed = load_yaml_file_readonly' in n.value)
        previous = get_hermes_home_override()
        try:
            for old_override in ('', 'TERMINAL_DOCKER_VOLUMES="[\\\"stale\\\"]"\n'):
                with self.subTest(old_override=bool(old_override)), tempfile.TemporaryDirectory() as scratch:
                    home = Path(scratch)
                    extra = str(home / "owner's files") + ':/extra:ro'
                    config = home / 'config.yaml'
                    expected = [extra, str(home / 'workspace') + ':/workspace',
                                '/home/hermes/.hermes/webui/attachments:/home/hermes/.hermes/webui/attachments:ro',
                                str(home / 'workspace') + ':' + str(home / 'workspace')]
                    atomic_roundtrip_yaml_save(config, {'terminal': {
                        'docker_volumes': expected if old_override else [extra]}})
                    env = home / '.env'
                    unrelated = '# keep reference\nUNRELATED=op://example/item/field\n'
                    env.write_text(unrelated + old_override)
                    env.chmod(0o640)
                    set_hermes_home_override(home)
                    exec(code, {'home': str(home), 'policy_only': False})
                    self.assertEqual(load_yaml_file_readonly(config)['terminal']['docker_volumes'], expected)
                    line = next(line for line in env.read_text().splitlines()
                                if line.startswith('TERMINAL_DOCKER_VOLUMES='))
                    webui_value = line.split('=', 1)[1].strip().strip('"').strip("'")
                    try:
                        parsed = json.loads(webui_value)
                    except ValueError:
                        self.fail('helper left mount JSON unreadable by WebUI')
                    self.assertEqual(parsed, expected)
                    self.assertEqual(json.loads(load_env_file(env)['TERMINAL_DOCKER_VOLUMES']), expected)
                    self.assertEqual(json.loads(build_profile_terminal_scope(home)['TERMINAL_DOCKER_VOLUMES']), expected)
                    self.assertTrue(env.read_text().startswith(unrelated))
                    self.assertEqual(env.stat().st_mode & 0o777, 0o640)
                    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (config, env)}
                    exec(code, {'home': str(home), 'policy_only': False})
                    self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}, before)
        finally:
            set_hermes_home_override(previous)


if __name__ == '__main__':
    unittest.main()
