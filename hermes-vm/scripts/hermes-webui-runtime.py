#!/usr/bin/env python3
"""Check the WebUI and agent imports in upstream's current PM environment."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

AGENT = Path('/home/hermes/.hermes/hermes-agent')
WEBUI = Path('/home/hermes/hermes-webui')


def build_command():
    sys.path.insert(0, str(AGENT))
    from hermes_cli._launchers import runtime_command
    code = (
        "import os,sys; "
        f"sys.path.insert(0, {str(WEBUI)!r}); "
        "os.environ['HERMES_WEBUI_PYTHON']=sys.executable; "
        "from api import yaml_compat; import api.profiles; import run_agent"
    )
    return runtime_command(AGENT, code=code, home='/home/hermes/.hermes')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-import', action='store_true', required=True)
    parser.parse_args(argv)
    # Even manual triage must never complete a pending core update.
    env = dict(os.environ, HERMES_DISABLE_LAZY_INSTALLS='1')
    with tempfile.TemporaryDirectory(prefix='hermes-webui-check-') as state:
        # api.config can reconcile settings on import; never give it live state.
        env['HERMES_WEBUI_STATE_DIR'] = state
        return subprocess.run(build_command(), cwd='/', env=env).returncode


if __name__ == '__main__':
    raise SystemExit(main())
