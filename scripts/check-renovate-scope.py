#!/usr/bin/env python3
"""Assert every version-pinned, keel-free namespace is watched by Renovate.

WHY THIS EXISTS
---------------
Renovate is the ONLY thing that proposes image bumps for the namespaces that
forbid keel, and the `homelab-update-watch` check is the only thing that says a
bump is waiting. Both rest on one line of configuration:

    "kubernetes": { "managerFilePatterns": [ ... ] }

and both fail SILENTLY when it is wrong. A typo in a pattern matches nothing, and
a new pinned namespace nobody added to the list is simply never scanned. In both
cases Renovate opens no pull request, the watcher counts zero, the check stays
green, and the estate quietly stops receiving updates — the exact failure the
watcher was built to prevent, wearing the watcher's own colours.

Two more traps this closes:

  * Scope is enforced by `kubernetes.managerFilePatterns`, NOT by
    `matchFileNames`. A packageRule only decorates the pull requests Renovate
    already decided to open, so widening only the packageRule does nothing at all.
  * `managerFilePatterns` entries are `/regex/` strings. Nothing validates them,
    so `/^homelab/helth/.+\\.yaml$/` is accepted and matches no file forever.

THE RULE
--------
1. Every `managerFilePatterns` entry must be a well-formed `/regex/` that matches
   at least one file that exists today.
2. Every YAML file under `homelab/` that pins an image (an explicit non-floating
   tag, or a digest) and carries NO keel annotations must be matched by some
   pattern, or sit under an entry on EXEMPT below.
3. Every EXEMPT entry must still exist. An exemption for a deleted directory is a
   licence nobody is using and a rule nobody is reading.

Floating tags (`:latest`, or no tag at all) are out of scope by rule, not by
exemption: keel manages those, and pointing Renovate at them would make red the
steady state and destroy the signal.

Usage:
  scripts/check-renovate-scope.py

Exit status:
  0  scope is complete
  1  a pattern matches nothing, or a pinned keel-free file is unwatched
  2  the check itself could not run (renovate.json unreadable or malformed)
"""
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RENOVATE_JSON = os.path.join(REPO_ROOT, "renovate.json")

# The tree whose pins this guard governs. The VPS cluster is entirely
# keel-managed and has no pinned, keel-free workload; add it here the day that
# stops being true.
SCAN_ROOT = "homelab"

# Directories whose pinned images are DELIBERATELY unwatched. Removing an entry
# here is how you opt a directory in — a deliberate act, not an oversight.
#
#   homelab/backup   — restic and the ssh-client image. No dump-first story and
#                      no upgrade runbook, so a pull request proposing them would
#                      be an alert with no defined action.
#   homelab/bootstrap — platform components (traefik, keel, cert-manager, the
#                      CSI drivers). Same reasoning; most are keel-managed
#                      anyway, so this exemption is mostly inert today.
#
# Both are recorded as a named deferral in the update-alerting design (A6).
# Revisit when either grows a runbook.
EXEMPT = (
    "homelab/backup",
    "homelab/bootstrap",
)

# `image: repo/name:tag`, `- image: "repo/name@sha256:..."`. Deliberately loose:
# a line this misses is a line this guard does not protect, so it errs towards
# matching.
IMAGE = re.compile(r"^\s*-?\s*image:\s*[\"']?([^\"'\s]+)")

# A tag that moves on its own. `latest` is the only one in this estate; a bare
# `repo/name` with no tag means the same thing.
FLOATING_TAGS = frozenset({"latest", "main", "master", "edge", "stable"})

KEEL_ANNOTATION = "keel.sh/policy"


class CheckUnrunnable(Exception):
    """The check could not run at all. Given its own exit code (2)."""


def yaml_files(root):
    base = os.path.join(REPO_ROOT, root)
    if not os.path.isdir(base):
        raise CheckUnrunnable("not a directory: %s" % base)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
        for filename in sorted(filenames):
            if filename.endswith((".yaml", ".yml")):
                path = os.path.join(dirpath, filename)
                yield os.path.relpath(path, REPO_ROOT), path


def load_patterns():
    """The compiled `kubernetes.managerFilePatterns` regexes, with their text."""
    try:
        with open(RENOVATE_JSON, encoding="utf-8") as handle:
            config = json.load(handle)
    except OSError as exc:
        raise CheckUnrunnable("cannot read %s: %s" % (RENOVATE_JSON, exc))
    except ValueError as exc:
        raise CheckUnrunnable("%s is not valid JSON: %s" % (RENOVATE_JSON, exc))

    kubernetes = config.get("kubernetes")
    if not isinstance(kubernetes, dict):
        raise CheckUnrunnable(
            "renovate.json has no `kubernetes` object — scope is enforced there, "
            "not by matchFileNames, so this check cannot be trusted without it")
    raw = kubernetes.get("managerFilePatterns")
    if not isinstance(raw, list) or not raw:
        raise CheckUnrunnable(
            "renovate.json's kubernetes.managerFilePatterns is missing or empty")

    compiled = []
    for entry in raw:
        if not isinstance(entry, str) or len(entry) < 3 or not (
                entry.startswith("/") and entry.endswith("/")):
            raise CheckUnrunnable(
                "managerFilePatterns entry %r is not a /regex/ string" % entry)
        body = entry[1:-1]
        try:
            compiled.append((entry, re.compile(body)))
        except re.error as exc:
            raise CheckUnrunnable(
                "managerFilePatterns entry %r is not a valid regex: %s"
                % (entry, exc))
    return compiled


def is_pinned(reference):
    """True if this image reference names one immutable-ish version."""
    if "${" in reference:                 # an envsubst placeholder, not a pin
        return False
    if "@sha256:" in reference:
        return True
    # Strip a registry host:port before looking for the tag separator.
    last = reference.rsplit("/", 1)[-1]
    if ":" not in last:
        return False
    return last.rsplit(":", 1)[1] not in FLOATING_TAGS


def pinned_keel_free(path):
    """True if this file pins at least one image and carries no keel policy."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        raise CheckUnrunnable("cannot read %s: %s" % (path, exc))
    if KEEL_ANNOTATION in text:
        return False
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = IMAGE.match(line)
        if match and is_pinned(match.group(1)):
            return True
    return False


def exempt(rel):
    return any(rel == e or rel.startswith(e.rstrip("/") + "/") for e in EXEMPT)


def main():
    try:
        patterns = load_patterns()
        for entry in EXEMPT:
            if not os.path.exists(os.path.join(REPO_ROOT, entry)):
                raise CheckUnrunnable(
                    "EXEMPT names %s, which does not exist — remove the entry "
                    "in the same commit that removed the directory, or this "
                    "list stops describing the estate" % entry)

        files = list(yaml_files(SCAN_ROOT))
        matched = {text: 0 for text, _ in patterns}
        watched = set()
        for rel, _path in files:
            for text, regex in patterns:
                if regex.search(rel):
                    matched[text] += 1
                    watched.add(rel)

        dead = [text for text, count in matched.items() if count == 0]
        unwatched = []
        for rel, path in files:
            if rel in watched or exempt(rel):
                continue
            if pinned_keel_free(path):
                unwatched.append(rel)
    except CheckUnrunnable as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2

    if dead:
        print("Renovate managerFilePatterns that match no file:\n")
        for text in sorted(dead):
            print("  %s" % text)
        print("\nA pattern that matches nothing fails silently: Renovate scans "
              "no manifest,\nopens no pull request, and homelab-update-watch "
              "stays green over an estate\nthat has stopped receiving updates. "
              "Fix the pattern, or delete it in the same\ncommit that removed "
              "the tree it pointed at.")
        return 1

    if unwatched:
        print("Pinned, keel-free manifests that Renovate does not watch:\n")
        for rel in sorted(unwatched):
            print("  %s" % rel)
        print("\nNothing else proposes bumps for these: keel is absent and the "
              "images are\npinned, so they will sit at the version they were "
              "written at, indefinitely and\nsilently. Add the directory to "
              "renovate.json's kubernetes.managerFilePatterns\n(a packageRule "
              "alone does NOT widen scope), or add it to EXEMPT in\n"
              "scripts/check-renovate-scope.py with a written reason.")
        return 1

    print("OK: %d pattern(s) match %d watched file(s) under %s/; no pinned, "
          "keel-free manifest is unwatched"
          % (len(patterns), len(watched), SCAN_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
