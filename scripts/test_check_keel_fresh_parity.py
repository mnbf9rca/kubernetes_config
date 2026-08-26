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
            self.original.replace("IMAGE_FLOOR=7", "IMAGE_FLOOR=6")))


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
