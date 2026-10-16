#!/bin/sh
# Finish the docker terminal setup for ONE named Hermes profile.
#
# Copy terminal policy from /etc/hermes/config.yaml into config and .env, then add
# the three durable mounts used by the trusted default, emh and hal profiles.
# shortcut: multiplex terminal scopes ignore managed config at 5575a08; remove
# the profile copies when upstream build_profile_terminal_scope honors it.
#
#   <workspace>:/workspace                 the sandbox's working directory
#   <attachments>:<attachments>:ro         the shared WebUI upload inbox, read
#                                          only, mounted at its HOST path
#   <workspace>:<workspace>                the workspace again at its host path
#
# The two identity mounts exist because the WebUI hands the agent HOST paths in
# the prompt text: it pastes the host path of a chat upload, and it labels a
# message with the host path of the selected workspace. A path that does not
# resolve inside the sandbox is a file the agent cannot read.
#
# IT APPENDS. Entries the profile already has are kept, an entry already
# present is not added twice. The .env list is repaired even when YAML already
# has all three; a repeat run changes neither file once both lists agree.
#
# THE ATTACHMENTS MOUNT IS A STOPGAP WITH AN END DATE. hermes-webui saves chat
# uploads to a single shared directory rather than per profile, which is
# upstream issue #6939; PR #7022 moves them into the profile's own cache, which
# hermes already mounts. When a hermes-webui update carries that fix, drop the
# attachments entry from every profile that has it and from this script - the
# cost of keeping it is that each profile's uploads are visible in every
# sandbox. The removal condition is in docs/operations/hermes-vm.md.
#
# Canonical copy: hermes-vm/scripts/hermes-profile-docker-setup.sh in
# github.com/mnbf9rca/kubernetes_config. Installed on the VM at
# /home/hermes/.hermes/scripts/ beside the two cron scripts, for the same
# reason they live there: that directory rides the nightly `hermes backup` zip.
# --policy-only copies policy without adding shared mounts, for isolated workers.
# THIS ONE IS NOT A CRON JOB. It is run by hand, once, per new profile, and it
# pushes to no monitor and reports to nothing.
set -eu

HERMES_BIN=/home/hermes/.local/bin/hermes
HERMES_HOME=/home/hermes/.hermes

profile=${1:-}
policy_only=${2:-}
case "$policy_only" in ''|--policy-only|--sync-mounts) ;; *) echo "unknown option: $policy_only" >&2; exit 2 ;; esac

if [ -z "$profile" ]; then
  echo "usage: hermes-profile-docker-setup.sh <profile-name> [--policy-only|--sync-mounts]" >&2
  exit 2
fi

# The default workspace is not under profiles/; only its policy can be copied.
if [ "$profile" = default ] && [ "$policy_only" != --policy-only ] && [ "$policy_only" != --sync-mounts ]; then
  echo "default mounts are already configured; use --policy-only for its policy." >&2
  exit 2
fi

profile_dir=$HERMES_HOME/profiles/$profile
if [ "$profile" = default ]; then profile_dir=$HERMES_HOME; fi
if [ ! -d "$profile_dir" ]; then
  echo "no profile directory at $profile_dir" >&2
  echo "create the profile first: $HERMES_BIN profile create $profile" >&2
  exit 1
fi

# Use upstream's current PM runtime without allowing pending core completion.
/usr/bin/python3 - "$profile_dir" "$policy_only" <<'PYTHON'
from pathlib import Path
import os
import subprocess
import sys

agent = Path('/home/hermes/.hermes/hermes-agent')
sys.path.insert(0, str(agent))
from hermes_cli._launchers import runtime_command

code = "home = " + repr(sys.argv[1]) + "\npolicy_only = " + repr(sys.argv[2] == "--policy-only") + "\nsync_mounts = " + repr(sys.argv[2] == "--sync-mounts") + "\n" + r'''
import copy
import json
sync_mounts = globals().get("sync_mounts", False)
from pathlib import Path
from utils import load_yaml_file_readonly, atomic_roundtrip_yaml_update
from tools.terminal_scope import build_profile_terminal_scope
from hermes_constants import set_hermes_home_override
from hermes_cli.config import (save_env_value, env_write_refusal, _env_write_lock,
                               _write_env_lines, _env_line_defines_key)
from agent.secret_scope import load_env_file
managed = load_yaml_file_readonly('/etc/hermes/config.yaml')['terminal']
keys = {'backend': 'TERMINAL_ENV', 'docker_image': 'TERMINAL_DOCKER_IMAGE',
        'docker_run_as_host_user': 'TERMINAL_DOCKER_RUN_AS_HOST_USER', 'cwd': 'TERMINAL_CWD'}
path = Path(home) / 'config.yaml'
set_hermes_home_override(Path(home))
before = load_yaml_file_readonly(path)
env_path = Path(home) / '.env'
before_env = load_env_file(env_path)
if not sync_mounts:
    selected = dict(managed)
    selected['docker_image'] = before.get('terminal', {}).get('docker_image') or before_env.get('TERMINAL_DOCKER_IMAGE') or managed['docker_image']
    expected = copy.deepcopy(before)
    expected.setdefault('terminal', {}).update({key: selected[key] for key in keys})
    for key in keys:
        if before.get('terminal', {}).get(key) != selected[key]:
            atomic_roundtrip_yaml_update(path, 'terminal.' + key, selected[key])
        # Bulk config saves strip managed YAML keys; .env keeps the sandbox policy.
        value = str(selected[key]).lower() if isinstance(selected[key], bool) else str(selected[key])
        if before_env.get(keys[key]) != value:
            save_env_value(keys[key], value)
    assert load_yaml_file_readonly(path) == expected, 'unrelated profile config changed'
    after_env = load_env_file(env_path)
    assert {k: v for k, v in before_env.items() if k not in keys.values()} == {
        k: v for k, v in after_env.items() if k not in keys.values()}, 'unrelated profile env changed'
    scope = build_profile_terminal_scope(home)
    for key, variable in keys.items():
        assert str(scope[variable]).lower() == str(selected[key]).lower(), key

if sync_mounts or not policy_only:
    workspace = Path(home) / 'workspace'
    if not sync_mounts:
        workspace.mkdir(exist_ok=True)
    attachments = '/home/hermes/.hermes/webui/attachments'
    volumes = list(before.get('terminal', {}).get('docker_volumes') or [])
    if not sync_mounts:
        for entry in (f'{workspace}:/workspace', f'{attachments}:{attachments}:ro',
                      f'{workspace}:{workspace}'):
            if entry not in volumes:
                volumes.append(entry)
    variable = 'TERMINAL_DOCKER_VOLUMES'
    refusal = env_write_refusal(variable, 'set')
    if refusal:
        raise RuntimeError(refusal)
    if before.get('terminal', {}).get('docker_volumes') != volumes:
        atomic_roundtrip_yaml_update(path, 'terminal.docker_volumes', volumes)
    # WebUI strips quotes without dotenv unescaping; use JSON inside single quotes.
    value = json.dumps(volumes).replace("'", r"\u0027")
    assignment = variable + "='" + value + "'\n"
    with _env_write_lock(env_path):
        lines = env_path.read_text().splitlines(keepends=True) if env_path.exists() else []
        updated = [assignment if _env_line_defines_key(line, variable) else line for line in lines]
        if not any(_env_line_defines_key(line, variable) for line in lines):
            if updated and not updated[-1].endswith('\n'):
                updated[-1] += '\n'
            updated.append(assignment)
        if updated != lines:
            _write_env_lines(env_path, updated, preserve_mode=env_path.exists())
    assert json.loads(load_env_file(env_path)[variable]) == volumes
    assert json.loads(build_profile_terminal_scope(home)[variable]) == volumes
'''
subprocess.run(runtime_command(agent, code=code, home=sys.argv[1]),
               env=dict(os.environ, HERMES_DISABLE_LAZY_INSTALLS='1'), check=True)
PYTHON

if [ "$policy_only" = --sync-mounts ]; then
  echo "Saved YAML mounts synchronized to profile env; policy unchanged."
  exit 0
fi

if [ "$policy_only" = --policy-only ]; then
  echo "Managed terminal policy copied to config and .env; profile mounts unchanged."
  exit 0
fi

cat <<EOF

Done: $profile now mounts its workspace and the shared WebUI attachments inbox.

  The four terminal policy keys were copied from /etc/hermes/config.yaml.
  That root-owned file remains the source of record; the profile copies
  work around multiplex terminal scopes bypassing the managed layer.

  Restart the shared hermes-gateway (and the WebUI when it serves this
  profile) before the volumes take effect.

  The attachments mount comes out when hermes-webui ships the fix for
  upstream issue #6939.
EOF
