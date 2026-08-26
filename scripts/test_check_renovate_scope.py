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
    def test_a_floating_tag_is_banned_in_each_no_float_namespace(self):
        # BEHAVIOUR, not a restatement of the constant. An earlier version of
        # this test asserted NO_FLOAT_NAMESPACES equalled its own literal, which
        # detects a change without proving the change matters. What matters is
        # that a floating tag FAILS in those four namespaces and passes in an
        # ordinary one.
        for namespace in ("health", "hindsight", "ops", "backup"):
            failures, _adv = crs.analyse_render(
                "homelab", _pod("influxdb:latest", namespace=namespace),
                _PATTERNS_HOMELAB, [], _OWNED_HOMELAB)
            self.assertEqual(len(failures), 1, namespace)
            self.assertIn("forbids", failures[0])
        failures, _adv = crs.analyse_render(
            "homelab", _pod("influxdb:latest", namespace="downloads"),
            _PATTERNS_HOMELAB, [], _OWNED_HOMELAB)
        self.assertNotIn("forbids", " ".join(failures))

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

    def test_a_rooted_glob_does_not_match_the_same_directory_nested(self):
        # THE OVER-MATCH THIS TEST EXISTS FOR. An earlier version stripped the
        # stars off a glob and looked for the bare directory anywhere in the
        # path, so `secrets/**` -- which Renovate anchors at the repo root --
        # silently exempted `homelab/secrets/x.yaml` too. Exempting a file the
        # check should have judged is the failure direction that hides things.
        self.assertFalse(crs.path_ignored("homelab/secrets/x.yaml",
                                          ["secrets/**"]))
        self.assertTrue(crs.path_ignored("secrets/x.yaml", ["secrets/**"]))


# --- fixtures for the reader and the scope evaluator -------------------------
# `analyse_render` takes the rendered stream, so these exercise the whole
# verdict without a `kustomize` on PATH.

_PATTERNS_HOMELAB = crs._compile_patterns(
    ["/^homelab/.+\\.yaml$/"], "test.managerFilePatterns")
_PATTERNS_VPS = crs._compile_patterns(
    ["/^vps/.+\\.yaml$/"], "test.managerFilePatterns")

# (path, owning cluster, image values) -- what source_index() produces.
_OWNED_HOMELAB = [("homelab/workloads/x.yaml", "homelab",
                   frozenset({"influxdb:latest", "influxdb:2.9.1",
                              "alpine:3.20"}))]


def _pod(image, namespace="downloads", annotations="", name="thing",
         kind="Deployment"):
    """One minimal pod-parent document naming one image."""
    meta = "  annotations:\n" + annotations if annotations else ""
    return ("apiVersion: apps/v1\nkind: %s\nmetadata:\n%s  name: %s\n"
            "  namespace: %s\nspec:\n  template:\n    spec:\n"
            "      containers:\n      - image: %s\n        name: app\n"
            % (kind, meta, name, namespace, image))


# A two-container Deployment (floating app + pinned sidecar, the shape the
# classifier has to get right) followed by a ConfigMap carrying a NESTED Pod as
# a block scalar -- which is exactly how local-path-provisioner ships its
# `busybox` helper, and which must not be attributed to the Deployment.
FIXTURE_RENDER = """apiVersion: apps/v1
kind: Deployment
metadata:
  annotations:
    keel.sh/match-tag: "true"
    keel.sh/pollSchedule: '@every 6h'
    keel.sh/policy: force
    keel.sh/trigger: poll
  name: freshrss
  namespace: vps
spec:
  template:
    metadata:
      annotations:
        keel.sh/policy: never
    spec:
      containers:
      - env:
        - name: TZ
          value: UTC
        image: lscr.io/linuxserver/freshrss:latest
        imagePullPolicy: Always
        name: freshrss
      - args:
        - -c
        - sleep 1
        command:
        - /bin/sh
        image: alpine:3.20
        name: quiesce
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: local-path-config
  namespace: local-path-storage
data:
  helperPod.yaml: |-
    apiVersion: v1
    kind: Pod
    metadata:
      name: helper-pod
    spec:
      containers:
      - name: helper-pod
        image: busybox
"""

_FIXTURE_DEPLOYMENT = crs.documents(FIXTURE_RENDER)[0]


class TestImageRepo(unittest.TestCase):
    def test_a_tag_is_stripped(self):
        self.assertEqual(crs.image_repo("alpine:3.20"), "alpine")

    def test_a_digest_is_stripped(self):
        self.assertEqual(
            crs.image_repo("ghcr.io/keel-hq/keel:0.22.1@sha256:" + "a" * 64),
            "ghcr.io/keel-hq/keel")

    def test_a_registry_port_is_not_mistaken_for_a_tag(self):
        self.assertEqual(crs.image_repo("registry.local:5000/thing:1.2"),
                         "registry.local:5000/thing")

    def test_an_untagged_reference_is_returned_whole(self):
        self.assertEqual(crs.image_repo("busybox"), "busybox")


class TestTheYamlReader(unittest.TestCase):
    """The riskiest code in the guard: hand-rolled, with no upstream to fall
    back on. Everything else is judged over whatever this returns."""

    def test_both_containers_are_found_with_their_names(self):
        self.assertEqual(
            crs.containers_of(_FIXTURE_DEPLOYMENT),
            [("freshrss", "lscr.io/linuxserver/freshrss:latest"),
             ("quiesce", "alpine:3.20")])

    def test_an_env_var_name_is_not_mistaken_for_the_container_name(self):
        # `- name: TZ` sits deeper than the container's keys. Reading it would
        # label the app container `TZ`.
        names = [name for name, _image in crs.containers_of(_FIXTURE_DEPLOYMENT)]
        self.assertNotIn("TZ", names)

    def test_the_workload_annotations_are_read_and_the_templates_are_not(self):
        # The Pod template carries `keel.sh/policy: never`. Reading the template
        # would report a policy the workload does not have -- and keel reads the
        # workload's own annotations, not the template's.
        self.assertEqual(crs.workload_annotations(_FIXTURE_DEPLOYMENT),
                         FULL_KEEL)

    def test_a_nested_pod_is_not_attributed_to_the_deployment(self):
        images = [image for _n, image in crs.containers_of(_FIXTURE_DEPLOYMENT)]
        self.assertNotIn("busybox", images)

    def test_the_nested_pod_lives_in_a_configmap_and_is_never_judged(self):
        # It is a ConfigMap to this reader and to `kubectl apply` alike, so it
        # is skipped by kind rather than found and judged. Documented as a known
        # blind spot; it comes from a remote base, so it would be advisory.
        configmap = crs.documents(FIXTURE_RENDER)[1]
        self.assertEqual(crs.scalar(configmap, "kind", 0), "ConfigMap")
        failures, _adv = crs.analyse_render(
            "vps", FIXTURE_RENDER, _PATTERNS_VPS, [], [])
        self.assertNotIn("busybox", " ".join(failures))

    def test_has_content_ignores_blanks_and_comments(self):
        self.assertFalse(crs.has_content("\n  # just a comment\n\n"))
        self.assertTrue(crs.has_content("\nkind: Pod\n"))


class TestScopeEvaluation(unittest.TestCase):
    def test_an_owner_in_the_other_clusters_tree_confers_no_scope(self):
        # THE FALSE GREEN THIS TEST EXISTS FOR. Both trees name the same images
        # -- `restic/restic:0.17.3` and the keel digest are in all four backup
        # files and both keel.yamls. A repo-wide owner lookup let a WATCHED
        # homelab file vouch for an UNWATCHED VPS container: with scope
        # simulated as `homelab/**`, the VPS render dropped from nine findings
        # to six and restic-backup, restic-init and keel all went quiet.
        source = [("homelab/workloads/x.yaml", "homelab",
                   frozenset({"alpine:3.20"})),
                  ("vps/workloads/y.yaml", "vps", frozenset({"alpine:3.20"}))]
        failures, _adv = crs.analyse_render(
            "vps", _pod("alpine:3.20", namespace="vps"),
            _PATTERNS_HOMELAB, [], source)
        self.assertEqual(len(failures), 1)
        self.assertIn("no file naming it is inside Renovate's scope", failures[0])
        self.assertIn("vps/workloads/y.yaml", failures[0])
        self.assertNotIn("homelab/workloads/x.yaml", failures[0])

    def test_an_owner_in_this_clusters_tree_and_in_scope_passes(self):
        failures, advisories = crs.analyse_render(
            "homelab", _pod("influxdb:2.9.1", namespace="health"),
            _PATTERNS_HOMELAB, [], _OWNED_HOMELAB)
        self.assertEqual(failures, [])
        self.assertEqual(advisories, [])

    def test_an_ignored_owner_file_confers_no_scope(self):
        source = [("homelab/secrets/x.yaml", "homelab",
                   frozenset({"influxdb:2.9.1"}))]
        failures, _adv = crs.analyse_render(
            "homelab", _pod("influxdb:2.9.1"), _PATTERNS_HOMELAB,
            ["**/secrets/**"], source)
        self.assertEqual(len(failures), 1)

    def test_an_image_no_file_names_is_advisory_not_a_failure(self):
        failures, advisories = crs.analyse_render(
            "homelab", _pod("quay.io/jetstack/cert-manager-controller:v1.20.2"),
            _PATTERNS_HOMELAB, [], _OWNED_HOMELAB)
        self.assertEqual(failures, [])
        self.assertEqual(len(advisories), 1)
        self.assertIn("remote base", advisories[0])

    def test_a_remote_base_that_is_frozen_is_advisory_not_a_failure(self):
        # Ownership is established BEFORE the frozen branch, so "remote-base
        # images are advisory" holds in every mode. Otherwise a remote base that
        # ever shipped keel annotations on a pinned tag would hard-fail an apply
        # over a manifest this repo cannot edit.
        annotations = "".join("    %s: %r\n" % (k, v)
                              for k, v in sorted(FULL_KEEL.items()))
        failures, advisories = crs.analyse_render(
            "homelab", _pod("someone/else:v1.2.3", annotations=annotations),
            _PATTERNS_HOMELAB, [], _OWNED_HOMELAB)
        self.assertEqual(failures, [])
        self.assertEqual(len(advisories), 1)
        self.assertIn("frozen", advisories[0])

    def test_a_pod_parent_with_no_image_is_reported_not_dropped(self):
        doc = ("apiVersion: batch/v1\nkind: CronJob\nmetadata:\n"
               "  name: empty\n  namespace: ops\nspec: {}\n")
        failures, advisories = crs.analyse_render(
            "homelab", doc, _PATTERNS_HOMELAB, [], _OWNED_HOMELAB)
        self.assertEqual(failures, [])
        self.assertEqual(len(advisories), 1)
        self.assertIn("found no `image:`", advisories[0])

    def test_a_document_with_no_readable_kind_is_reported_not_dropped(self):
        failures, advisories = crs.analyse_render(
            "homelab", "notAKind: something\n", _PATTERNS_HOMELAB, [],
            _OWNED_HOMELAB)
        self.assertEqual(failures, [])
        self.assertEqual(len(advisories), 1)
        self.assertIn("could not parse", advisories[0])

    def test_two_containers_sharing_an_image_are_told_apart(self):
        # `vps/workloads/umami.yaml` really does declare postgres:16-alpine on
        # two containers, and the first draft printed two byte-identical lines.
        failures, _adv = crs.analyse_render("vps", FIXTURE_RENDER,
                                            _PATTERNS_HOMELAB, [],
                                            [("vps/workloads/f.yaml", "vps",
                                              frozenset({"alpine:3.20"}))])
        self.assertEqual(len(failures), 1)
        self.assertIn("(quiesce)", failures[0])


class TestDeadPatterns(unittest.TestCase):
    def test_a_pattern_matching_nothing_is_named(self):
        # A typo in a pattern is accepted by Renovate and matches no file
        # forever: no scan, no pull request, and update-watch green over it.
        source = [("homelab/health/influxdb.yaml", "homelab", frozenset())]
        typo = crs._compile_patterns(["/^homelab/helth/.+\\.yaml$/"], "test")
        self.assertEqual(crs.dead_patterns(typo, source, []),
                         ["/^homelab/helth/.+\\.yaml$/"])

    def test_a_pattern_matching_a_real_file_is_not_named(self):
        source = [("homelab/health/influxdb.yaml", "homelab", frozenset())]
        good = crs._compile_patterns(["/^homelab/health/.+\\.yaml$/"], "test")
        self.assertEqual(crs.dead_patterns(good, source, []), [])

    def test_a_pattern_whose_only_match_is_ignored_is_dead(self):
        source = [("homelab/secrets/x.yaml", "homelab", frozenset())]
        pattern = crs._compile_patterns(["/^homelab/secrets/.+\\.yaml$/"], "test")
        self.assertEqual(len(crs.dead_patterns(pattern, source,
                                               ["**/secrets/**"])), 1)


class TestEnabledManagers(unittest.TestCase):
    """`enabledManagers` is a WHITELIST: naming any manager disables the rest.
    A config this check cannot trust is exit 2, never a pass."""

    def _load(self, config):
        import json
        import tempfile
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(config, handle)
        handle.close()
        original, crs.RENOVATE_JSON = crs.RENOVATE_JSON, handle.name
        try:
            return crs.load_renovate()
        finally:
            crs.RENOVATE_JSON = original
            os.unlink(handle.name)

    BASE = {"kubernetes": {"managerFilePatterns": ["/^homelab/.+\\.yaml$/"]}}

    def test_kubernetes_missing_from_the_whitelist_cannot_run(self):
        config = dict(self.BASE, enabledManagers=["kustomize"])
        with self.assertRaises(crs.CheckUnrunnable):
            self._load(config)

    def test_a_kustomize_block_not_in_the_whitelist_cannot_run(self):
        config = dict(self.BASE, enabledManagers=["kubernetes"],
                      kustomize={"managerFilePatterns": ["/kustomization/"]})
        with self.assertRaises(crs.CheckUnrunnable):
            self._load(config)

    def test_both_enabled_is_fine(self):
        config = dict(self.BASE, enabledManagers=["kubernetes", "kustomize"],
                      kustomize={"managerFilePatterns": ["/kustomization/"]})
        patterns, kustomize, _ignore = self._load(config)
        self.assertEqual(len(patterns), 1)
        self.assertEqual(len(kustomize), 1)

    def test_no_whitelist_at_all_means_every_manager_is_on(self):
        patterns, kustomize, ignore = self._load(dict(self.BASE))
        self.assertEqual(len(patterns), 1)
        self.assertEqual(kustomize, [])
        self.assertEqual(ignore, [])


if __name__ == "__main__":
    unittest.main()
