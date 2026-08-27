#!/usr/bin/env python3
"""Unit tests for check-keel-fresh-parity.py.

Stdlib `unittest` only, like every other suite in this repo: there is no Python
toolchain here and a test that needed installing would not get run.

    python3 scripts/test_check_keel_fresh_parity.py

WHAT IS WORTH TESTING HERE IS THE FAILURE DIRECTION. A parity guard that passes
is indistinguishable from a parity guard that masks everything, and the second
one is worse than no guard at all: it reports a matched pair over two copies
that have drifted apart, which is exactly the false green the `keel-fresh` job
itself exists to abolish. So most of these assert that something FAILS.

The masking is exercised against the real repository files rather than fixtures,
because the thing most likely to break this guard is somebody rewording a
sanctioned region in those files -- and a fixture would go on passing while the
real pair went unchecked.
"""
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_HERE, "check-keel-fresh-parity.py")
_spec = importlib.util.spec_from_file_location("check_keel_fresh_parity", _PATH)
kfp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kfp)


class TestTheRealPair(unittest.TestCase):
    """The committed files must pass, and every rule must still find its region."""

    def test_the_committed_pair_matches(self):
        for label, homelab, vps, rules in kfp.PAIRS:
            self.assertIsNone(kfp.divergence(label, homelab, vps, rules),
                              "%s diverged" % label)

    def test_every_rule_still_matches_its_region(self):
        # A stale rule raises CheckUnrunnable from mask(); this is the assertion
        # that the allowlist has not drifted away from the sources it describes.
        for label, homelab, vps, rules in kfp.PAIRS:
            kfp.mask(kfp.read(homelab), rules, "homelab", label)
            kfp.mask(kfp.read(vps), rules, "vps", label)


class TestDivergenceIsCaught(unittest.TestCase):
    """A behavioural edit to one copy must be reported, wherever it lands."""

    def setUp(self):
        self.label, self.homelab, self.vps, self.rules = kfp.PAIRS[0]
        self.original = kfp.read(self.vps)

    def _diverges(self, mutated):
        left = kfp.mask(kfp.read(self.homelab), self.rules, "homelab", self.label)
        right = kfp.mask(mutated, self.rules, "vps", self.label)
        return left != right

    def test_a_changed_metric_name_diverges(self):
        self.assertTrue(self._diverges(self.original.replace(
            "POLL_METRIC=registries_scanned_total",
            "POLL_METRIC=registries_scanned_total_v2")))

    def test_a_changed_verdict_diverges(self):
        self.assertTrue(self._diverges(self.original.replace(
            "VERDICT=polls-stalled", "VERDICT=polls-slow")))

    def test_a_deleted_guard_diverges(self):
        # The zero-counter check is one of the two fixes that closed a false
        # green. Losing it on one cluster only is the whole point of this guard.
        self.assertTrue(self._diverges(self.original.replace(
            'if [ "$POLLS" -eq 0 ]; then', 'if [ "$POLLS" -lt 0 ]; then')))

    def test_a_reordered_branch_diverges(self):
        # Ordering is behaviour: the restart branch must stay above the
        # zero-counter check. Swapping two lines is a diff either way.
        lines = self.original.splitlines(True)
        index = next(i for i, line in enumerate(lines)
                     if line.strip() == "VERDICT=restarted")
        lines[index], lines[index + 1] = lines[index + 1], lines[index]
        self.assertTrue(self._diverges("".join(lines)))

    def test_an_edit_inside_a_sanctioned_region_does_not_diverge(self):
        # IMAGE_FLOOR is meant to differ; changing it must stay silent, or the
        # guard would forbid the one difference the design requires.
        self.assertFalse(self._diverges(
            self.original.replace("IMAGE_FLOOR=8", "IMAGE_FLOOR=6")))


class TestNoRuleSwallowsCode(unittest.TestCase):
    """The boundary that a suite of otherwise sound cases still missed.

    A masking rule is only as safe as its span. The first version of the
    `IMAGE_FLOOR` rule used `.*?` under `re.S` and therefore swallowed every
    line between the comment's first line and the assignment -- nineteen on
    homelab, thirty-one on the VPS -- whatever those lines held. Real shell
    inserted into that window in ONE copy passed at exit 0, and
    `check-script-lint` could not have helped: the insertion is valid `sh` on
    both sides. The window sits directly above the one constant a future editor
    is most likely to touch.

    These assert the property rather than that one regex: no rule may span a
    line it does not name. The outcome is CheckUnrunnable (exit 2) rather than a
    reported divergence (exit 1), and that is the better of the two -- it names
    the rule, the observed count and the expected one, instead of printing a
    diff and leaving the reader to work out which rule stopped fitting.
    """

    def _insert_above(self, text, marker, injected):
        return text.replace(marker, injected + "\n" + marker)

    def test_code_above_IMAGE_FLOOR_is_not_swallowed(self):
        label, _, vps, rules = kfp.PAIRS[0]
        mutated = self._insert_above(
            kfp.read(vps), "IMAGE_FLOOR=8",
            "IMAGE_FLOOR_OVERRIDE_HACK=1\n"
            "curl -fsS -m 5 http://evil.example/ >/dev/null || true")
        with self.assertRaises(kfp.CheckUnrunnable):
            kfp.mask(mutated, rules, "vps", label)

    def test_code_above_IMAGE_FLOOR_on_the_homelab_side_is_not_swallowed(self):
        # The rule is symmetric; a hole on one side is a hole on the other.
        label, homelab, _, rules = kfp.PAIRS[0]
        mutated = self._insert_above(
            kfp.read(homelab), "IMAGE_FLOOR=4", "rm -rf /state")
        with self.assertRaises(kfp.CheckUnrunnable):
            kfp.mask(mutated, rules, "homelab", label)

    def test_yaml_above_the_schedule_is_not_swallowed(self):
        label, homelab, _, rules = kfp.PAIRS[1]
        mutated = self._insert_above(
            kfp.read(homelab), '  schedule: "15 7 * * *"', "  suspend: true")
        with self.assertRaises(kfp.CheckUnrunnable):
            kfp.mask(mutated, rules, "homelab", label)

    def test_yaml_above_the_nodeSelector_is_not_swallowed(self):
        label, _, vps, rules = kfp.PAIRS[1]
        mutated = self._insert_above(
            kfp.read(vps), "          nodeSelector:",
            "          hostNetwork: true")
        with self.assertRaises(kfp.CheckUnrunnable):
            kfp.mask(mutated, rules, "vps", label)

    def test_no_rule_swallows_an_injected_line_anywhere(self):
        """The general property, applied to every multi-line rule mechanically.

        The four cases above name their insertion points, so they cannot cover
        an eleventh rule nobody has written yet. This one derives the point:
        for each rule that spans more than one line, drop a sentinel
        immediately after the first line of its match and assert NO match
        contains the sentinel afterwards. A comment-bounded span breaks; a
        `.*`-across-newlines span absorbs it and keeps matching, which is the
        whole failure.

        Written this way after the first attempt at a general test turned out
        to be vacuous: asserting a shape over the CLEAN files passes against a
        loose rule too, because clean files have nothing in the window for a
        loose rule to swallow. A span test has to inject something.
        """
        sentinel = "SENTINEL_INJECTED_LINE=1"
        checked = 0
        for label, homelab, vps, rules in kfp.PAIRS:
            for which, relative in (("homelab", homelab), ("vps", vps)):
                text = kfp.read(relative)
                index = 3 if which == "homelab" else 4
                for rule in rules:
                    name, pattern = rule[0], rule[1]
                    if rule[index] is None:
                        continue
                    for match in pattern.finditer(text):
                        body = match.group(0)
                        if "\n" not in body.rstrip("\n"):
                            continue          # single line: nothing to swallow
                        first, rest = body.split("\n", 1)
                        mutated = text.replace(
                            body, first + "\n" + sentinel + "\n" + rest, 1)
                        checked += 1
                        for after in pattern.finditer(mutated):
                            self.assertNotIn(
                                sentinel, after.group(0),
                                "rule %r in %s swallowed an unnamed line"
                                % (name, relative))
        self.assertGreater(checked, 0, "no multi-line rule was exercised")


class TestStaleAllowlistIsAnError(unittest.TestCase):
    """A rule that stops matching must report itself, never mask nothing quietly."""

    def test_a_reworded_sanctioned_region_is_unrunnable(self):
        label, _, vps, rules = kfp.PAIRS[0]
        mutated = kfp.read(vps).replace(
            "# The literal floor for tracked images.",
            "# The tracked-image floor.")
        with self.assertRaises(kfp.CheckUnrunnable):
            kfp.mask(mutated, rules, "vps", label)

    def test_a_one_cluster_region_appearing_in_the_other_is_unrunnable(self):
        # The copy note names the other file. If it is ever pasted into the
        # homelab copy, the two would "match" while both claimed to be copies
        # of the same original.
        label, homelab, vps, rules = kfp.PAIRS[1]
        note = ("# A DELIBERATE COPY of homelab/ops/keel-fresh.yaml. See the note "
                "in this\n# directory's scripts/keel-fresh.sh: edit the two "
                "together.\n#\n")
        with self.assertRaises(kfp.CheckUnrunnable):
            kfp.mask(note + kfp.read(homelab), rules, "homelab", label)


class TestOperationalContract(unittest.TestCase):
    def test_it_takes_no_cluster_argument(self):
        # It compares the two trees against each other, so a per-cluster half of
        # it is not a thing that exists. Passing one must be an error, not a
        # silently ignored argument that makes a caller think it scoped the run.
        self.assertEqual(kfp.main(["check-keel-fresh-parity.py", "vps"]), 2)

    def test_a_missing_file_is_unrunnable_not_a_pass(self):
        with self.assertRaises(kfp.CheckUnrunnable):
            kfp.read("vps/ops/scripts/keel-fresh-that-does-not-exist.sh")


if __name__ == "__main__":
    unittest.main(verbosity=2)
