#!/usr/bin/env python3
"""Nuke karakeep's AI tags, re-tag every bookmark, sweep orphans.

One-shot tag-taxonomy cleanup. The re-tag job atomically replaces a
bookmark's AI-attached tags in karakeep's worker, so human-attached
tags survive untouched. After all jobs drain, deleteUnused mops up
the orphan tag rows.

Reads:
  KARAKEEP_URL       default http://localhost:3000 (use kubectl port-forward)
  KARAKEEP_API_KEY   required, admin user

Idempotent: safe to ctrl-C and re-run. The expensive enqueue step
replays cheaply because each bookmark's AI tags are replaced atomically.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

BASE = os.environ.get("KARAKEEP_URL", "http://localhost:3000").rstrip("/")
KEY = os.environ.get("KARAKEEP_API_KEY")
if not KEY:
    sys.exit("ERROR: KARAKEEP_API_KEY not set")

KCTX = os.environ.get("KUBECTX", "cynexia-vps")
NS = "vps"
DEPLOY = "deployment/karakeep"

HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def _http(req: urllib.request.Request, what: str):
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        raise SystemExit(f"HTTP {e.code} for {what}: {body}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Connection error for {what}: {e}\nIs port-forward running? (kubectl --context={KCTX} -n {NS} port-forward svc/karakeep 3000:3000)")


def trpc_query(proc: str, payload=None):
    inp = json.dumps({"0": {"json": payload if payload is not None else {}}})
    url = f"{BASE}/api/trpc/{proc}?batch=1&input={urllib.parse.quote(inp)}"
    res = _http(urllib.request.Request(url, headers=HEADERS, method="GET"), f"GET {proc}")
    return res[0]["result"]["data"]["json"]


def trpc_mutate(proc: str, payload=None):
    url = f"{BASE}/api/trpc/{proc}?batch=1"
    body = json.dumps({"0": {"json": payload if payload is not None else {}}}).encode()
    res = _http(urllib.request.Request(url, data=body, headers=HEADERS, method="POST"), f"POST {proc}")
    return res[0]["result"]["data"]["json"]


def sql(query: str) -> str:
    cmd = ["kubectl", f"--context={KCTX}", "-n", NS, "exec", DEPLOY,
           "-c", "sqlite-snapshot", "--", "sqlite3", "/data/db.db", query]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def stats_snapshot(label: str):
    # tag usage counts via SQL — fast and exact
    out = sql(
        "SELECT t.id, t.name, "
        "COALESCE((SELECT COUNT(*) FROM tagsOnBookmarks WHERE tagId=t.id), 0) AS total, "
        "COALESCE((SELECT COUNT(*) FROM tagsOnBookmarks WHERE tagId=t.id AND attachedBy='human'), 0) AS human "
        "FROM bookmarkTags t;"
    )
    rows = [l.split("|") for l in out.splitlines() if l.strip()]
    total_tags = len(rows)
    counts = [int(r[2]) for r in rows]
    human_only_tags = sum(1 for r in rows if int(r[3]) > 0)
    by_count = Counter(counts)
    print(f"  [{label}] total_tags={total_tags}  with_human_attachment={human_only_tags}")
    print(f"  [{label}] usage histogram (bookmarks_per_tag -> num_tags):")
    for k in sorted(by_count):
        if k <= 5 or k in (10, 20, 50, 100) or k == max(by_count):
            print(f"      {k:>4}: {by_count[k]:>4}")
    return rows


def main():
    print("=== Pre-flight ===")
    who = trpc_query("users.whoami")
    print(f"  user={who.get('email','?')}  (admin role check: skipped; relying on admin tRPC to 403 if not)")

    starting_rows = stats_snapshot("starting")
    snap = f"/tmp/karakeep-tag-snapshot-{int(time.time())}.json"
    with open(snap, "w") as f:
        json.dump({"rows": starting_rows}, f)
    print(f"  snapshot: {snap}")

    pre = trpc_query("admin.backgroundJobsStats")["inferenceStats"]
    print(f"  inferenceQueue (before): queued={pre['queued']} pending={pre['pending']} failed={pre['failed']}")
    if pre["queued"] + pre["pending"] > 0:
        print("  WARNING: queue not idle — proceeding anyway (the new bulk-enqueue will pile on)")

    print("\n=== Trigger bulk re-tag ===")
    trpc_mutate("admin.reRunInferenceOnAllBookmarks", {"type": "tag", "status": "all"})
    print("  enqueued.")

    print("\n=== Wait for drain ===")
    deadline = time.time() + 90 * 60  # 90 min hard cap
    start = time.time()
    last_total = None
    last_print = 0.0
    while time.time() < deadline:
        time.sleep(15)
        cur = trpc_query("admin.backgroundJobsStats")["inferenceStats"]
        total = cur["queued"] + cur["pending"]
        elapsed = time.time() - start
        # print on every change, or every 60s as a heartbeat
        if total != last_total or (time.time() - last_print) > 60:
            print(f"  t+{int(elapsed):>5}s  inflight={total:>4}  failed={cur['failed']}")
            last_total = total
            last_print = time.time()
        if total == 0:
            print("  drained.")
            break
    else:
        print("  WARNING: drain timed out at 90min; proceeding with cleanup")

    print("\n=== Sweep orphan tags ===")
    res = trpc_mutate("tags.deleteUnused", {})
    print(f"  deleted {res.get('deletedTags', '?')} orphan tags")

    print("\n=== Final stats ===")
    stats_snapshot("final")
    print(f"\nDone. Snapshot of starting state: {snap}")


if __name__ == "__main__":
    main()
