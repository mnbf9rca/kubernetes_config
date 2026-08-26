#!/usr/bin/env python3
"""Assert every container is in exactly one update mode, and the right one.

WHY THIS EXISTS
---------------
Two mechanisms update this estate and each is silent when it stops. keel updates
floating tags on a timer. Renovate proposes bumps for pinned tags. The rule is:

    FLOATING TAG MEANS KEEL. PINNED TAG MEANS RENOVATE. NEVER BOTH.

Every way of getting that wrong fails quietly:

  * A pinned tag carrying keel annotations is FROZEN while looking covered.
    `keel.sh/match-tag: "true"` on a pinned tag only refreshes the digest, so
    `traefik:v3.3` and `meilisearch:v1.41.0` sat unbumped for months with four
    annotations apparently watching them.
  * An INCOMPLETE keel annotation set is worse than none. Without
    `keel.sh/match-tag`, keel silently downgrades a semver tag to `:latest` -
    an unrequested major upgrade delivered by a controller nobody asked.
  * A pinned tag with no keel annotations and OUTSIDE Renovate's scope receives
    nothing at all, forever. Renovate opens no pull request, `update-watch`
    counts zero, and the check stays green over an estate that has stopped
    receiving updates - the watcher's own failure mode, wearing its colours.
  * A FLOATING tag inside `health`, `hindsight`, `ops` or `backup` is an
    unattended update to a stateful store, a scheduled job or a backup runner.
    Those namespaces forbid keel precisely so that cannot happen.

WHY THE RENDER AND NOT THE SOURCE TREE
--------------------------------------
The previous version of this check asked whether a FILE mentioned
`keel.sh/policy` anywhere and whether a FILE pinned any image. A file holding one
keel-managed Deployment and one pinned CronJob passed on the Deployment's
annotations, and the CronJob was never examined. Namespaces are also a render
property, not a path property: `kustomization.yaml` can set one, and a workload's
directory need not match it.

So this runs `kustomize build <cluster>` - the same render `check-script-lint`
already produces - and evaluates ONE CONTAINER AT A TIME.

ONE CONTAINER AT A TIME, BUT KEEL ANNOTATIONS ARE A WORKLOAD PROPERTY
---------------------------------------------------------------------
keel reads the workload's annotations and applies them to the images it can
track, which are the FLOATING ones. A Deployment whose app image floats and
whose quiesce sidecar is `alpine:3.20` is CORRECT and intended: keel bumps the
app, Renovate bumps the sidecar. Smearing the workload's annotations across
every container reads four such sidecars as "frozen" - and there are four of
them on the VPS cluster.

So the frozen verdict needs the whole workload: keel annotations present AND
nothing in the workload floating, meaning there is nothing for keel to track and
the annotations can only be about a pin. That is exactly the traefik and
meilisearch shape. A PINNED container inside a workload that DOES float is
Renovate's territory, like any other pin.

Scope is still a FILE question, because `managerFilePatterns` matches paths. So
for each pinned, keel-free image the check locates the source file(s) naming that
image and requires at least one of them to be matched by a pattern and not
excluded by `ignorePaths`. An image that appears in NO repo file came from a
remote base (cert-manager, the CSI drivers, local-path-provisioner): those are
reported as advisory and do not fail the check, exactly as `check-script-lint`
treats upstream findings. Failing an apply on somebody else's manifest makes a
gate people route around.

TWO TRAPS THIS STILL CLOSES
---------------------------
  * Scope is enforced by `kubernetes.managerFilePatterns`, NOT by
    `matchFileNames`. A packageRule only decorates the pull requests Renovate
    already decided to open, so widening only a packageRule does nothing.
  * `managerFilePatterns` entries are `/regex/` strings and nothing validates
    them, so `/^homelab/helth/.+\\.yaml$/` is accepted and matches no file
    forever. BOTH manager blocks are checked for that, `kubernetes` and
    `kustomize`: a typo in either is the same silent-scope failure. Only the
    `kubernetes` patterns confer scope on a container image, because that is
    the manager which reads `image:` fields; the `kustomize` manager reads
    `kustomization.yaml` image transformers and remote bases.

Usage:
  scripts/check-renovate-scope.py [homelab|vps]     default: both

Exit status:
  0  every container is in a legal mode and every pinned one is in scope
  1  at least one is not
  2  the check itself could not run (renovate.json unreadable, kustomize failed)
"""
import fnmatch
import json
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RENOVATE_JSON = os.path.join(REPO_ROOT, "renovate.json")

CLUSTERS = ("homelab", "vps")

# The complete set. Any subset that is not the empty set is a failure: a missing
# `match-tag` is the one that silently downgrades a semver tag to `:latest`.
KEEL_ANNOTATIONS = frozenset({
    "keel.sh/policy",
    "keel.sh/match-tag",
    "keel.sh/trigger",
    "keel.sh/pollSchedule",
})

# Namespaces where an unattended update is the failure to design out: stateful
# stores that run forward-only migrations, scheduled jobs whose output nothing
# re-verifies, and the backup runners themselves. These are the namespaces that
# forbid keel in AGENTS.md, expressed as the render sees them.
NO_FLOAT_NAMESPACES = frozenset({"health", "hindsight", "ops", "backup"})

# The written exemptions to the floating ban. An entry is a deliberate act and
# must carry a namespace, an image and a reason; the test suite asserts all
# three are non-empty. Consulted on BOTH floating arms below, not just inside
# the NO_FLOAT_NAMESPACES branch - keyed to a namespace the workload does not
# live in, or reachable from only one arm, an exemption is dead code that looks
# like policy.
FLOATING_EXEMPT = (
    {
        "namespace": "jottacloud-backup",
        "image": "ghcr.io/mnbf9rca/jottacloud-backup",
        "reason": (
            "A CronJob, not a long-running Deployment. Every scheduled run "
            "starts a fresh pod that pulls :latest, so the schedule ALREADY "
            "delivers the auto-pull behaviour keel would provide - which is "
            "why this workload carries no keel annotations at all and does not "
            "need any. It is also the operator's own image, built by their own "
            "CI, so pulling its :latest is a deployment rather than an "
            "unattended third-party update."),
    },
    {
        "namespace": "backup",
        "image": "ghcr.io/mnbf9rca/jottacloud-backup",
        "reason": (
            "The same workload, listed a second time against the namespace it "
            "would land in if it is ever folded in with restic, so that move "
            "cannot turn this guard red for a reason nobody wrote down."),
    },
)

# Tags that move on their own. THREE shapes, because this estate has all three:
#
#   * the well-known channel names below;
#   * a `-latest` suffix - `ghcr.io/umami-software/umami:postgresql-latest` is
#     umami's per-database build and keel tracks it, correctly;
#   * a bare MAJOR version stream - `louislam/uptime-kuma:2` moves on every 2.x
#     release, and `v2` is the same thing spelled differently.
#
# A DOTTED tag is not a stream by this rule. `alpine:3.20`, `traefik:v3.3`,
# `postgres:16-alpine` and `pgvector/pgvector:0.8.1-pg17` are pins that Renovate
# bumps, and calling any of them floating would hand a reviewed bump to keel.
FLOATING_TAGS = frozenset({"latest", "main", "master", "edge", "stable", "release"})
FLOATING_STREAM = re.compile(r"^v?\d+$")


def is_floating_tag(tag):
    """True if this TAG (not reference) moves on its own."""
    if tag in FLOATING_TAGS:
        return True
    if tag.endswith("-latest") or tag.startswith("latest-"):
        return True
    return bool(FLOATING_STREAM.match(tag))

# The four modes a container can be in.
MODE_KEEL = "keel"                          # floating tag, complete keel set - legal
MODE_PINNED = "pinned"                      # pinned tag, no keel - must be in scope
MODE_FROZEN = "frozen"                      # pinned tag WITH keel - always a failure
MODE_INCOMPLETE_KEEL = "incomplete-keel"    # a partial keel set - always a failure
MODE_FLOATING_UNMANAGED = "floating-unmanaged"  # floating tag, no keel - nothing updates it

# Workload kinds that carry containers this check cares about. A Pod is included
# because a bare Pod would otherwise be invisible.
POD_PARENTS = frozenset({
    "Deployment", "DaemonSet", "StatefulSet", "ReplicaSet",
    "Job", "CronJob", "Pod",
})


class CheckUnrunnable(Exception):
    """The check could not run at all. Given its own exit code (2)."""


def is_pinned(reference):
    """True if this image reference names one immutable-ish version."""
    if not reference or "${" in reference:   # an envsubst placeholder, not a pin
        return False
    if "@sha256:" in reference:
        return True
    last = reference.rsplit("/", 1)[-1]      # strip a registry host:port
    if ":" not in last:
        return False
    return not is_floating_tag(last.rsplit(":", 1)[1])


def image_repo(reference):
    """The repository part, with any tag and digest removed."""
    head = reference.split("@", 1)[0]
    prefix, _, last = head.rpartition("/")
    if ":" in last:
        last = last.rsplit(":", 1)[0]
    return (prefix + "/" + last) if prefix else last


def classify_container(reference, annotations, workload_floats=None):
    """Return (mode, why) for ONE container. `why` is '' when the mode is legal.

    `annotations` are the WORKLOAD's, because that is where keel reads them.
    `workload_floats` is True when SOME container in the same workload carries
    a floating tag - which is what tells a keel-tracked app image apart from a
    pinned sidecar riding along beside it. It defaults to judging this
    container alone, which is what a single-container workload means.
    """
    present = KEEL_ANNOTATIONS & set(annotations or {})
    pinned = is_pinned(reference)
    if workload_floats is None:
        workload_floats = not pinned

    if present and present != KEEL_ANNOTATIONS:
        missing = ", ".join(sorted(KEEL_ANNOTATIONS - present))
        return MODE_INCOMPLETE_KEEL, (
            "an incomplete keel annotation set (missing: %s). Without "
            "keel.sh/match-tag keel silently downgrades a semver tag to :latest"
            % missing)
    if present and pinned and not workload_floats:
        # Nothing in this workload floats, so the annotations can only be about
        # a pin. That is the frozen state.
        return MODE_FROZEN, (
            "a pinned tag carrying keel annotations, with nothing floating in "
            "the same workload for keel to track. keel.sh/match-tag on a "
            "pinned tag only refreshes the digest, so this is frozen while "
            "looking covered. Pick one mode: drop the annotations and let "
            "Renovate propose the bump, or float the tag")
    if present and pinned:
        # A pinned SIDECAR beside a floating app image: correct and intended.
        # keel bumps the app, Renovate bumps this. Renovate's territory.
        return MODE_PINNED, ""
    if present:
        return MODE_KEEL, ""
    if pinned:
        return MODE_PINNED, ""
    return MODE_FLOATING_UNMANAGED, (
        "a floating tag with no keel annotations, so nothing updates it and "
        "nothing pins it either")


def floating_exempt(namespace, reference):
    """True if this floating image is a written exemption in this namespace."""
    repo = image_repo(reference)
    for entry in FLOATING_EXEMPT:
        if entry["namespace"] == namespace and entry["image"] == repo:
            return True
    return False


def path_ignored(rel, ignore_paths):
    """Renovate's `ignorePaths` are minimatch globs against the repo-relative path."""
    for glob in ignore_paths or ():
        if fnmatch.fnmatch(rel, glob):
            return True
        # `**/secrets/**` must also match `homelab/secrets/x.yaml`; fnmatch
        # treats `**` as a plain `*` across separators, which over-matches
        # rather than under-matches. Over-matching here would silently exempt a
        # file, so the directory form is checked explicitly instead.
        core = glob.strip("*").strip("/")
        if core and ("/" + core + "/") in ("/" + rel):
            return True
    return False


def _compile_patterns(raw, where):
    """Compile one `managerFilePatterns` list of `/regex/` strings."""
    if not isinstance(raw, list) or not raw:
        raise CheckUnrunnable("renovate.json's %s is missing or empty" % where)
    compiled = []
    for entry in raw:
        if not isinstance(entry, str) or len(entry) < 3 or not (
                entry.startswith("/") and entry.endswith("/")):
            raise CheckUnrunnable(
                "%s entry %r is not a /regex/ string" % (where, entry))
        try:
            compiled.append((entry, re.compile(entry[1:-1])))
        except re.error as exc:
            raise CheckUnrunnable(
                "%s entry %r is not a valid regex: %s" % (where, entry, exc))
    return compiled


def load_renovate():
    """Return (kubernetes_patterns, kustomize_patterns, ignore_paths).

    Both manager blocks are compiled, because a typo in either is the same
    silent-scope failure. Only the `kubernetes` patterns confer scope on a
    container image; `kustomize` is optional and may be absent.
    """
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
            "renovate.json has no `kubernetes` object - scope is enforced there, "
            "not by matchFileNames, so this check cannot be trusted without it")
    compiled = _compile_patterns(kubernetes.get("managerFilePatterns"),
                                 "kubernetes.managerFilePatterns")

    # Optional, and validated only if present: the config has no `kustomize`
    # block until the scope widening lands.
    kustomize = config.get("kustomize")
    kustomize_compiled = []
    if isinstance(kustomize, dict) and "managerFilePatterns" in kustomize:
        kustomize_compiled = _compile_patterns(
            kustomize["managerFilePatterns"], "kustomize.managerFilePatterns")

    ignore = config.get("ignorePaths")
    if ignore is not None and not isinstance(ignore, list):
        raise CheckUnrunnable("renovate.json's ignorePaths is not a list")
    return compiled, kustomize_compiled, list(ignore or ())


def repo_yaml_files():
    """Every repo-relative YAML path under the two cluster trees."""
    found = []
    for root in CLUSTERS:
        base = os.path.join(REPO_ROOT, root)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
            for filename in sorted(filenames):
                if filename.endswith((".yaml", ".yml")):
                    path = os.path.join(dirpath, filename)
                    found.append((os.path.relpath(path, REPO_ROOT), path))
    return found


def render(cluster):
    """`kustomize build <cluster>` as text. Never carries a secret: envsubst has
    not run, so every placeholder is still `${VAR}`."""
    try:
        out = subprocess.run(["kustomize", "build", cluster], check=False,
                             capture_output=True, text=True, cwd=REPO_ROOT,
                             timeout=300)
    except FileNotFoundError:
        raise CheckUnrunnable("kustomize is not on PATH (`make check-tools`)")
    except subprocess.TimeoutExpired:
        raise CheckUnrunnable("`kustomize build %s` timed out" % cluster)
    if out.returncode != 0:
        raise CheckUnrunnable(
            "`kustomize build %s` failed (exit %d):\n%s"
            % (cluster, out.returncode, out.stderr.strip()[:2000]))
    return out.stdout


# --- a deliberately small YAML reader ---------------------------------------
# The estate has no PyYAML and adding a dependency for a preflight guard would
# make the guard the reason an apply cannot run on a fresh machine. The render
# is kustomize's own normalised output: two-space indent, no tabs, no anchors,
# no flow mappings at the levels read here. Only four things are extracted -
# kind, namespace, the workload's annotations, and each container's image - and
# anything unparseable is reported, never skipped.

DOC_SEP = re.compile(r"^---\s*$")


def documents(text):
    doc, docs = [], []
    for line in text.splitlines():
        if DOC_SEP.match(line):
            if doc:
                docs.append("\n".join(doc))
            doc = []
        else:
            doc.append(line)
    if doc:
        docs.append("\n".join(doc))
    return docs


def scalar(text, key, indent):
    """The value of `<indent spaces>key: value` at exactly this indent."""
    pattern = re.compile(r"^%s%s:\s*(.*?)\s*$" % (" " * indent, re.escape(key)),
                         re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return None
    return match.group(1).strip().strip("\"'") or None


def workload_annotations(doc):
    """The annotations under `metadata:` at the document's top level only.

    Pod-template annotations are deliberately NOT read: keel reads the
    workload's own annotations, so reading the template's would report coverage
    that does not exist.
    """
    lines = doc.splitlines()
    result = {}
    in_meta = in_annotations = False
    for line in lines:
        if re.match(r"^metadata:\s*$", line):
            in_meta, in_annotations = True, False
            continue
        if in_meta and re.match(r"^\S", line):
            break
        if in_meta and re.match(r"^  annotations:\s*$", line):
            in_annotations = True
            continue
        if in_annotations:
            if not re.match(r"^    \S", line):
                in_annotations = False
                continue
            key, _, value = line.strip().partition(":")
            result[key.strip().strip("\"'")] = value.strip().strip("\"'")
    return result


IMAGE_LINE = re.compile(r"^\s*-?\s*image:\s*[\"']?([^\"'\s]+)", re.MULTILINE)


def containers_of(doc):
    """Every `image:` value in the document, in order."""
    return [m.group(1) for m in IMAGE_LINE.finditer(doc)]


def analyse(cluster, patterns, ignore_paths, source_files):
    """Return (failures, advisories) for one cluster."""
    failures, advisories = [], []
    text = render(cluster)

    for doc in documents(text):
        kind = scalar(doc, "kind", 0)
        if kind not in POD_PARENTS:
            continue
        name = scalar(doc, "name", 2) or "<unnamed>"
        namespace = scalar(doc, "namespace", 2) or "default"
        annotations = workload_annotations(doc)
        images = containers_of(doc)
        # Workload-level, computed once: does keel have ANYTHING here it could
        # track? If it does, a pinned container in this workload is a sidecar
        # beside a keel-tracked image, not a frozen pin.
        floats = any(not is_pinned(reference) for reference in images)
        for reference in images:
            where = "%s %s/%s [%s]" % (kind, namespace, name, reference)
            mode, why = classify_container(reference, annotations, floats)

            if mode in (MODE_FROZEN, MODE_INCOMPLETE_KEEL):
                failures.append("%s: %s" % (where, why))
                continue

            # Whether this container is OURS. An image named by no file in this
            # repo came from a remote base and can only be changed by forking,
            # so every verdict below it is advisory. Computed before the
            # floating branch as well as the pinned one: cert-manager's or the
            # CSI driver's tag choices are not this repo's to enforce, and
            # failing an apply on somebody else's manifest makes a gate people
            # route around.
            owners = [rel for rel, _p, blob in source_files
                      if reference in blob]

            if mode in (MODE_KEEL, MODE_FLOATING_UNMANAGED):
                if not owners:
                    advisories.append(
                        "%s: a floating tag from a remote base. Advisory."
                        % where)
                elif floating_exempt(namespace, reference):
                    # A WRITTEN exemption, consulted BEFORE both failure arms.
                    # It has to be: jottacloud-backup carries no keel
                    # annotations, so it lands on the unmanaged arm, and its
                    # namespace is not in NO_FLOAT_NAMESPACES - an exemption
                    # consulted only inside that branch is unreachable code
                    # that reads like policy.
                    pass
                elif namespace in NO_FLOAT_NAMESPACES:
                    failures.append(
                        "%s: a floating tag in namespace `%s`, which forbids "
                        "unattended updates. Pin it and let Renovate propose the "
                        "bump, or add a written exemption to FLOATING_EXEMPT."
                        % (where, namespace))
                elif mode == MODE_FLOATING_UNMANAGED:
                    failures.append("%s: %s" % (where, why))
                continue

            # MODE_PINNED: it must be visible to Renovate.
            if not owners:
                advisories.append(
                    "%s: pinned, keel-free, and named by no file in this repo - "
                    "it comes from a remote base, so it can only be changed by "
                    "forking. Advisory." % where)
                continue
            covered = [rel for rel in owners
                       if not path_ignored(rel, ignore_paths)
                       and any(rx.search(rel) for _t, rx in patterns)]
            if not covered:
                failures.append(
                    "%s: pinned and keel-free, but no file naming it is inside "
                    "Renovate's scope (%s). Nothing proposes bumps for it, so it "
                    "sits at this version indefinitely and silently. Widen "
                    "kubernetes.managerFilePatterns - a packageRule alone does "
                    "NOT widen scope." % (where, ", ".join(sorted(owners))))
    return failures, advisories


def main(argv):
    which = argv[1:] or list(CLUSTERS)
    for cluster in which:
        if cluster not in CLUSTERS:
            print("ERROR: unknown cluster %r (expected one of %s)"
                  % (cluster, ", ".join(CLUSTERS)), file=sys.stderr)
            return 2

    try:
        patterns, kustomize_patterns, ignore_paths = load_renovate()
        source_files = []
        for rel, path in repo_yaml_files():
            with open(path, encoding="utf-8", errors="replace") as handle:
                source_files.append((rel, path, handle.read()))

        # Rule 1: a pattern matching nothing fails silently. BOTH manager
        # blocks, because a typo in either is the same failure.
        dead = [text for text, rx in patterns + kustomize_patterns
                if not any(rx.search(rel) and not path_ignored(rel, ignore_paths)
                           for rel, _p, _b in source_files)]
        if dead:
            print("Renovate managerFilePatterns that match no scanned file:\n")
            for text in sorted(dead):
                print("  %s" % text)
            print("\nA pattern that matches nothing fails silently: Renovate "
                  "scans no manifest,\nopens no pull request, and "
                  "homelab-update-watch stays green over an estate\nthat has "
                  "stopped receiving updates. Fix the pattern, or delete it in "
                  "the same\ncommit that removed the tree it pointed at.")
            return 1

        failures, advisories = [], []
        for cluster in which:
            cluster_failures, cluster_advisories = analyse(
                cluster, patterns, ignore_paths, source_files)
            failures += ["[%s] %s" % (cluster, f) for f in cluster_failures]
            advisories += ["[%s] %s" % (cluster, a) for a in cluster_advisories]
    except CheckUnrunnable as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2

    for line in sorted(advisories):
        print("advisory: %s" % line)

    if failures:
        print("\nUpdate-mode violations:\n")
        for line in sorted(failures):
            print("  %s" % line)
        print("\nThe rule: FLOATING TAG MEANS KEEL, PINNED TAG MEANS RENOVATE, "
              "NEVER BOTH.\nSee AGENTS.md and "
              "docs/operations/apply-workflow.md.")
        return 1

    print("OK: [%s] every container is in exactly one update mode; %d advisory "
          "note(s) from remote bases" % (", ".join(which), len(advisories)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
