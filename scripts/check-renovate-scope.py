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

So this runs its own `kustomize build <cluster>` - an identical render to the
one `check-script-lint` produces, not a shared one - and evaluates ONE CONTAINER
AT A TIME.

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
excluded by `ignorePaths`.

That lookup is confined to the CLUSTER BEING ANALYSED, and confining it is
load-bearing rather than tidiness. The two trees name many of the same images -
`restic/restic:0.17.3` and the keel digest appear under both - so a repo-wide
lookup lets a homelab file in scope vouch for a VPS container that nothing
watches. Simulated with scope widened to `homelab/**` alone, a repo-wide lookup
dropped the VPS render from nine findings to six: `restic-backup`, `restic-init`
and `keel` all went quiet while `vps/backup/*.yaml` and
`vps/bootstrap/keel/keel.yaml` were still genuinely unwatched. That is this
guard committing the exact failure it exists to catch.

The lookup also compares EXTRACTED IMAGE VALUES, never raw file text. A
substring search over the whole file matches prose - `restic/restic:0.17.3`
appears in three comment sentences in `homelab/backup/restic-cronjob.yaml` - and
has no right boundary, so `alpine:3.2` would be "owned" by any file naming
`alpine:3.20`.

An image that appears in NO file of its own cluster came from a remote base
(cert-manager, the CSI drivers, local-path-provisioner): every verdict about it
is reported as advisory and does not fail the check, exactly as
`check-script-lint` treats upstream findings. Failing an apply on somebody
else's manifest makes a gate people route around.

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
  * A manager block that is not in `enabledManagers` does nothing at all.
    `enabledManagers` is a WHITELIST: naming any manager there disables every
    manager not named. So a perfectly-written `kustomize.managerFilePatterns`
    added without also adding `kustomize` to that list is inert, and dropping
    `kubernetes` from it makes every scope verdict below vacuous. Both are
    checked, and both are exit 2 - a config this check cannot trust is a check
    that could not run, not a check that passed.

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
        #
        # KNOWN AND DELIBERATELY ACCEPTED, so the next reader does not rediscover
        # it: the same arm swallows the inverse shape, where the FLOATING image
        # is an init container or sidecar and the PINNED one is the app keel was
        # annotated for. That workload is frozen, and this returns "pinned". The
        # distinction needs a container role this reader does not model, and
        # neither render contains such a workload today (checked). Revisit if
        # one appears.
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
    """Renovate's `ignorePaths` are minimatch globs against the repo-relative path.

    fnmatch alone is enough here because it treats `**` as a `*` that crosses
    separators, so `**/secrets/**` already matches `homelab/secrets/x.yaml`.
    An earlier version bolted a "strip the stars and look for the directory"
    fallback onto this, which was dead on every real glob and OVER-matched on a
    rooted one: `secrets/**` reduced to the core `secrets` and would have
    silently exempted `homelab/secrets/x.yaml`, which Renovate does not ignore.
    Over-matching here hides a file from the check, so the loose form is gone.
    """
    for glob in ignore_paths or ():
        if fnmatch.fnmatch(rel, glob):
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

    # `enabledManagers` is a WHITELIST: present, it disables every manager not
    # named in it. A manager block whose manager is not enabled is inert
    # configuration that reads like coverage, and this check would certify it.
    enabled = config.get("enabledManagers")
    if enabled is not None:
        if not isinstance(enabled, list):
            raise CheckUnrunnable("renovate.json's enabledManagers is not a list")
        if "kubernetes" not in enabled:
            raise CheckUnrunnable(
                "renovate.json's enabledManagers is %r, which does not include "
                "`kubernetes`. enabledManagers is a whitelist, so the kubernetes "
                "manager is OFF and no `image:` field is scanned at all - every "
                "scope verdict this check could make would be vacuous"
                % (enabled,))
        if kustomize_compiled and "kustomize" not in enabled:
            raise CheckUnrunnable(
                "renovate.json defines kustomize.managerFilePatterns but "
                "enabledManagers is %r, which does not include `kustomize`. "
                "Those patterns are inert: add `kustomize` to enabledManagers, "
                "or drop the block rather than leaving configuration that reads "
                "like coverage it does not provide" % (enabled,))

    ignore = config.get("ignorePaths")
    if ignore is not None and not isinstance(ignore, list):
        raise CheckUnrunnable("renovate.json's ignorePaths is not a list")
    return compiled, kustomize_compiled, list(ignore or ())


def repo_yaml_files(roots=CLUSTERS):
    """Every repo-relative YAML path under the given cluster trees."""
    found = []
    for root in roots:
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
# no flow mappings at the levels read here. Five things are extracted - kind,
# namespace, the workload's annotations, and each container's name and image.
#
# WHAT "REPORTED, NEVER SKIPPED" MEANS HERE, PRECISELY, AND WHERE IT STOPS. Two
# reader failures are printed as advisories rather than dropped: a non-empty
# document whose `kind` cannot be read, and a pod-parent document this reader
# finds no `image:` in AT ALL. Silence on either would be the check claiming
# coverage it does not have.
#
# The bound worth stating: the second advisory fires only on a pod parent that
# yields NO container. A pod parent where SOME `image:` values are read and
# others are not - a container whose image is a block scalar or a flow
# collection, which `_value` returns None for - is judged on the containers it
# did read and says nothing about the ones it missed. That is a silently
# PARTIAL verdict, not a silent skip, and this reader cannot tell the two
# apart without a real YAML parser. Neither render contains such a workload
# today. If one appears, the missed container is unjudged and unreported.
#
# What it does NOT see, and cannot: a manifest embedded inside another
# resource. local-path-provisioner ships its helper Pod - untagged
# `image: busybox`, keel-free - as a block scalar inside a ConfigMap, which is a
# ConfigMap to this reader and to `kubectl apply` alike. It comes from a remote
# base, so every verdict about it would be advisory anyway.

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


# One `key: value` line, capturing its indent prefix (a leading `- ` included,
# because a list item's first key sits at the same column as its siblings).
KEY_LINE = re.compile(r"^((?:\s*)(?:-\s+)?)([A-Za-z0-9_.\-/]+):\s*(.*?)\s*$")


def _value(raw):
    """The first bare token of a scalar value, quotes stripped."""
    raw = raw.strip()
    if not raw or raw[0] in "|>&*{[":      # a block scalar or a flow collection
        return None
    token = raw.split()[0].strip("\"'")
    return token or None


def _sibling_name(lines, index, col):
    """The `name:` belonging to the same list item as the `image:` at `index`.

    kustomize sorts mapping keys, so `image` precedes `name` in its output and
    the forward scan is the one that fires. The backward scan is for
    hand-ordered YAML, where `- name:` opens the item. Either way the scan stops
    at the item boundary, so an `env:` entry's `name:` - which is deeper - and
    the next container's - which opens a new item - are both out of reach.
    """
    for step in (1, -1):
        j = index + step
        while 0 <= j < len(lines):
            line = lines[j]
            if not line.strip():
                j += step
                continue
            match = KEY_LINE.match(line)
            if match is None:
                # A block-scalar body or a bare list element: only a dedent ends
                # the item.
                if len(line) - len(line.lstrip()) < col:
                    break
                j += step
                continue
            here = len(match.group(1))
            if here < col:
                break
            if here == col:
                opens_item = match.group(1).strip().startswith("-")
                if match.group(2) == "name":
                    return _value(match.group(3)) or "<unnamed>"
                if opens_item:
                    break                  # a different container
            j += step
    return "<unnamed>"


def containers_of(doc):
    """Every (container name, image) pair in the document, in order."""
    lines = doc.splitlines()
    found = []
    for index, line in enumerate(lines):
        match = KEY_LINE.match(line)
        if match is None or match.group(2) != "image":
            continue
        image = _value(match.group(3))
        if image:
            found.append((_sibling_name(lines, index, len(match.group(1))), image))
    return found


def has_content(doc):
    """True if the document holds anything but blank and comment lines."""
    return any(line.strip() and not line.lstrip().startswith("#")
               for line in doc.splitlines())


def source_index(roots=CLUSTERS):
    """(repo-relative path, owning cluster, frozenset of image values) per file.

    The image set comes from the same reader the render goes through, so a
    prose mention of an image in a comment confers no ownership and a tag is
    compared whole rather than as a substring.
    """
    index = []
    for rel, path in repo_yaml_files(roots):
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        index.append((rel, rel.split("/", 1)[0],
                      frozenset(image for _name, image in containers_of(text))))
    return index


def dead_patterns(patterns, source_files, ignore_paths):
    """Pattern texts that match no scanned, non-ignored file. A pattern matching
    nothing fails silently, which is the whole failure class this guards."""
    return [text for text, regex in patterns
            if not any(regex.search(rel) and not path_ignored(rel, ignore_paths)
                       for rel, _cluster, _images in source_files)]


def analyse(cluster, patterns, ignore_paths, source_files):
    """Return (failures, advisories) for one cluster, rendering it first."""
    return analyse_render(cluster, render(cluster), patterns, ignore_paths,
                          source_files)


def analyse_render(cluster, text, patterns, ignore_paths, source_files):
    """The whole verdict for one cluster, over an already-rendered stream.

    Split out from `analyse` so the test suite can drive it over a fixture
    without a `kustomize` on PATH and without a five-minute render.
    """
    failures, advisories = [], []

    for doc in documents(text):
        kind = scalar(doc, "kind", 0)
        if kind is None:
            if has_content(doc):
                advisories.append(
                    "a document whose `kind` this reader could not parse, so no "
                    "container in it was judged. Advisory.")
            continue
        if kind not in POD_PARENTS:
            continue
        name = scalar(doc, "name", 2) or "<unnamed>"
        namespace = scalar(doc, "namespace", 2) or "default"
        annotations = workload_annotations(doc)
        containers = containers_of(doc)
        if not containers:
            advisories.append(
                "%s %s/%s: a pod parent this reader found no `image:` in, so "
                "nothing in it was judged. Advisory." % (kind, namespace, name))
            continue
        # Workload-level, computed once: does keel have ANYTHING here it could
        # track? If it does, a pinned container in this workload is a sidecar
        # beside a keel-tracked image, not a frozen pin.
        floats = any(not is_pinned(reference) for _name, reference in containers)
        for container, reference in containers:
            where = "%s %s/%s (%s) [%s]" % (kind, namespace, name, container,
                                            reference)
            mode, why = classify_container(reference, annotations, floats)

            # Whether this container is OURS, and therefore whether any verdict
            # about it can fail an apply. Computed BEFORE every branch, the
            # frozen and incomplete-keel ones included: "remote-base images are
            # advisory" has to hold for all five modes, or a remote base that
            # ever shipped keel annotations on a pinned tag would hard-fail an
            # apply over a manifest this repo cannot edit. Confined to THIS
            # cluster's files: the two trees name many of the same images, so a
            # repo-wide lookup lets a watched homelab file vouch for an
            # unwatched VPS container.
            owners = [rel for rel, owner_cluster, images in source_files
                      if owner_cluster == cluster and reference in images]

            if mode in (MODE_FROZEN, MODE_INCOMPLETE_KEEL):
                if owners:
                    failures.append("%s: %s" % (where, why))
                else:
                    advisories.append(
                        "%s: %s - but it is named by no file in this cluster's "
                        "tree, so it comes from a remote base and can only be "
                        "changed by forking. Advisory." % (where, why))
                continue

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
                    "%s: pinned, keel-free, and named by no file in this "
                    "cluster's tree - it comes from a remote base, so it can "
                    "only be changed by forking. Advisory." % where)
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
        source_files = source_index()

        # Rule 1: a pattern matching nothing fails silently. BOTH manager
        # blocks, because a typo in either is the same failure.
        dead = dead_patterns(patterns + kustomize_patterns, source_files,
                             ignore_paths)
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
