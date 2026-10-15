import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).with_name('hermes-webui-runtime.py')


class RuntimeCheckTests(unittest.TestCase):
    def load_check(self):
        self.assertTrue(SCRIPT.exists(), 'PM-aware import check is missing')
        spec = importlib.util.spec_from_file_location('webui_runtime_check', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_uses_upstream_runtime_without_pinning_a_generation(self):
        check = self.load_check()
        calls = []
        upstream = types.ModuleType('hermes_cli._launchers')
        def runtime_command(root, **kwargs):
            calls.append((root, kwargs))
            return ['/managed/python', '-I', '-c', kwargs['code']]
        upstream.runtime_command = runtime_command
        with patch.dict(sys.modules, {'hermes_cli': types.ModuleType('hermes_cli'),
                                      'hermes_cli._launchers': upstream}):
            command = check.build_command()
        self.assertEqual(calls[0][0], Path('/home/hermes/.hermes/hermes-agent'))
        self.assertEqual(calls[0][1]['home'], '/home/hermes/.hermes')
        self.assertNotIn('/venv/bin/python', ' '.join(command))
        self.assertNotIn('/environments/', ' '.join(command))
        code = calls[0][1]['code']
        self.assertIn('yaml_compat', code)
        self.assertIn('api.profiles', code)
        self.assertIn('run_agent', code)
        self.assertNotIn('run_path', code)

    def test_guards_bootstrap_and_isolates_settings_before_child_starts(self):
        check = self.load_check()
        observed = {}
        def run(command, **kwargs):
            observed.update(kwargs)
            state = Path(kwargs['env']['HERMES_WEBUI_STATE_DIR'])
            self.assertTrue(state.is_dir())
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)
            observed['state'] = state
            self.assertEqual(kwargs['env']['HERMES_DISABLE_LAZY_INSTALLS'], '1')
            return types.SimpleNamespace(returncode=7)
        with patch.object(check, 'build_command', return_value=['/managed/python']), \
             patch.object(check.subprocess, 'run', side_effect=run), \
             patch.dict(os.environ, {'HERMES_WEBUI_STATE_DIR': '/real/user/state'}):
            self.assertEqual(check.main(['--check-import']), 7)
        self.assertFalse(observed['state'].exists())
        self.assertEqual(observed['cwd'], '/')

    def test_has_no_server_or_unrecognised_mode(self):
        check = self.load_check()
        for args in ([], ['--serve'], ['--unknown']):
            with self.subTest(args=args), self.assertRaises(SystemExit) as caught:
                check.main(args)
            self.assertEqual(caught.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
