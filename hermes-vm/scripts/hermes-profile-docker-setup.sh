#!/bin/sh
# Finish the docker terminal setup for ONE named Hermes profile.
#
# Copy the four terminal policy keys from /etc/hermes/config.yaml, then add
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
# present is not added twice, and a run that finds all three present writes
# nothing at all - so running it a second time changes nothing.
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
ATTACHMENTS=$HERMES_HOME/webui/attachments

profile=${1:-}
policy_only=${2:-}
case "$policy_only" in ''|--policy-only) ;; *) echo "unknown option: $policy_only" >&2; exit 2 ;; esac

if [ -z "$profile" ]; then
  echo "usage: hermes-profile-docker-setup.sh <profile-name> [--policy-only]" >&2
  exit 2
fi

# The default workspace is not under profiles/; only its policy can be copied.
if [ "$profile" = default ] && [ "$policy_only" != --policy-only ]; then
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
/usr/bin/python3 - "$profile_dir" <<'PYTHON'
from pathlib import Path
import os
import subprocess
import sys

agent = Path('/home/hermes/.hermes/hermes-agent')
sys.path.insert(0, str(agent))
from hermes_cli._launchers import runtime_command

code = "home = " + repr(sys.argv[1]) + "\n" + r'''
import copy
from pathlib import Path
from utils import load_yaml_file_readonly, atomic_roundtrip_yaml_update
from tools.terminal_scope import build_profile_terminal_scope
managed = load_yaml_file_readonly('/etc/hermes/config.yaml')['terminal']
keys = {'backend': 'TERMINAL_ENV', 'docker_image': 'TERMINAL_DOCKER_IMAGE',
        'docker_run_as_host_user': 'TERMINAL_DOCKER_RUN_AS_HOST_USER', 'cwd': 'TERMINAL_CWD'}
path = Path(home) / 'config.yaml'
before = load_yaml_file_readonly(path)
expected = copy.deepcopy(before)
expected.setdefault('terminal', {}).update({key: managed[key] for key in keys})
for key in keys:
    if before.get('terminal', {}).get(key) != managed[key]:
        atomic_roundtrip_yaml_update(path, 'terminal.' + key, managed[key])
assert load_yaml_file_readonly(path) == expected, 'unrelated profile config changed'
scope = build_profile_terminal_scope(home)
for key, variable in keys.items():
    assert str(scope[variable]).lower() == str(managed[key]).lower(), key
'''
subprocess.run(runtime_command(agent, code=code, home=sys.argv[1]),
               env=dict(os.environ, HERMES_DISABLE_LAZY_INSTALLS='1'), check=True)
PYTHON

if [ "$policy_only" = --policy-only ]; then
  echo "Managed terminal policy copied; profile mounts unchanged."
  exit 0
fi

workspace=$profile_dir/workspace
mkdir -p "$workspace"

# The plain (non-JSON) form prints one `- <entry>` line per list entry and
# nothing of that shape otherwise, so this reads an unset key as no entries
# without parsing YAML. Everything the profile already has is kept.
volumes=$("$HERMES_BIN" -p "$profile" config get terminal.docker_volumes \
  | sed -n 's/^- //p')

added=0
for want in \
  "$workspace:/workspace" \
  "$ATTACHMENTS:$ATTACHMENTS:ro" \
  "$workspace:$workspace"
do
  if printf '%s\n' "$volumes" | grep -Fxq -- "$want"; then
    continue
  fi
  if [ -z "$volumes" ]; then
    volumes=$want
  else
    volumes=$(printf '%s\n%s' "$volumes" "$want")
  fi
  added=$((added + 1))
done

if [ "$added" -eq 0 ]; then
  echo "$profile already has all three entries; nothing written."
  exit 0
fi

# A JSON list, which the CLI parses into a real list; a bare string would be
# stored as a string and silently ignored by every reader of this key.
json=
while IFS= read -r entry; do
  [ -n "$entry" ] || continue
  if [ -z "$json" ]; then
    json="\"$entry\""
  else
    json="$json, \"$entry\""
  fi
done <<EOF
$volumes
EOF

"$HERMES_BIN" -p "$profile" config set terminal.docker_volumes "[$json]"

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
