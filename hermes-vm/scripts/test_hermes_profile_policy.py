"""Run on the VM under its selected PM runtime; uses only disposable profile files."""
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.secret_scope import load_env_file
from hermes_cli.config import _strip_managed_keys_for_save
from hermes_constants import get_hermes_home_override, set_hermes_home_override
from tools.terminal_scope import build_profile_terminal_scope
from utils import atomic_roundtrip_yaml_save, load_yaml_file_readonly


class ProfilePolicyTests(unittest.TestCase):
    def test_image_selection_without_managed_image_pin(self):
        helper = Path(__file__).with_name('hermes-profile-docker-setup.sh')
        outer = ast.parse(helper.read_text().split("<<'PYTHON'\n", 1)[1].split('\nPYTHON', 1)[0])
        code = next(n.value for n in ast.walk(outer) if isinstance(n, ast.Constant)
                    and isinstance(n.value, str) and 'managed = load_yaml_file_readonly' in n.value)
        previous = get_hermes_home_override()
        try:
            for yaml_image, env_image, expected in (
                    (None, None, 'nousresearch/hermes-sandbox:desktop'),
                    (None, 'env-image', 'env-image'),
                    ('ghcr.io/example/reviewer:stable', 'stale-image', 'ghcr.io/example/reviewer:stable')):
                with self.subTest(image=expected), tempfile.TemporaryDirectory() as scratch:
                    home = Path(scratch) / 'profile'; home.mkdir()
                    managed = Path(scratch) / 'managed'; managed.mkdir()
                    (managed / 'config.yaml').write_text('terminal:\n  backend: docker\n  docker_run_as_host_user: true\n  cwd: /workspace\n')
                    config = home / 'config.yaml'; env = home / '.env'
                    terminal = {'docker_volumes': ['/approved:/approved:ro']}
                    if yaml_image:
                        terminal['docker_image'] = yaml_image
                    atomic_roundtrip_yaml_save(config, {'unrelated': 'keep', 'terminal': terminal})
                    env.write_text('UNRELATED=op://example/item/field\n' +
                                   (f'TERMINAL_DOCKER_IMAGE={env_image}\n' if env_image else ''))
                    env.chmod(0o640)
                    set_hermes_home_override(home)
                    # The helper's fixed managed path reads this fixture; scope resolution uses the same directory.
                    def read(path):
                        return load_yaml_file_readonly(managed / 'config.yaml' if str(path) == '/etc/hermes/config.yaml' else path)
                    with patch.dict(os.environ, {'HERMES_MANAGED_DIR': str(managed)}), patch('utils.load_yaml_file_readonly', read):
                        try:
                            exec(code, {'home': str(home), 'policy_only': True})
                        except KeyError as exc:
                            self.fail(f'helper requires an optional managed image: {exc}')
                        scope = build_profile_terminal_scope(home)
                        self.assertEqual(scope['TERMINAL_DOCKER_IMAGE'], expected)
                        self.assertEqual(scope['TERMINAL_ENV'], 'docker')
                        self.assertEqual(scope['TERMINAL_CWD'], '/workspace')
                        self.assertEqual(scope['TERMINAL_DOCKER_RUN_AS_HOST_USER'].lower(), 'true')
                    saved = load_yaml_file_readonly(config)
                    self.assertEqual(saved['unrelated'], 'keep')
                    self.assertEqual(saved['terminal']['docker_volumes'], ['/approved:/approved:ro'])
                    self.assertIn('UNRELATED=op://example/item/field', env.read_text())
                    self.assertEqual(env.stat().st_mode & 0o777, 0o640)
        finally:
            set_hermes_home_override(previous)

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
                    self.assertEqual(scope['TERMINAL_DOCKER_IMAGE'], 'old-image' if old_env else 'nousresearch/hermes-sandbox:desktop')
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


    def test_reviewer_image_survives_modes_and_bulk_save(self):
        helper = Path(__file__).with_name('hermes-profile-docker-setup.sh')
        outer = ast.parse(helper.read_text().split("<<'PYTHON'\n", 1)[1].split('\nPYTHON', 1)[0])
        code = next(n.value for n in ast.walk(outer) if isinstance(n, ast.Constant)
                    and isinstance(n.value, str) and 'managed = load_yaml_file_readonly' in n.value)
        previous = get_hermes_home_override()
        try:
            for mode in ('ordinary', 'policy', 'sync'):
                with self.subTest(mode=mode), tempfile.TemporaryDirectory() as scratch:
                    home=Path(scratch); set_hermes_home_override(home)
                    image='ghcr.io/example/reviewer@sha256:'+'a'*64
                    config=home/'config.yaml'; env=home/'.env'
                    atomic_roundtrip_yaml_save(config, {'terminal': {'docker_image':image, 'docker_volumes':[]}})
                    env.write_text('TERMINAL_DOCKER_IMAGE='+(image if mode=='sync' else 'stale-image')+'\n');env.chmod(0o600)
                    ctx={'home':str(home),'policy_only':mode=='policy','sync_mounts':mode=='sync'}
                    exec(code,ctx)
                    self.assertEqual(build_profile_terminal_scope(home)['TERMINAL_DOCKER_IMAGE'],image)
                    with contextlib.redirect_stderr(io.StringIO()):
                        atomic_roundtrip_yaml_save(config,_strip_managed_keys_for_save(load_yaml_file_readonly(config)))
                    exec(code,ctx)
                    self.assertEqual(build_profile_terminal_scope(home)['TERMINAL_DOCKER_IMAGE'],image)
                    self.assertEqual(env.stat().st_mode & 0o777,0o600)
        finally:
            set_hermes_home_override(previous)

    def test_sync_mounts_default_isolated_and_hal(self):
        helper = Path(__file__).with_name('hermes-profile-docker-setup.sh')
        outer=ast.parse(helper.read_text().split("<<'PYTHON'\n",1)[1].split('\nPYTHON',1)[0])
        code=next(n.value for n in ast.walk(outer) if isinstance(n,ast.Constant)
                  and isinstance(n.value,str) and 'managed = load_yaml_file_readonly' in n.value)
        previous=get_hermes_home_override()
        try:
            for name in ('default','isolated','hal'):
                with self.subTest(profile=name), tempfile.TemporaryDirectory() as scratch:
                    home=Path(scratch);set_hermes_home_override(home)
                    volumes=[] if name=='isolated' else [str(home/"owner's scripts")+':/scripts:ro']
                    config=home/'config.yaml';env=home/'.env'
                    atomic_roundtrip_yaml_save(config,{'terminal':{'docker_volumes':volumes}})
                    env.write_text('UNRELATED=op://example/item/field\n');env.chmod(0o640)
                    original=config.read_bytes()
                    ctx={'home':str(home),'policy_only':False,'sync_mounts':True}
                    exec(code,ctx)
                    self.assertEqual(config.read_bytes(),original)
                    self.assertEqual(json.loads(build_profile_terminal_scope(home)['TERMINAL_DOCKER_VOLUMES']),volumes)
                    line=next(x for x in env.read_text().splitlines() if x.startswith('TERMINAL_DOCKER_VOLUMES='))
                    self.assertEqual(json.loads(line.split('=',1)[1].strip().strip('"').strip("'")),volumes)
                    self.assertEqual(env.stat().st_mode & 0o777,0o640)
                    before=env.read_bytes(),env.stat().st_mtime_ns
                    exec(code,ctx)
                    self.assertEqual((env.read_bytes(),env.stat().st_mtime_ns),before)
        finally:
            set_hermes_home_override(previous)


if __name__ == '__main__':
    unittest.main()
