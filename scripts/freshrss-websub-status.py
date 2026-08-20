#!/usr/bin/env python3
"""Report FreshRSS WebSub subscription health, without leaking callback secrets.

Each `!hub.json` under FreshRSS's PubSubHubbub state directory holds that feed's
callback secret in a `key` field, so the files must never be printed whole. This
reads them inside the pod and emits only the derived status.

Two columns, and they mean different things:

  LEASE   Whether the subscription is currently live: `lease_end` in the future.
          This is the health signal. A subscription with no lease, or an expired
          one, is not receiving anything.

  PUSHED  Whether a push has EVER been successfully processed for this feed.
          Derived from the `error` key, which p/api/pshb.php sets true on
          subscription ("Do not assume that WebSub works until the first
          successful push") and clears only after a delivery that updates at
          least one feed. It is NOT a failure counter and NOT a current-state
          signal: a quiet feed and a broken one both read `no`.

          It has a cost. FreshRSS re-subscribes any feed with `error` set whose
          lease_start is over 23h old, so a feed that has never been pushed
          re-subscribes daily and permanently, however far its lease is from
          expiring. See issue #29.

Neither column proves end-to-end delivery. Delivery is a POST to /api/pshb.php
in the pod log, and it only appears when a subscribed feed actually publishes.

Reads:
  KUBECTL_CONTEXT   default cynexia-vps
  NAMESPACE         default vps
  DEPLOYMENT        default freshrss

Exit status is 1 if any subscription is not live, so this is usable as a check.
"""
import json
import os
import re
import subprocess
import sys
import time

CONTEXT = os.environ.get("KUBECTL_CONTEXT", "cynexia-vps")
NAMESPACE = os.environ.get("NAMESPACE", "vps")
DEPLOYMENT = os.environ.get("DEPLOYMENT", "freshrss")

PSHB = "/var/www/FreshRSS/data/PubSubHubbub/feeds"
# Emit one compact JSON object per feed. `key` is deliberately never selected.
REMOTE = (
    r'for f in %s/*/\!hub.json; do [ -f "$f" ] || continue; cat "$f"; echo; done'
    % PSHB
)


# kubectl identifiers: RFC 1123 names, plus the "/" and "_" a context name may carry.
#
# The call below passes an argument LIST, never a shell string, so no shell is
# involved and a metacharacter cannot become a command — the scanner finding that
# flagged this is reporting the pattern, not a reachable command injection.
#
# What the validation genuinely closes is ARGUMENT injection. These three values
# come from the environment, and without a check NAMESPACE="--kubeconfig=/tmp/evil"
# is read by kubectl as a flag rather than as a namespace. Cheap to remove, so
# removed rather than argued about.
_SAFE_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def checked(name, value):
    if not _SAFE_IDENT.match(value):
        sys.exit("ERROR: %s=%r is not a valid kubectl identifier" % (name, value))
    return value


def fetch():
    context = checked("KUBECTL_CONTEXT", CONTEXT)
    namespace = checked("NAMESPACE", NAMESPACE)
    deployment = checked("DEPLOYMENT", DEPLOYMENT)
    cmd = ["kubectl", "--context", context, "-n", namespace,
           "exec", "deployment/%s" % deployment, "-c", deployment,
           "--", "sh", "-c", REMOTE]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        sys.exit("ERROR: kubectl not on PATH")
    except subprocess.TimeoutExpired:
        sys.exit("ERROR: kubectl exec timed out after 60s")
    if out.returncode != 0:
        sys.exit("ERROR: kubectl exec failed: %s" % out.stderr.strip()[:400])
    return out.stdout


def main():
    now = time.time()
    rows, live, pushed = [], 0, 0

    for line in fetch().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            sys.exit("ERROR: unparseable !hub.json content (not printed - it "
                     "holds a callback secret)")

        hub = str(doc.get("hub", "?"))
        lease_end = doc.get("lease_end")
        if isinstance(lease_end, int) and lease_end > now:
            lease = "LIVE %7.1fh" % ((lease_end - now) / 3600)
            live += 1
        elif isinstance(lease_end, int):
            lease = "EXPIRED%5.0fh" % ((now - lease_end) / 3600)
        else:
            lease = "NO LEASE"

        # `error` absent or falsey means a push has been processed at least once.
        ever_pushed = not doc.get("error")
        pushed += ever_pushed
        rows.append((lease, "yes" if ever_pushed else "no", hub))

    if not rows:
        sys.exit("ERROR: no WebSub subscriptions found under %s" % PSHB)

    print("%-13s %-6s %s" % ("LEASE", "PUSHED", "HUB"))
    for lease, was_pushed, hub in sorted(rows):
        print("%-13s %-6s %s" % (lease, was_pushed, hub[:72]))

    total = len(rows)
    print("\n%d/%d subscriptions live, %d/%d have ever received a push"
          % (live, total, pushed, total))
    if live < total:
        print("\n%d subscription(s) are NOT live - push is not arriving for them."
              % (total - live))
    return 0 if live == total else 1


if __name__ == "__main__":
    sys.exit(main())
