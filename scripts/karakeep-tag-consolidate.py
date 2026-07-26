#!/usr/bin/env python3
"""Karakeep tag consolidation via LLM-driven clustering + tags.merge.

Strategy: instead of re-tagging (which regenerates a long tail), take the
existing tags as input, ask a strong model to cluster them into a target
taxonomy of N tags, then apply the merges via the tags.merge endpoint.
Bookmark associations are preserved because merge moves attachments.

Dry-run by default. Pass --apply to actually merge.

Env:
  KARAKEEP_URL       default http://localhost:3000 (use kubectl port-forward)
  KARAKEEP_API_KEY   required, admin user
  OPENAI_API_KEY     required, for the clustering call
  OPENAI_MODEL       default gpt-5.5
  TARGET_N           default 40
"""
import argparse
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
KEY = os.environ.get("KARAKEEP_CLEANUP_API_KEY") or os.environ.get("KARAKEEP_API_KEY")
OPENAI_KEY = os.environ.get("KARAKEEP_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
TARGET_N = int(os.environ.get("TARGET_N", "40"))
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-pro")

KCTX = os.environ.get("KUBECTX", "cynexia-vps")
NS = "vps"
DEPLOY = "deployment/karakeep"


# ---- karakeep tRPC ----
def _http(req: urllib.request.Request, what: str):
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read().decode()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:600]
        raise SystemExit(f"HTTP {e.code} for {what}: {body}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Connection error for {what}: {e}\nIs port-forward up?")


def trpc_query(proc, payload=None):
    inp = json.dumps({"0": {"json": payload if payload is not None else {}}})
    url = f"{BASE}/api/trpc/{proc}?batch=1&input={urllib.parse.quote(inp)}"
    h = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    res = _http(urllib.request.Request(url, headers=h, method="GET"), f"GET {proc}")
    return res[0]["result"]["data"]["json"]


def trpc_mutate(proc, payload=None):
    url = f"{BASE}/api/trpc/{proc}?batch=1"
    body = json.dumps({"0": {"json": payload if payload is not None else {}}}).encode()
    h = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    res = _http(urllib.request.Request(url, data=body, headers=h, method="POST"), f"POST {proc}")
    return res[0]["result"]["data"]["json"]


# ---- sqlite via sidecar ----
def fetch_tag_data():
    """Return [{id, name, count, samples}] for every tag."""
    sql = (
        "SELECT t.id, t.name, "
        "COALESCE((SELECT COUNT(*) FROM tagsOnBookmarks WHERE tagId=t.id), 0) AS cnt, "
        "COALESCE((SELECT GROUP_CONCAT(SUBSTR(COALESCE(b.title, bl.title, bl.url, b.id), 1, 80), '||') "
        "          FROM tagsOnBookmarks tob "
        "          JOIN bookmarks b ON b.id = tob.bookmarkId "
        "          LEFT JOIN bookmarkLinks bl ON bl.id = b.id "
        "          WHERE tob.tagId = t.id LIMIT 5), '') AS samples "
        "FROM bookmarkTags t;"
    )
    cmd = ["kubectl", f"--context={KCTX}", "-n", NS, "exec", DEPLOY,
           "-c", "sqlite-snapshot", "--",
           "sqlite3", "/data/db.db", "-separator", "\t", sql]
    raw = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    rows = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            rows.append({
                "id": parts[0],
                "name": parts[1],
                "count": int(parts[2]) if parts[2].isdigit() else 0,
                "samples": [s for s in parts[3].split("||") if s] if parts[3] else [],
            })
    return rows


# ---- OpenAI ----
# Use curl rather than urllib so macOS system CA store is used. Python's urllib
# on macOS often lacks CA roots (your Python install may be from platformio /
# pyenv / Homebrew without the certifi bundle wired in), giving SSL cert verify
# errors against api.openai.com. curl uses /etc/ssl/cert.pem which has the
# normal CA chain.
def call_openai(prompt: str, max_completion_tokens=15000):
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system",
             "content": "You are a taxonomy expert. Cluster tags into useful categories. "
                        "Always reply with strict JSON conforming to the requested schema."},
            {"role": "user", "content": prompt},
        ],
        "max_completion_tokens": max_completion_tokens,
        "response_format": {"type": "json_object"},
    })
    proc = subprocess.run(
        ["curl", "-sS", "--fail-with-body", "--max-time", "600",
         "-H", f"Authorization: Bearer {OPENAI_KEY}",
         "-H", "Content-Type: application/json",
         "--data-binary", "@-",
         "https://api.openai.com/v1/chat/completions"],
        input=body, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"OpenAI call failed: rc={proc.returncode}\nstderr={proc.stderr[:400]}\nstdout={proc.stdout[:400]}")
    try:
        d = json.loads(proc.stdout)
    except json.JSONDecodeError:
        sys.exit(f"OpenAI returned non-JSON:\n{proc.stdout[:800]}")
    if "error" in d:
        sys.exit(f"OpenAI error: {d['error']}")
    usage = d.get("usage", {})
    print(f"  tokens: prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')}")
    return d["choices"][0]["message"]["content"]


def build_prompt(tags, target_n):
    lines = [
        f"You will receive a list of {len(tags)} tags from a personal bookmark collection.",
        f"Cluster them into approximately {target_n} target categories (a few more or fewer is fine).",
        "",
        "COMPLETENESS — THIS IS LOAD-BEARING:",
        f"- Every one of the {len(tags)} source tag IDs MUST appear in exactly one cluster.",
        f"- BEFORE you return your JSON, count the total number of source_tag_ids across all your",
        f"  clusters and verify it equals {len(tags)}. If it doesn't, revise until it does.",
        "- Do not invent tag IDs. Use only IDs from the input list.",
        "- If you can't categorise a tag confidently, put it in a cluster called 'miscellaneous'.",
        "  Better one misc bucket than dropping the tag.",
        "",
        "QUALITY:",
        "- Cluster names: lowercase, spaces allowed, British English. Prefer reusing an existing",
        "  source tag name as the cluster label when one is a natural fit; only invent a new name",
        "  if no existing tag is a clear cluster label.",
        "- Cluster names should be short noun phrases describing a searchable topic (not adjectives or qualities).",
        "- Prefer larger, broader clusters over many tiny ones. A cluster covering only one or two",
        "  bookmarks is acceptable only for genuinely unique long-tail topics.",
        "- Bookmarks not topics: cluster names describe what a bookmark is *about*, not how it's",
        "  presented (no 'tutorial', 'guide', 'opinion').",
        "",
        "OUTPUT a single JSON object with this shape (no markdown, no commentary):",
        '{"clusters": [',
        '  {"target_name": "kubernetes",',
        '   "source_tag_ids": ["id1", "id2", ...],',
        '   "rationale": "all k8s / kubernetes / k3s / helm tags"},',
        '  ...',
        ']}',
        "",
        "INPUT (one tag per line: id|name|count|sample_titles_joined_by_||):",
        "",
    ]
    for t in tags:
        samples = " | ".join(t["samples"][:3])[:240]
        # strip pipes from name/samples to avoid format ambiguity
        n = t["name"].replace("|", "/")
        s = samples.replace("|", "/")
        lines.append(f"{t['id']}|{n}|{t['count']}|{s}")
    return "\n".join(lines)


# ---- main ----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually apply merges. Without this flag, prints proposal and exits.")
    ap.add_argument("--proposal", type=str, default=None,
                    help="Skip the OpenAI call and read a proposal JSON from disk. Use with --apply.")
    args = ap.parse_args()

    if not KEY: sys.exit("KARAKEEP_CLEANUP_API_KEY (or KARAKEEP_API_KEY) not set")
    if not OPENAI_KEY and not args.proposal: sys.exit("KARAKEEP_OPENAI_API_KEY (or OPENAI_API_KEY) not set")

    print(f"=== Pre-flight ===")
    print(f"  model={MODEL} target_n={TARGET_N} apply={args.apply}")

    tags = fetch_tag_data()
    by_count = Counter(t["count"] for t in tags)
    print(f"  starting: {len(tags)} tags  histogram={dict(sorted(by_count.items())[:12])}")

    snap = f"/tmp/karakeep-cluster-snapshot-{int(time.time())}.json"
    with open(snap, "w") as f:
        json.dump(tags, f)
    print(f"  snapshot: {snap}")

    name_by_id = {t["id"]: t["name"] for t in tags}
    count_by_id = {t["id"]: t["count"] for t in tags}
    src_ids = {t["id"] for t in tags}

    # ---- get proposal (either OpenAI or from disk) ----
    if args.proposal:
        with open(args.proposal) as f:
            result = json.load(f)
        print(f"  loaded proposal from {args.proposal}")
    else:
        print(f"\n=== Build prompt + call {MODEL} ===")
        prompt = build_prompt(tags, TARGET_N)
        approx_tokens = len(prompt) // 4
        print(f"  prompt: {len(prompt)} chars (~{approx_tokens} tokens)")
        raw = call_openai(prompt, max_completion_tokens=20000)
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            err_path = f"/tmp/karakeep-cluster-raw-{int(time.time())}.txt"
            with open(err_path, "w") as f:
                f.write(raw)
            sys.exit(f"OpenAI returned invalid JSON. Saved raw to {err_path}")

    clusters = result.get("clusters", [])

    # ---- deterministic fixup: drop extras, dedup, route missing to 'miscellaneous' ----
    # The model is unreliable about completeness on large inputs. Rather than refusing
    # to apply (which leaves the user stuck), normalise the proposal deterministically.
    print(f"\n=== Deterministic fixup ===")
    seen_set = set()
    for c in clusters:
        cleaned = []
        for tid in c.get("source_tag_ids", []):
            if tid in src_ids and tid not in seen_set:
                cleaned.append(tid)
                seen_set.add(tid)
            # silently drops: extras (not in DB) and duplicates (already assigned)
        c["source_tag_ids"] = cleaned

    missing = src_ids - seen_set
    if missing:
        misc = next((c for c in clusters if c.get("target_name", "").lower() == "miscellaneous"), None)
        if misc is None:
            misc = {"target_name": "miscellaneous",
                    "source_tag_ids": [],
                    "rationale": "deterministic fallback: tags the model omitted"}
            clusters.append(misc)
        misc["source_tag_ids"].extend(sorted(missing))
        print(f"  routed {len(missing)} unassigned tags into 'miscellaneous'")
    # remove empty clusters after dedup
    clusters[:] = [c for c in clusters if c.get("source_tag_ids")]

    # ---- re-validate after fixup ----
    all_sources = []
    for c in clusters:
        all_sources.extend(c.get("source_tag_ids", []))
    seen = Counter(all_sources)
    missing = src_ids - set(seen.keys())
    extra = set(seen.keys()) - src_ids
    dups = [tid for tid, c in seen.items() if c > 1]

    print(f"\n=== Validation (post-fixup) ===")
    print(f"  {len(clusters)} clusters, {len(all_sources)} source-tag assignments")
    if missing or extra or dups:
        print(f"  STILL WRONG: missing={len(missing)} extra={len(extra)} duplicated={len(dups)}")
        if list(missing)[:8]:
            print(f"    missing sample names: {[name_by_id.get(i,'?') for i in list(missing)[:8]]}")
    else:
        print("  OK: every source tag assigned exactly once, no extras")
    # persist the FIXED proposal so the user can inspect what was actually applied
    result["clusters"] = clusters

    # ---- print proposal ranked by total bookmarks ----
    print(f"\n=== Cluster proposal ===")
    def cluster_total(c):
        return sum(count_by_id.get(s, 0) for s in c.get("source_tag_ids", []))
    for c in sorted(clusters, key=lambda c: -cluster_total(c)):
        srcs = c.get("source_tag_ids", [])
        total_b = cluster_total(c)
        sample_src = [name_by_id.get(s, "?") for s in srcs[:10]]
        more = f" +{len(srcs)-10} more" if len(srcs) > 10 else ""
        print(f"  [bookmarks={total_b:>4}  src_tags={len(srcs):>3}]  {c.get('target_name','?')}")
        print(f"        e.g. {', '.join(sample_src)}{more}")

    proposal_path = f"/tmp/karakeep-cluster-proposal-{int(time.time())}.json"
    with open(proposal_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n  proposal written: {proposal_path}")

    if not args.apply:
        print(f"\n  DRY-RUN. To apply: re-run with --apply --proposal {proposal_path}")
        return

    if missing or extra or dups:
        sys.exit("Refusing to apply: validation problems above. Edit proposal JSON or re-run.")

    # ---- apply ----
    print(f"\n=== Apply merges ===")
    merge_count = 0
    create_count = 0
    by_lower = {t["name"].lower(): t for t in tags}
    for c in clusters:
        target_name = c.get("target_name", "").strip()
        srcs = c.get("source_tag_ids", [])
        if not target_name or not srcs:
            continue
        existing = by_lower.get(target_name.lower())
        if existing:
            target_id = existing["id"]
            from_ids = [s for s in srcs if s != target_id]
        else:
            res = trpc_mutate("tags.create", {"name": target_name})
            target_id = res["id"]
            create_count += 1
            from_ids = srcs
        if from_ids:
            trpc_mutate("tags.merge", {"intoTagId": target_id, "fromTagIds": from_ids})
            merge_count += 1

    print(f"  created {create_count} new target tags")
    print(f"  ran {merge_count} merge operations")

    res = trpc_mutate("tags.deleteUnused", {})
    print(f"  deleteUnused removed {res.get('deletedTags','?')} tags")

    tags_after = fetch_tag_data()
    by_count_after = Counter(t["count"] for t in tags_after)
    print(f"\n=== Final ===")
    print(f"  total_tags: {len(tags)} -> {len(tags_after)}")
    print(f"  histogram: {dict(sorted(by_count_after.items())[:12])}")


if __name__ == "__main__":
    main()
