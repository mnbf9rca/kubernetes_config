#!/bin/sh
# Finish the docker terminal setup for ONE named Hermes profile.
#
# Everything a profile needs for the docker terminal backend is already pinned
# for every profile by /etc/hermes/config.yaml (the managed scope): backend,
# image, run-as-host-user and cwd. The one thing that stays per profile is
# terminal.docker_volumes, and this script adds the three entries the default,
# emh and hal profiles carry:
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
# sandbox. The removal is a step of the update runbook, docs/operations/
# hermes-vm-updates.md.
#
# Canonical copy: hermes-vm/scripts/hermes-profile-docker-setup.sh in
# github.com/mnbf9rca/kubernetes_config. Installed on the VM at
# /home/hermes/.hermes/scripts/ beside the two cron scripts, for the same
# reason they live there: that directory rides the nightly `hermes backup` zip.
# THIS ONE IS NOT A CRON JOB. It is run by hand, once, per new profile, and it
# pushes to no monitor and reports to nothing.
set -eu

HERMES_BIN=/home/hermes/.local/bin/hermes
HERMES_HOME=/home/hermes/.hermes
ATTACHMENTS=$HERMES_HOME/webui/attachments

profile=${1:-}

if [ -z "$profile" ]; then
  echo "usage: hermes-profile-docker-setup.sh <profile-name>" >&2
  exit 2
fi

# The default profile is already configured, and its workspace is not under
# profiles/ at all, so the paths below would be wrong for it.
if [ "$profile" = default ]; then
  echo "default is already configured; nothing to do." >&2
  exit 2
fi

profile_dir=$HERMES_HOME/profiles/$profile
if [ ! -d "$profile_dir" ]; then
  echo "no profile directory at $profile_dir" >&2
  echo "create the profile first: $HERMES_BIN profile create $profile" >&2
  exit 1
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

  terminal.backend, docker_image, docker_run_as_host_user and cwd are pinned
  for every profile by /etc/hermes/config.yaml. Nothing to set here, and
  \`hermes config set\` refuses to change them - edit that file with sudo.

  Restart the profile's gateway (or the WebUI, if the profile is served
  through it) before the volumes take effect.

  The attachments mount comes out when hermes-webui ships the fix for
  upstream issue #6939.
EOF
