#!/bin/sh
# The keel dead-man's-switch. Runs daily in the `keel-fresh` CronJob and is the
# only thing that notices keel has stopped doing its job.
#
# THIS IS A DELIBERATE COPY of homelab/ops/scripts/keel-fresh.sh, not a shared
# file. kustomize will not read a generator source outside its own kustomization
# root, and the Makefile's doctrine is that copy-paste duplication beats
# abstraction at two instances. The two differ only in IMAGE_FLOOR, in the
# comments that say so, and in the path named in the PUSH_URL note below. EDIT
# THEM TOGETHER: a fix applied to one cluster and not the other is a check that
# has quietly stopped checking on the cluster nobody looked at.
#
# WHAT IT CATCHES, AND WHY A PROBE CANNOT. keel's liveness and readiness probes
# hit /healthz, which answers from the HTTP server. The registry poll loop is a
# separate goroutine. A wedged or crashed poll loop leaves the pod Running,
# Ready and green while every floating-tag workload in the estate silently stops
# receiving updates - the changedetection and uptime-kuma failure class, applied
# to the thing that updates everything else. Nothing else in the estate would
# ever report it.
#
# TWO SIGNALS, BOTH READ FROM keel'S OWN /metrics, NEVER FROM LOG TEXT:
#
#   1. POLLS  - a monotonic counter that advances on every registry scan.
#               Compared against the value this job stored on its previous run:
#               a counter that has not moved in 24 hours means the poll loop is
#               dead. keel's schedule here is every 6h per tracked image, so a
#               healthy day moves it by about four times the tracked-image count.
#   2. IMAGES - how many images keel's poll trigger currently tracks. This
#               catches the OTHER failure: keel alive and polling, but its
#               Deployment watch has fallen over, so it tracks nothing and polls
#               nothing that matters. A counter alone would read as healthy.
#
# BOTH COME FROM ONE ENDPOINT, AND THAT IS A CORRECTION TO THE ORIGINAL DESIGN.
# This job was planned to read the image count from keel's `/v1/tracked` REST
# listing. That endpoint does not exist on this deployment. Verified against the
# running 0.22.1 on 2026-08-26: keel registers its entire `/v1/*` admin API
# inside `if s.adminEnabled()`, which is false unless authentication is
# configured, and this Deployment sets no BASIC_AUTH_USER - so `/v1/tracked`
# answers 404, as does every other admin path. Only /healthz, /version and
# /metrics serve GET.
#
# The replacement is strictly better than the credentialed workaround the plan
# feared it would need. `poll_trigger_tracked_images` is a gauge on /metrics that
# keel sets to `len(tracked)` every time its poll watcher reconciles, so it is
# the same number the REST listing would have returned - from an endpoint that
# needs no credential, in the SAME response this job already fetches for the
# counter. One HTTP call, no auth, no second failure mode.
#
# THE COUNTER SUM IS MONOTONIC ACROSS A DE-KEELING, WHICH IS NOT OBVIOUS.
# `registries_scanned_total` is a Prometheus CounterVec with one series per
# image, and this script sums every series. Removing a workload's keel
# annotations makes keel stop scanning that image - but keel's `unwatch` only
# deletes the cron job and its internal map entry, never the Prometheus series
# (verified in trigger/poll/watcher.go). The retired series stays at its last
# value instead of vanishing, so the sum cannot fall. Without that, de-keeling a
# workload would subtract its accumulated count and manufacture a `polls-stalled`
# false alarm on the next run.
#
# THE STATE FILE IS THE ONLY WAY TO SAY "INCREASING". A stateless variant was
# considered and rejected: with no stored value the strongest available
# assertion is POLLS >= uptime_hours / 6, which after a few weeks of uptime is
# satisfied by a counter that froze yesterday. The state is two integers on a
# 32Mi PVC - no ServiceAccount, no RBAC, no API access.
#
# COUNTER RESETS ARE A REAL STATE, NOT A FAILURE. keel restarting sets POLLS back
# to a small number, which a naive comparison reads as "went backwards". The
# process start time is stored alongside the counter, so a restart is recognised
# and reported as `verdict=restarted` at exit 0 rather than as a false red.
#
# THE PUSH TOKEN REACHES THIS SCRIPT AS `PUSH_URL`, NOT AS ITS REAL NAME.
# Generated scripts ride the same envsubst stream as every manifest and envsubst
# substitutes the bare $NAME form as well as ${NAME}, so naming the allowlisted
# variable here - even in a comment - would publish the push token inside a
# ConfigMap. `make check-script-substitution` enforces the rename. The real name
# is in vps/ops/keel-fresh.yaml, where substitution is what is meant to
# happen. PUSH_URL arrives already carrying the token as its last path segment.
set -u

KEEL=http://keel.keel.svc.cluster.local:9300

# All three names verified against the live endpoint before this script was
# written (see the plan's Task 2 Step 1). None of them carries a `keel_` prefix -
# keel registers them unprefixed, which is why the originally planned
# `keel_registry_request_duration_seconds_count` matched nothing.
#
# registries_scanned_total: CounterVec, labelled by registry and image, one
# increment per registry scan. Monotonic; reset only by a process restart, which
# the start-time comparison below handles.
POLL_METRIC=registries_scanned_total
# poll_trigger_tracked_images: gauge, "How many images are tracked by poll
# trigger", re-Set on every reconcile of the watched set.
IMAGES_METRIC=poll_trigger_tracked_images
# The Go client library's default process collector. Its value is the process
# start time in Unix seconds, so it changes if and only if keel restarted. Note
# it is exposed in scientific notation (1.787750239e+09); awk's numeric coercion
# handles that, and the %d truncation to whole seconds is deliberate.
START_METRIC=process_start_time_seconds

# The literal floor for tracked images. It must be derived from the count that
# will be true AFTER this plan's de-keeling, not from the count observed today,
# and it must keep a container of headroom: the point of a floor is that losing
# one workload's annotations does not alarm while losing the WATCH does.
#
# The arithmetic, measured 2026-08-26 from this cluster's own
# poll_trigger_tracked_images: keel tracks 9 images on the VPS. That is already
# after Task 1 removed keel's own annotations and Task 4 removed meilisearch's,
# both of which are applied here, so 9 is the steady state rather than a number
# still due to fall.
#
# THE GAUGE COUNTS IMAGES, NOT WORKLOADS, WHICH IS WHY IT IS 9 AND NOT 8. Eight
# Deployments here carry the keel annotation set - cloudflared, karakeep, n8n,
# umami, uptime-kuma, freshrss, changedetection.io and sockpuppetbrowser - but
# four of them (freshrss, karakeep, n8n, uptime-kuma) also run an alpine:3.20
# quiesce sidecar, and keel tracks the images of every container in an annotated
# workload. Those four contribute one shared ninth image between them. Anyone
# reconciling this number against the workload list will be off by one until
# they account for that sidecar.
#
# meilisearch is NOT in the tracked set any more, but its registries_scanned_total
# series is still on /metrics at its last value - the retired-series behaviour the
# header describes, observed here rather than reasoned about: ten counter series,
# nine tracked images.
#
# So the steady-state count is 11: pinepods joined the keel set on 2026-08-27
# and, like those four, contributes MORE THAN ONE image - pinepods:latest plus
# its own valkey/valkey:8-alpine sidecar - so it moves the count by two, not
# one. The floor is 9, two below. Setting it to 11 would leave ZERO margin,
# which is the failure this constant exists to avoid. Confirm the 11 against
# poll_trigger_tracked_images after the first apply.
#
# Raise it deliberately when the estate grows; a floor that drifts below reality
# is a check that has stopped checking.
IMAGE_FLOOR=9

STATE_DIR=/state
STATE_FILE=$STATE_DIR/last

# ---- uptime-kuma push with a short message --------------------------------
# The dead-man's-switch for this job is an uptime-kuma PUSH monitor, not a
# healthchecks.io check. Two consequences, both deliberate:
#
#   NO /start PING. The push API has no such concept: a push is a heartbeat
#   carrying a status. So `activeDeadlineSeconds` on the CronJob is the WHOLE of
#   the hang bound, and the monitor's own heartbeat interval plus retries is the
#   silence bound. A run that starts and wedges is killed by the deadline and
#   then shows up as a missing heartbeat, which is the same alarm one step later.
#
#   THE MESSAGE IS SHORT AND FIXED-SHAPE. kuma stores one `msg` string per
#   heartbeat and shows it in the alert, so it carries the verdict from the enum
#   below plus one or two integers - nothing else. The full diagnostic stays in
#   this pod's log. That is a real loss of forensics against a healthchecks.io
#   body, and it is the accepted price of the check budget: read the pod log
#   before its ttlSecondsAfterFinished expires, and the ping history in kuma
#   after that.
#
# NEVER EMIT A COMMAND'S OUTPUT. A failing curl quotes the URL, and for a push
# the URL carries the monitor's token. Everything emitted below is a digit-gated
# integer or a verdict from the fixed enum in the trap. `emit` is deliberately
# still called `emit`: `make check-ping-bodies` recognises body sinks by FUNCTION
# NAME (`emit`, `say_err`, `fatal`), not by the ping host, so keeping the name
# keeps this script under the guard, `untaint` comments and all.
#
# `true >`, not `: >`: a redirection error on a POSIX special built-in aborts the
# shell even behind `|| true`. The stderr redirection PRECEDES the message
# redirection so the shell's own "cannot create" diagnostic reaches the pod log.
# Tokens are space-separated on ONE line, because a kuma msg is one line.
MSG_FILE=/tmp/kuma-msg
msg_reset() { true 2>/dev/null > "$MSG_FILE" || true; }
# shellcheck disable=SC2329 # called only from on_exit, which runs from the EXIT trap.
emit() { { printf '%s ' "$*" | LC_ALL=C tr -cd '\040-\176'; } 2>/dev/null >> "$MSG_FILE" || true; }

# GET https://uptime.cynexia.com/api/push/<token>?status=up|down&msg=<short>
# `-G --data-urlencode` builds the query safely: the message never has to be
# escaped by hand, and no value is interpolated into the URL string. The msg is
# capped at 200 characters because kuma stores it in one column and an alert
# nobody can read is worse than a shorter one.
# shellcheck disable=SC2329 # called only from on_exit, which runs from the EXIT trap.
push_kuma() {
  _st=$1
  _m=$(cut -c1-200 "$MSG_FILE" 2>/dev/null) || _m=""
  curl -fsS -m 15 -o /dev/null -G \
    --data-urlencode "status=$_st" \
    --data-urlencode "msg=$_m" \
    "$PUSH_URL" || echo "kuma: push not delivered" >&2
  msg_reset
  return 0
}

# ---- body values ----------------------------------------------------------
# VERDICT is a fixed enum and starts at the failure that is true before anything
# has run; each successful phase narrows it. The enum in full:
#   metrics-unreachable  keel's /metrics did not answer 200
#   metric-missing       /metrics answered but did not carry all three metrics
#   too-few-images       keel tracks fewer images than the floor
#   polls-stalled        the counter has not moved since the previous run
#   first-run            no stored state; nothing to compare against
#   restarted            keel restarted, so the counter legitimately reset
#   ok                   counter moved and the image count is at or above floor
#
# There is deliberately no `tracked-unreachable`: the image count comes from the
# same /metrics response as the counter, so there is no second endpoint that
# could fail on its own. `metrics-unreachable` covers both.
VERDICT=metrics-unreachable
POLLS=0
LAST_POLLS=0
POLLS_DELTA=0
IMAGES=0
START_EPOCH=0
LAST_START=0

# shellcheck disable=SC2329 # invoked by `trap ... EXIT` below, not by name.
on_exit() {
  _xrc=$?
  trap - EXIT
  msg_reset
  # SHORT AND FIXED-SHAPE. One verdict from the enum plus two counters: the
  # poll delta, which is the signal, and the tracked-image count against its
  # floor. Everything else - the metric names, the stored state, the resolved
  # endpoint - stays in the pod log above.
  emit "verdict=$VERDICT"
  emit "polls_delta=$POLLS_DELTA"
  emit "images=$IMAGES/$IMAGE_FLOOR"
  if [ "$_xrc" -eq 0 ]; then
    push_kuma up
  else
    push_kuma down
  fi
  exit "$_xrc"
}
trap on_exit EXIT

msg_reset

# ---- 1. read /metrics ------------------------------------------------------
# One request, parsed three times. `-f` so a non-2xx is a failure rather than an
# error page that awk would silently sum to zero.
METRICS=/tmp/keel-metrics
if ! curl -fsS -m 20 -o "$METRICS" "$KEEL/metrics"; then
  echo "ERROR: keel /metrics did not answer" >&2
  exit 1
fi

# ---- 2. assert all three metrics are PRESENT before reading any value -------
# An absent metric must never read as zero. Presence is checked separately from
# value because for the image gauge the two mean different things: absent is an
# upstream rename (metric-missing), whereas a present zero is keel tracking
# nothing at all (too-few-images), and conflating them would send the operator
# to the wrong runbook. The three names are script literals matching ^[a-z_]+$,
# so using them as a grep pattern carries no injection concern.
metric_seen() { grep -q "^$1[ {]" "$METRICS"; }

if ! metric_seen "$POLL_METRIC" || ! metric_seen "$START_METRIC" \
   || ! metric_seen "$IMAGES_METRIC"; then
  VERDICT=metric-missing
  echo "ERROR: /metrics answered but did not carry all three expected metrics" >&2
  exit 1
fi

# Sum the counter across every label set. `index($1, m "{") == 1 || $1 == m`
# covers both the labelled and unlabelled forms; a Prometheus text line is
# `name{labels} value`, whitespace separated, so $2 is the value. keel's label
# values are registry and image names, neither of which contains a space.
POLLS=$(awk -v m="$POLL_METRIC" '
  index($1, m "{") == 1 || $1 == m { s += $2 }
  END { printf "%d", s }' "$METRICS")
case "$POLLS" in ''|*[!0-9]*) POLLS=0 ;; esac
# check-ping-bodies: untaint POLLS - awk sum of one Prometheus counter, gated to digits by the case above; never a slice of the response

# The start time is a float in scientific notation; awk coerces it and %d
# truncates to whole seconds, which is all the restart comparison needs.
START_EPOCH=$(awk -v m="$START_METRIC" '
  index($1, m "{") == 1 || $1 == m { v = $2 }
  END { printf "%d", v }' "$METRICS")
case "$START_EPOCH" in ''|*[!0-9]*) START_EPOCH=0 ;; esac
# check-ping-bodies: untaint START_EPOCH - awk read of one Prometheus gauge, gated to digits by the case above; never emitted, used only to detect a restart

# The tracked-image gauge. Same idiom; the gauge is unlabelled today, and the
# labelled branch costs nothing if that ever changes.
IMAGES=$(awk -v m="$IMAGES_METRIC" '
  index($1, m "{") == 1 || $1 == m { v = $2 }
  END { printf "%d", v }' "$METRICS")
case "$IMAGES" in ''|*[!0-9]*) IMAGES=0 ;; esac
# check-ping-bodies: untaint IMAGES - awk read of one Prometheus gauge, gated to digits by the case above; never a slice of the response

if [ "$START_EPOCH" -eq 0 ]; then
  # The metric is present but unreadable as an epoch. Fail closed and name it.
  VERDICT=metric-missing
  echo "ERROR: $START_METRIC present but did not parse as an epoch" >&2
  exit 1
fi

# ---- 3. the image floor ----------------------------------------------------
if [ "$IMAGES" -lt "$IMAGE_FLOOR" ]; then
  VERDICT=too-few-images
  echo "ERROR: keel tracks $IMAGES image(s), floor is $IMAGE_FLOOR" >&2
  exit 1
fi

# ---- 4. compare against the previous run -----------------------------------
# The state file is two integers on one line: the start epoch and the counter.
# Anything that is not two USABLE integers - missing, truncated, non-numeric, or
# a line carrying extra fields - is treated as absent, which costs one
# `first-run` verdict and never a wrong answer.
#
# A STORED COUNTER OF ZERO IS ALSO ABSENT, AND THAT IS LOAD-BEARING. `read` with
# three or more fields on the line puts the whole remainder into LAST_POLLS, so
# the embedded space fails the digit gate and zeroes it - while LAST_START
# parsed fine and stays valid. A partial write does the same. Without the
# `-eq 0` arm below, that combination skips the `first-run` guard and computes
# the delta against zero, which is ALWAYS positive because the counter has
# already been proven non-zero. The run would report `verdict=ok` with an
# inflated delta and hold the monitor UP over a genuinely dead poll loop, for a
# whole day. A stored zero is never a legitimate value to compare against: it is
# either that corruption, or the restart branch below recording a keel that had
# not scanned yet, and both want a fresh baseline rather than a comparison.
if [ -r "$STATE_FILE" ]; then
  read -r LAST_START LAST_POLLS < "$STATE_FILE" || true
fi
case "${LAST_START:-}" in ''|*[!0-9]*) LAST_START=0 ;; esac
case "${LAST_POLLS:-}" in ''|*[!0-9]*) LAST_POLLS=0 ;; esac

write_state() {
  # A failed write must not fail the run: the next run then sees `first-run`,
  # which is green-with-evidence, not a false red.
  printf '%s %s\n' "$START_EPOCH" "$POLLS" > "$STATE_FILE" 2>/dev/null \
    || echo "WARNING: could not write $STATE_FILE" >&2
}

# THE RESTART BRANCH GETS FIRST REFUSAL, BEFORE THE ZERO-COUNTER CHECK BELOW.
# keel restarting sets the counter back to zero and only raises it on its first
# registry scan, so there is a window of seconds between process start and first
# scan in which the counter is legitimately zero. This job's own `backoffLimit`
# retry lands about ten seconds after a failure, likely still inside that
# window, so testing the counter first would report a red `polls-stalled` for a
# keel that had simply just started. The stored start epoch is what distinguishes
# the two, so it is consulted first.
if [ "$LAST_START" -ne 0 ] && [ "$START_EPOCH" -ne "$LAST_START" ]; then
  # keel restarted between runs, so the counter reset legitimately. A restarting
  # keel is a keel that polls on start; the image floor above is the assertion
  # that carries this run.
  VERDICT=restarted
  POLLS_DELTA=0
  write_state
  echo "==> keel restarted since the last run; counter reset is expected"
  exit 0
fi

# A present counter summing to zero means keel has tracked images but has never
# scanned one: the stalled poll loop, caught without waiting a day for a delta.
#
# THIS SITS ABOVE `first-run`, NOT BELOW IT, AND THE ORDER IS DELIBERATE. A
# stored zero reads as absent, so if this check ran after the `first-run` guard
# a permanently-zero counter would store zero, read it back as absent, and take
# the green `first-run` branch again on every subsequent run - a wedged keel,
# green forever. Placed here it is reached in every case except the restart
# above, which is the only one where a zero counter is explainable.
if [ "$POLLS" -eq 0 ]; then
  VERDICT=polls-stalled
  echo "ERROR: $POLL_METRIC is present but sums to zero; no registry ever scanned" >&2
  exit 1
fi

if [ "$LAST_START" -eq 0 ] || [ "$LAST_POLLS" -eq 0 ]; then
  # Green on evidence, not on assumption: reaching here means /metrics answered,
  # all three metrics are present, the counter is non-zero and keel tracks at
  # least IMAGE_FLOOR images.
  VERDICT=first-run
  POLLS_DELTA=0
  write_state
  echo "==> first run: stored start=$START_EPOCH polls=$POLLS"
  exit 0
fi

POLLS_DELTA=$((POLLS - LAST_POLLS))
if [ "$POLLS_DELTA" -le 0 ]; then
  VERDICT=polls-stalled
  write_state
  echo "ERROR: registry poll counter has not moved in a day (delta $POLLS_DELTA)" >&2
  exit 1
fi

VERDICT=ok
write_state
echo "==> ok: polls +$POLLS_DELTA over $IMAGES tracked image(s)"
exit 0
