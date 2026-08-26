#!/bin/sh
# Update the Hermes agent and its two venv passengers together, prove the result
# still serves, and roll back to last-good when it does not.
#
# DELIBERATELY NOT SCHEDULED. `hermes update` sometimes carries manual steps
# (config schema migrations), so this runs on demand — by hand, or from the
# 4-to-6-week update session — never from a timer. There is no .timer unit for
# THIS script and adding one is a design change, not a tidy-up. (The sibling
# hermes-app-alive.sh does have a timer; it is a heartbeat, not an update.)
#
# Canonical copy: hermes-vm/scripts/hermes-update.sh in
# github.com/mnbf9rca/kubernetes_config. Runbook:
# docs/operations/hermes-vm-updates.md. Installed on VM 103 at
# /home/hermes/bin/hermes-update.sh, run as the `hermes` user — normally through
# /usr/local/bin/hermes-update, which starts the systemd unit that runs this.
set -eu

# ---- pure helpers (unit-tested by test-hermes-update.sh) -------------------
# NO CONSTANTS IN THIS BLOCK. They arrive in the next append, beside their
# consumers: a constant with no reader is SC2034 and would make
# `make check-vm-scripts` fail on this very commit.

# True when $1 is exactly a 40-character lowercase hex object name — the shape
# `git rev-parse` prints. This is the webui phase's FAIL-CLOSED gate and it is
# what replaced the retired newest-tag policy: the checkout only ever moves to
# something that passed this. A failed fetch, a renamed default branch or a ref
# name instead of a SHA all fail it, and the caller exits BEFORE the tree moves.
#
# `case` matches the WHOLE argument, which is the point. The grep this replaced
# matched per LINE, so 'junk<newline>3b9c...' passed it — strictly more than the
# contract above allows, and enough to put a newline into the ping body that
# Task 2's `untaint` marker promises is 40 hex characters.
#
# The character list is enumerated rather than written as an `a-f` RANGE because
# range endpoints in a bracket expression are collation-dependent, and under
# some locales that range takes in uppercase letters. git never prints
# uppercase, so this must never accept it.
valid_sha40() {
  _s=${1:-}
  case $_s in
    '' | *[!0123456789abcdef]*) return 1 ;;
  esac
  [ ${#_s} -eq 40 ]
}

# True when $1 is exactly X.Y.Z with no prefix, suffix or fourth component, each
# component one or more digits. Whole-argument match for the same reason as
# valid_sha40: the grep this replaced accepted a conforming line inside
# multi-line text. The cases reject, in order, an empty string or any character
# outside digits and dots (a newline included), a fourth component, and anything
# without exactly two dots; the second `case` then rejects an empty component —
# a leading dot, a trailing dot, or '1..2'.
valid_semver() {
  _sv=${1:-}
  case $_sv in
    '' | *[!0123456789.]*) return 1 ;;
    *.*.*.*) return 1 ;;
    *.*.*) ;;
    *) return 1 ;;
  esac
  case $_sv in
    .* | *. | *..*) return 1 ;;
  esac
  return 0
}

# Seconds since the epoch of $1's mtime. GNU stat first, BSD stat second, so the
# tests run on the operator's macOS as well as on the VM.
file_mtime() {
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1"
}

# Whole days between $1's mtime and the epoch second $2. 9999 when $1 is absent,
# which is the same signal as "far too old" and keeps the caller branch-free.
apt_age_days() {
  if [ ! -f "$1" ]; then
    printf '9999\n'
    return 0
  fi
  _m=$(file_mtime "$1")
  printf '%s\n' "$(( ( $2 - _m ) / 86400 ))"
}

# Print the value of key $1 from $LAST_GOOD. Returns 1 when the file or the key
# is absent. The file is PARSED, never sourced: it is state this script wrote,
# but sourcing it would execute whatever a future bug puts there.
lg_get() {
  [ -f "$LAST_GOOD" ] || return 1
  _v=$(sed -n "s/^$1=\\(.*\\)\$/\\1/p" "$LAST_GOOD" | head -n 1)
  [ -n "$_v" ] || return 1
  printf '%s\n' "$_v"
}
