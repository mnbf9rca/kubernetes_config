#!/usr/bin/env python3
"""Assert the two `keel-fresh` copies have not diverged behaviourally.

`homelab/ops/` and `vps/ops/` hold a deliberate copy-paste pair: the same runner
script and the same CronJob manifest, twice. They are duplicated rather than
shared because kustomize will not read a generator source outside its own
kustomization root, and because the alternative -- one job reaching across to
the other cluster -- would need a VPS kubeconfig inside a homelab pod, which is
a credential crossing a cluster boundary to save one file.

The cost of that choice is an invariant nothing enforced until this guard
existed: EDIT THEM TOGETHER. A fix applied to one cluster and not the other is a
dead-man's-switch that has quietly stopped switching on the cluster nobody
looked at -- the precise failure the `keel-fresh` job was built to remove, now
reintroduced one level up. Two source comments say "edit them together", both of
them in the VPS copies; a comment has never stopped anybody.

HOW IT WORKS, AND WHY IT IS A MASK-THEN-COMPARE RATHER THAN A DIFF CLASSIFIER.
Each sanctioned difference is a rule below. Every rule rewrites its region to a
fixed token in whichever file(s) carry it, and once every rule has fired the two
texts must be BYTE-IDENTICAL. So the allowlist is closed by construction: a line
this file does not name cannot differ, and there is no "small enough to ignore"
hunk size for a real edit to hide under. A hunk-classifying diff would have the
opposite default -- unrecognised means unjudged.

NO RULE MAY SPAN A LINE IT DOES NOT NAME. Every multi-line pattern below is
built from a run of comment lines plus the specific line or two it exists to
mask, so an inserted statement ends the run rather than disappearing into it.
That is the property to preserve when adding an eleventh rule: a `.*?` across
newlines is a hole the size of whatever anyone puts there, and it will pass.

Each rule also declares how many times it must fire in each file. That matters
more than it looks: if somebody rewords a sanctioned region so a pattern stops
matching, the guard reports ITSELF as stale (exit 2) instead of silently
masking nothing and passing. A guard that cannot find what it was told to
ignore has stopped being a guard.

THE ALLOWLIST IS DELIBERATELY NARROW, and every entry is a per-cluster fact
that CANNOT be shared:

  script    the copy note itself (VPS only -- it names the other file)
            the `IMAGE_FLOOR` constant and its comment: 6 on homelab against a
              steady state of 7, 7 on the VPS against 9
            the manifest path in the push-URL note

  manifest  the copy note itself (VPS only)
            the monitor name, `homelab-keel-fresh` / `vps-keel-fresh`
            the script path in the header
            the schedule and its comment: 07:15Z and 07:45Z, half an hour apart
              so the two clusters' alerts are distinguishable in an inbox
            the `nodeSelector` block (VPS only -- one storage node there)
            the 1Password vault path and the envsubst variable name

Anything else -- a metric name, a verdict, a deadline, a probe, an ordering, a
resource limit, a security context -- is behaviour, and behaviour must match.

NOT PER-CLUSTER, WHICH MAKES IT THE ODD ONE OUT. Every other `check-*` guard
here has a `-homelab` / `-vps` pair so a fault in one tree cannot block an apply
to the other. This one compares the two trees AGAINST EACH OTHER, so "the VPS
half of a comparison between homelab and VPS" is not a thing that exists. It
therefore takes no cluster argument and runs whole on all four chains. The
consequence is worth stating plainly rather than discovering: a divergence
introduced by editing the VPS copy WILL block `apply-homelab` too. That is
correct -- while the pair is out of step, neither cluster's copy is trustworthy,
and the fix is to finish the edit, not to route around the gate.

It reads four files and shells out to no renderer, so it is cheap and sits on
BOTH halves of the diff/apply chains alongside `check-script-substitution` and
`check-ping-bodies`, not on the public half only like the three render-based
guards.

Usage:
  scripts/check-keel-fresh-parity.py

Exit status:
  0  the pair matches outside the sanctioned regions
  1  they have diverged (the offending lines are printed)
  2  the check itself could not run (a file is missing, or a rule went stale)
"""
import difflib
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)


class CheckUnrunnable(Exception):
    """The guard could not do its job. Never confused with a real divergence."""


# --- the allowlist ---------------------------------------------------------
#
# A rule is (name, pattern, replacement, homelab_count, vps_count). The counts
# are exact, not minimums: `None` means "this rule does not apply to that file
# and must not match there". Patterns are applied in order, so a later rule
# sees the text a former one already rewrote.

SCRIPT_RULES = (
    # The copy note names the OTHER cluster's file, so it can only live in one
    # of the two. It is removed rather than tokenised: homelab has no
    # counterpart region for a token to line up against.
    ("script copy note",
     re.compile(r"^# THIS IS A DELIBERATE COPY of homelab/ops/scripts/keel-fresh\.sh"
                r".*(?:\n#.*)*?\n#\n(?=# WHAT IT CATCHES)", re.M),
     "", None, 1),
    # The push-URL note points at the manifest that carries the real variable
    # name, and that manifest is per-cluster.
    ("push-URL note manifest path",
     re.compile(r"^# is in (?:homelab|vps)/ops/keel-fresh\.yaml,", re.M),
     "# is in <CLUSTER>/ops/keel-fresh.yaml,", 1, 1),
    # The floor and the whole comment deriving it. The estates differ, so the
    # arithmetic differs; what must not differ is anything below it.
    #
    # EVERY LINE OF THE SPAN MUST BE A COMMENT, and the `(?:\n#.*)*` is what
    # says so. An earlier version wrote this as `.*?` under `re.S`, which
    # swallowed whatever lay between the comment's first line and the
    # assignment -- nineteen lines on homelab, thirty-one on the VPS --
    # regardless of content. Real shell inserted into that window in one copy
    # only passed at exit 0, which is precisely the divergence this guard
    # exists to catch, sitting directly above the one constant a future editor
    # is most likely to touch. `check-script-lint` would not have caught it
    # either: the insertion is valid `sh` on both sides. Constrained here, an
    # inserted code line simply ends the comment run, the assignment no longer
    # follows, the rule matches zero times and the guard fails at exit 2.
    ("IMAGE_FLOOR block",
     re.compile(r"^# The literal floor for tracked images\..*(?:\n#.*)*"
                r"\nIMAGE_FLOOR=\d+$", re.M),
     "<IMAGE_FLOOR BLOCK>", 1, 1),
)

MANIFEST_RULES = (
    # The two block removals come first: a region that exists in only one file
    # can contain a name the inline rules below also match, and counting it
    # would make those counts differ between the copies for no real reason.
    ("manifest copy note",
     re.compile(r"^# A DELIBERATE COPY of homelab/ops/keel-fresh\.yaml\."
                r".*(?:\n#.*)*?\n#\n", re.M),
     "", None, 1),
    # VPS only: that cluster's local-path storage lives on one node, so the
    # state PVC is reachable from nowhere else. Homelab has no such constraint.
    ("nodeSelector block",
     re.compile(r"^ +# PINNED TO THE STORAGE NODE\."
                r".*(?:\n *#.*)*\n *nodeSelector:\n *kubernetes\.io/hostname: \S+\n",
                re.M),
     "", None, 1),
    ("uptime-kuma monitor name",
     re.compile(r"(?:homelab|vps)-keel-fresh"),
     "<CLUSTER>-keel-fresh", 2, 2),
    ("runner script path",
     re.compile(r"(?:homelab|vps)/ops/scripts/keel-fresh\.sh"),
     "<CLUSTER>/ops/scripts/keel-fresh.sh", 1, 1),
    # The comment is inside the region because it explains the offset, and the
    # offset is the thing that differs. `[^"\n]*` rather than `[^"]*`: a
    # negated class matches newlines, so the looser form could in principle
    # run the value on to some later line ending in a quote and mask
    # everything between. It does not today -- checked against both files --
    # and excluding the newline means it never can.
    ("schedule and its comment",
     re.compile(r"^(?:  #.*\n)+  schedule: \"[^\"\n]*\"$", re.M),
     "<SCHEDULE BLOCK>", 1, 1),
    ("1Password vault path",
     re.compile(r"op://(?:Homelab|VPS)/keel-fresh/kuma-push-token"),
     "op://<VAULT>/keel-fresh/kuma-push-token", 1, 1),
    ("envsubst variable name",
     re.compile(r"\$\{(?:VPS_)?OPS_KUMA_KEEL_TOKEN\}"),
     "${<TOKEN VAR>}", 1, 1),
)

PAIRS = (
    ("runner script",
     "homelab/ops/scripts/keel-fresh.sh", "vps/ops/scripts/keel-fresh.sh",
     SCRIPT_RULES),
    ("CronJob manifest",
     "homelab/ops/keel-fresh.yaml", "vps/ops/keel-fresh.yaml",
     MANIFEST_RULES),
)


def read(relative):
    path = os.path.join(ROOT, relative)
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise CheckUnrunnable(
            "cannot read %s (%s). Both copies must exist; if one was renamed or "
            "removed, this guard needs updating in the same commit, along with "
            "the `keel-fresh copy pair` rule in renovate.json, which names the "
            "same two paths."
            % (relative, exc.strerror))


def mask(text, rules, which, label):
    """Rewrite every sanctioned region to its token, asserting the exact count.

    `which` is "homelab" or "vps" and selects which declared count applies.
    A count mismatch is exit 2, not exit 1: it means this file's allowlist has
    gone stale against the sources, and a stale allowlist masks the wrong thing.
    """
    index = 3 if which == "homelab" else 4
    for name, pattern, replacement, *counts in rules:
        expected = counts[index - 3]
        found = len(pattern.findall(text))
        if expected is None:
            if found:
                raise CheckUnrunnable(
                    "the %s rule matched %d time(s) in the %s copy of the %s, "
                    "where it must never appear. Either the region moved to the "
                    "wrong file or this guard's allowlist is out of date."
                    % (name, found, which, label))
            continue
        if found != expected:
            raise CheckUnrunnable(
                "the %s rule matched %d time(s) in the %s copy of the %s, "
                "expected %d. A sanctioned region was reworded, moved or "
                "duplicated; update scripts/check-keel-fresh-parity.py in the "
                "same commit that changed it."
                % (name, found, which, label, expected))
        text = pattern.sub(replacement, text)
    return text


def divergence(label, homelab_relative, vps_relative, rules):
    """Return a printable divergence report, or None when the pair matches."""
    left = mask(read(homelab_relative), rules, "homelab", label)
    right = mask(read(vps_relative), rules, "vps", label)
    if left == right:
        return None
    diff = difflib.unified_diff(
        left.splitlines(True), right.splitlines(True),
        fromfile=homelab_relative + " (masked)",
        tofile=vps_relative + " (masked)", n=2)
    return "".join(diff)


def main(argv):
    if len(argv) > 1:
        print("ERROR: this guard takes no arguments. It compares the two trees "
              "against each other, so there is no per-cluster half of it — see "
              "the header.", file=sys.stderr)
        return 2

    reports = []
    try:
        for label, homelab_relative, vps_relative, rules in PAIRS:
            report = divergence(label, homelab_relative, vps_relative, rules)
            if report:
                reports.append((label, report))
    except CheckUnrunnable as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2

    if reports:
        print("The two keel-fresh copies have diverged outside the sanctioned "
              "regions.\nSanctioned differences are already masked below, so "
              "every line shown is a\nREAL behavioural difference between the "
              "two clusters' dead-man's-switches.\n")
        for label, report in reports:
            print("--- %s ---" % label)
            print(report)
        print("Apply the change to BOTH copies. If the difference is genuinely "
              "meant to be\nper-cluster, it needs a new rule in "
              "scripts/check-keel-fresh-parity.py, added\nin the same commit "
              "and with its reason written down.")
        return 1

    print("OK: both keel-fresh copies match outside %d sanctioned region kind(s)"
          % (len(SCRIPT_RULES) + len(MANIFEST_RULES)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
