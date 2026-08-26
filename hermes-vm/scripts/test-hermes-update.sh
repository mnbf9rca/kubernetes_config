#!/bin/sh
# Unit tests for the pure helpers in hermes-update.sh.
#
# The script is sourced with HERMES_UPDATE_LIB_ONLY=1, which defines every
# function and runs nothing. These tests must pass on the operator's macOS as
# well as on the Debian VM, so nothing here uses a GNU-only flag.
#
# Run: sh hermes-vm/scripts/test-hermes-update.sh   (or `make check-vm-scripts`)
set -eu

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
HERMES_UPDATE_LIB_ONLY=1
export HERMES_UPDATE_LIB_ONLY
# `source-path=SCRIPTDIR` makes ShellCheck resolve the directive below relative
# to THIS file rather than to the caller's working directory, so `make
# check-vm-scripts` (run from the repo root) and a direct `sh
# hermes-vm/scripts/test-hermes-update.sh` both work. Without it, and without
# the -x flag, SC1091 fires at info level and the default `style` severity
# floor makes the check exit 1.
#
# NO COMMENT LINE MAY BEGIN WITH `# shellcheck` EXCEPT A REAL DIRECTIVE.
# ShellCheck parses any such line as one and answers SC1073/SC1072 (both
# ERRORS) when it does not parse — so a sentence that merely starts with the
# tool's name fails the very check it is explaining. Write `ShellCheck` or
# reflow the line. This bit an earlier draft of this plan.
# shellcheck source-path=SCRIPTDIR
# shellcheck source=hermes-update.sh
. "$HERE/hermes-update.sh"

FAILED=0
pass() { printf 'ok   - %s\n' "$1"; }
fail() { printf 'FAIL - %s\n' "$1"; FAILED=1; }
assert_eq() {  # want got name
  if [ "$1" = "$2" ]; then pass "$3"; else fail "$3 (want '$1', got '$2')"; fi
}
assert_rc() {  # want-rc got-rc name
  if [ "$1" = "$2" ]; then pass "$3"; else fail "$3 (want rc=$1, got rc=$2)"; fi
}

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# ---- valid_sha40 -----------------------------------------------------------
# The gate that replaced the tag policy. `git rev-parse` prints 40 lowercase hex
# characters; ANYTHING else means the fetch failed, the branch is gone or the
# argument was a ref name, and the caller must refuse to move the working tree
# rather than guess. The uppercase case matters: git never prints it, so
# accepting it would mean the gate was written against a shape nothing produces.
for good in \
  3b9c632a1fa339abcfd457973dcf10810640e760 \
  0000000000000000000000000000000000000000 ; do
  if valid_sha40 "$good"; then pass "valid_sha40 accepts a 40-hex object name"
  else fail "valid_sha40 rejected $good"; fi
done
for bad in '' 3b9c632a HEAD master origin/master \
  3B9C632A1FA339ABCFD457973DCF10810640E760 \
  3b9c632a1fa339abcfd457973dcf10810640e76 \
  3b9c632a1fa339abcfd457973dcf10810640e7601 \
  'v0.52.113' ; do
  if valid_sha40 "$bad"; then fail "valid_sha40 accepted '$bad'"
  else pass "valid_sha40 rejects '$bad'"; fi
done
# The multi-line cases are the regression: the first implementation piped into
# `grep -qE`, which matches per LINE, so ANY input holding one conforming line
# passed regardless of the rest. That is strictly more than "exactly a
# 40-character lowercase hex object name", and a value with a newline in it
# would break the one-key-per-line ping-body format. These are named
# individually rather than added to the loop above because a newline inside
# "$bad" would split the `ok` line in two.
MULTI=$(printf 'notasha\n3b9c632a1fa339abcfd457973dcf10810640e760')
if valid_sha40 "$MULTI"; then fail "valid_sha40 accepted a SHA on a later line"
else pass "valid_sha40 rejects a conforming line preceded by junk"; fi
MULTI=$(printf '3b9c632a1fa339abcfd457973dcf10810640e760\n3b9c632a1fa339abcfd457973dcf10810640e760')
if valid_sha40 "$MULTI"; then fail "valid_sha40 accepted two conforming lines"
else pass "valid_sha40 rejects two conforming lines"; fi

# ---- valid_semver ----------------------------------------------------------
for good in 0.9.1 1.0.0 10.20.30; do
  if valid_semver "$good"; then pass "valid_semver accepts $good"
  else fail "valid_semver rejected $good"; fi
done
for bad in '' 0.9 v0.9.1 0.9.1a 0.9.1.2 'x'; do
  if valid_semver "$bad"; then fail "valid_semver accepted '$bad'"
  else pass "valid_semver rejects '$bad'"; fi
done
MULTI=$(printf 'not-a-version\n0.9.1')
if valid_semver "$MULTI"; then fail "valid_semver accepted a version on a later line"
else pass "valid_semver rejects a conforming line preceded by junk"; fi
MULTI=$(printf '0.9.1\n1.0.0')
if valid_semver "$MULTI"; then fail "valid_semver accepted two conforming lines"
else pass "valid_semver rejects two conforming lines"; fi

# ---- file_mtime ------------------------------------------------------------
# The apt_age_days assertions below take their expected values FROM file_mtime,
# so a file_mtime that returned a wrong-but-stable number — ctime instead of
# mtime, or a BSD/GNU field mix-up — would leave all three green. These two pin
# it directly, and neither expected value passes through the function under
# test: the first comes from `date`, the second from the `touch -m` above it.
#
# GNU date first, BSD date second — the mirror of file_mtime's own fallback,
# but a DIFFERENT tool, so whichever half of that fallback this machine takes,
# the oracle is independent of it.
epoch_of() {  # 'CCYY-MM-DD hh:mm:ss' in local time
  date -d "$1" +%s 2>/dev/null || date -j -f '%Y-%m-%d %H:%M:%S' "$1" +%s
}

PROBE=$WORK/mtime-probe
: > "$PROBE"
touch -m -t 202001020304.05 "$PROBE"
WANT_MT=$(epoch_of '2020-01-02 03:04:05')
assert_eq "$WANT_MT" "$(file_mtime "$PROBE")" \
  "file_mtime returns the file's mtime as an epoch second"

# -a moves ONLY atime and chmod moves ONLY ctime, so a file_mtime reading
# either field would now disagree with the mtime `touch -m` pinned above.
touch -a -t 203001020304.05 "$PROBE"
chmod 600 "$PROBE"
assert_eq "$WANT_MT" "$(file_mtime "$PROBE")" \
  "file_mtime reads mtime, not atime or ctime"

# ---- apt_age_days ----------------------------------------------------------
NOW=1000000000
STAMP=$WORK/stamp
: > "$STAMP"
assert_eq "9999" "$(apt_age_days "$WORK/absent" "$NOW")" \
  "apt_age_days reports 9999 for a missing stamp"

MT=$(file_mtime "$STAMP")
assert_eq "0" "$(apt_age_days "$STAMP" "$MT")" \
  "apt_age_days reports 0 for a stamp written now"
assert_eq "20" "$(apt_age_days "$STAMP" "$(( MT + 20 * 86400 ))")" \
  "apt_age_days reports whole days elapsed"
assert_eq "13" "$(apt_age_days "$STAMP" "$(( MT + 14 * 86400 - 1 ))")" \
  "apt_age_days truncates rather than rounds"

# ---- lg_get ----------------------------------------------------------------
LAST_GOOD=$WORK/last-good
cat > "$LAST_GOOD" <<'EOF'
agent_sha=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
webui_sha=3b9c632a1fa339abcfd457973dcf10810640e760
client_version=0.9.1
stamp=1000000000
EOF
assert_eq "3b9c632a1fa339abcfd457973dcf10810640e760" "$(lg_get webui_sha)" "lg_get reads a key"
assert_eq "0.9.1" "$(lg_get client_version)" "lg_get reads another key"
set +e
lg_get nosuchkey >/dev/null 2>&1
rc=$?
set -e
assert_rc 1 "$rc" "lg_get fails on a missing key"

LAST_GOOD=$WORK/absent-last-good
set +e
lg_get webui_sha >/dev/null 2>&1
rc=$?
set -e
assert_rc 1 "$rc" "lg_get fails when the file does not exist"

# ---- parse_mode ------------------------------------------------------------
assert_eq "run"  "$(parse_mode)"        "parse_mode defaults to run"
assert_eq "seed" "$(parse_mode --seed)" "parse_mode understands --seed"
# In a SUBSHELL, and with errexit off. `exit` inside a shell function exits the
# SHELL, not the function, so a bare `parse_mode --wat` kills this test run
# mid-suite: no verdict, `make check-vm-scripts` red. The parentheses contain
# the exit; `set +e` stops errexit from killing the script on the subshell's own
# non-zero status. Both are required — verified under sh, dash and bash.
set +e
( parse_mode --wat ) >/dev/null 2>&1
rc=$?
set -e
assert_rc 64 "$rc" "parse_mode rejects an unknown argument with EX_USAGE"

# ---- classify_chat ---------------------------------------------------------
assert_eq "ok"  "$(classify_chat 200)" "classify_chat accepts 2xx"
assert_eq "ok"  "$(classify_chat 204)" "classify_chat accepts any 2xx"
assert_eq "bad" "$(classify_chat 401)" "classify_chat rejects 401"
assert_eq "bad" "$(classify_chat 000)" "classify_chat rejects a connection failure"
assert_eq "bad" "$(classify_chat '')"  "classify_chat rejects an empty status"

if [ "$FAILED" -ne 0 ]; then
  printf '\nFAILED\n'; exit 1
fi
printf '\nAll helper tests passed.\n'
