#!/usr/bin/env python3
"""Unit tests for check-renovate-scope.py.

Stdlib `unittest` only, like every other suite in this repo: there is no Python
toolchain here and a test that needed installing would not get run.

    python3 scripts/test_check_renovate_scope.py

What these lock down is the classifier, because every bug in it is INVISIBLE AT
RUNTIME. A classifier that reads a pinned-plus-keel container as keel-managed
reports a green estate over a workload frozen at the version it was written at
-- which is the exact state traefik and meilisearch were in for months.

The cases below are chosen from the SHAPES THIS ESTATE ACTUALLY CONTAINS, not
from the shapes that are easy to write. An earlier draft of this suite passed
19 tests over a classifier that failed on seven real containers, because it
tested none of: a pinned sidecar inside a keel-managed workload, a `-latest`
suffix tag, a bare major-version stream, or a floating image in a namespace
outside NO_FLOAT_NAMESPACES. Those four are the first four classes here.
"""
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_HERE, "check-renovate-scope.py")
_spec = importlib.util.spec_from_file_location("check_renovate_scope", _PATH)
crs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crs)

FULL_KEEL = {
    "keel.sh/policy": "force",
    "keel.sh/match-tag": "true",
    "keel.sh/trigger": "poll",
    "keel.sh/pollSchedule": "@every 6h",
}


class TestIsPinned(unittest.TestCase):
    def test_a_digest_is_pinned(self):
        self.assertTrue(crs.is_pinned("ghcr.io/keel-hq/keel@sha256:" + "a" * 64))

    def test_a_semver_tag_is_pinned(self):
        self.assertTrue(crs.is_pinned("traefik:v3.3"))

    def test_latest_is_not_pinned(self):
        self.assertFalse(crs.is_pinned("ghcr.io/mnbf9rca/jottacloud-backup:latest"))

    def test_no_tag_at_all_is_not_pinned(self):
        self.assertFalse(crs.is_pinned("busybox"))

    def test_a_registry_port_is_not_mistaken_for_a_tag(self):
        self.assertFalse(crs.is_pinned("registry.local:5000/thing"))

    def test_a_release_channel_tag_is_not_pinned(self):
        self.assertFalse(crs.is_pinned("ghcr.io/karakeep-app/karakeep:release"))

    def test_a_latest_suffix_tag_is_floating(self):
        # umami publishes per-database builds this way and keel tracks it.
        # Reading `postgresql-latest` as a pin reported umami FROZEN.
        self.assertFalse(
            crs.is_pinned("ghcr.io/umami-software/umami:postgresql-latest"))
        self.assertTrue(crs.is_floating_tag("postgresql-latest"))

    def test_a_bare_major_version_stream_is_floating(self):
        # uptime-kuma publishes `2`, which moves on every 2.x release.
        self.assertFalse(crs.is_pinned("louislam/uptime-kuma:2"))
        self.assertFalse(crs.is_pinned("louislam/uptime-kuma:v2"))

    def test_a_dotted_version_is_still_a_pin(self):
        # The boundary of the stream rule. Calling any of these floating would
        # hand a Renovate-managed pin to keel.
        for reference in ("alpine:3.20", "traefik:v3.3", "postgres:16-alpine",
                          "influxdb:2.9.1", "pgvector/pgvector:0.8.1-pg17"):
            self.assertTrue(crs.is_pinned(reference), reference)


class TestClassifyContainer(unittest.TestCase):
    def test_full_keel_annotations_on_a_floating_tag_is_keel_managed(self):
        mode, why = crs.classify_container("emby/embyserver:latest", FULL_KEEL)
        self.assertEqual(mode, crs.MODE_KEEL)
        self.assertEqual(why, "")

    def test_full_keel_annotations_on_a_pinned_tag_is_the_frozen_state(self):
        mode, why = crs.classify_container("traefik:v3.3", FULL_KEEL)
        self.assertEqual(mode, crs.MODE_FROZEN)
        self.assertIn("match-tag", why)

    def test_a_pinned_sidecar_in_a_keel_managed_workload_is_renovate_territory(self):
        # THE MODELLING BUG THIS CLASS EXISTS FOR. The quiesce and
        # sqlite-snapshot sidecars are `alpine:3.20` inside Deployments whose
        # APP image floats. Applying the workload's annotations to every
        # container called four correct, intended sidecars "frozen" and would
        # have blocked every apply on the VPS cluster.
        mode, why = crs.classify_container("alpine:3.20", FULL_KEEL,
                                           workload_floats=True)
        self.assertEqual(mode, crs.MODE_PINNED)
        self.assertEqual(why, "")

    def test_the_frozen_verdict_needs_every_container_pinned(self):
        # traefik and meilisearch: keel annotations with NOTHING floating for
        # keel to track. That, and only that, is the frozen state.
        mode, _why = crs.classify_container("traefik:v3.3", FULL_KEEL,
                                            workload_floats=False)
        self.assertEqual(mode, crs.MODE_FROZEN)

    def test_a_missing_match_tag_is_a_failure_even_on_a_floating_tag(self):
        partial = dict(FULL_KEEL)
        del partial["keel.sh/match-tag"]
        mode, why = crs.classify_container("emby/embyserver:latest", partial)
        self.assertEqual(mode, crs.MODE_INCOMPLETE_KEEL)
        self.assertIn("match-tag", why)

    def test_any_missing_annotation_is_incomplete(self):
        for missing in sorted(crs.KEEL_ANNOTATIONS):
            partial = {k: v for k, v in FULL_KEEL.items() if k != missing}
            mode, _why = crs.classify_container("emby/x:latest", partial)
            self.assertEqual(mode, crs.MODE_INCOMPLETE_KEEL, missing)

    def test_no_annotations_and_a_pin_is_renovate_territory(self):
        mode, why = crs.classify_container("influxdb:2.9.1", {})
        self.assertEqual(mode, crs.MODE_PINNED)
        self.assertEqual(why, "")

    def test_no_annotations_and_a_floating_tag_is_unmanaged(self):
        mode, why = crs.classify_container("busybox:latest", {})
        self.assertEqual(mode, crs.MODE_FLOATING_UNMANAGED)
        self.assertIn("nothing", why)


class TestFloatingBans(unittest.TestCase):
    def test_the_pinned_namespaces_are_the_ones_that_forbid_keel(self):
        self.assertEqual(crs.NO_FLOAT_NAMESPACES,
                         frozenset({"health", "hindsight", "ops", "backup"}))

    def test_jottacloud_is_exempt_in_the_namespace_it_actually_lives_in(self):
        # THE UNREACHABLE-CODE BUG THIS TEST EXISTS FOR. The first draft keyed
        # the entry on `backup`, but the workload's real namespace is
        # `jottacloud-backup`, so the exemption could never fire anywhere.
        self.assertTrue(crs.floating_exempt(
            "jottacloud-backup", "ghcr.io/mnbf9rca/jottacloud-backup:latest"))

    def test_the_exemption_also_covers_the_namespace_it_might_move_to(self):
        self.assertTrue(crs.floating_exempt(
            "backup", "ghcr.io/mnbf9rca/jottacloud-backup:latest"))

    def test_the_exempt_image_is_unmanaged_so_the_exemption_must_be_consulted_there(self):
        # The exemption is only useful if it is reachable from the arm the
        # image actually lands on. jottacloud-backup carries NO keel
        # annotations at all, so it classifies as floating-unmanaged, not as
        # a floating-in-a-banned-namespace case.
        mode, _why = crs.classify_container(
            "ghcr.io/mnbf9rca/jottacloud-backup:latest", {})
        self.assertEqual(mode, crs.MODE_FLOATING_UNMANAGED)
        self.assertNotIn("jottacloud-backup", crs.NO_FLOAT_NAMESPACES)

    def test_the_exemption_does_not_cover_anything_else(self):
        self.assertFalse(crs.floating_exempt("backup", "restic/restic:latest"))
        self.assertFalse(crs.floating_exempt("health", "influxdb:latest"))
        self.assertFalse(crs.floating_exempt(
            "vps", "ghcr.io/mnbf9rca/jottacloud-backup:latest"))

    def test_every_exemption_carries_a_written_reason(self):
        for entry in crs.FLOATING_EXEMPT:
            self.assertTrue(entry.get("reason", "").strip(), entry)
            self.assertTrue(entry.get("image", "").strip(), entry)
            self.assertTrue(entry.get("namespace", "").strip(), entry)


class TestIgnorePaths(unittest.TestCase):
    def test_a_secrets_file_is_out_of_scope(self):
        self.assertTrue(crs.path_ignored("homelab/secrets/hindsight.yaml",
                                         ["**/secrets/**"]))

    def test_a_workload_file_is_in_scope(self):
        self.assertFalse(crs.path_ignored("homelab/health/influxdb.yaml",
                                          ["**/secrets/**"]))

    def test_a_legacy_tree_is_out_of_scope(self):
        self.assertTrue(crs.path_ignored("legacy-microk8s/sonarr.yaml",
                                         ["legacy-microk8s/**"]))


if __name__ == "__main__":
    unittest.main()
