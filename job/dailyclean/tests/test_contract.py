"""Cross-implementation contract tests.

The Java and Python sides must agree byte-for-byte on the *shape* of the
suspension record and the patch bodies. These tests pin the JSON payloads to the
same literals used by ``CronJobSuspensionTest`` so a divergence is caught here.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dailyclean_ops.cronjob import build_resume_patch, build_suspend_patch  # noqa: E402
from tests.fakes import make_cronjob  # noqa: E402


def as_json(payload):
    return json.loads(json.dumps(payload, sort_keys=True))


class SharedContractTestCase(unittest.TestCase):
    def test_suspend_patch_shape_matches_the_documented_example(self):
        obj = make_cronjob("nightly-report", uid="0f1c", resource_version="7")
        patch = as_json(build_suspend_patch(obj, "2026-11-23T01:00:00Z"))

        expected = {
            "metadata": {
                "resourceVersion": "7",
                "annotations": {
                    "axa.com/dailyclean-suspend-managed-by": "dailyclean",
                    "axa.com/dailyclean-suspend-uid": "0f1c",
                    "axa.com/dailyclean-suspend-at": "2026-11-23T01:00:00Z",
                },
            },
            "spec": {"suspend": True},
        }
        self.assertEqual(patch, expected)

    def test_resume_patch_shape_matches_the_documented_example(self):
        obj = make_cronjob("nightly-report", uid="0f1c", resource_version="7")
        patch = json.loads(json.dumps(build_resume_patch(obj)))

        expected = {
            "metadata": {
                "resourceVersion": "7",
                "annotations": {
                    "axa.com/dailyclean-suspend-managed-by": None,
                    "axa.com/dailyclean-suspend-uid": None,
                    "axa.com/dailyclean-suspend-at": None,
                },
            },
            "spec": {"suspend": False},
        }
        self.assertEqual(patch, expected)

    def test_metadata_key_set_is_stable(self):
        obj = make_cronjob("x", uid="u", resource_version="1")
        for builder in (lambda: build_suspend_patch(obj, "t"), lambda: build_resume_patch(obj)):
            patch = builder()
            self.assertEqual(set(patch), {"metadata", "spec"})
            self.assertEqual(set(patch["metadata"]), {"resourceVersion", "annotations"})
            self.assertEqual(set(patch["metadata"]["annotations"]), {
                "axa.com/dailyclean-suspend-managed-by",
                "axa.com/dailyclean-suspend-uid",
                "axa.com/dailyclean-suspend-at",
            })


class HpaNeverWrittenTestCase(unittest.TestCase):
    def test_only_read_methods_are_used_on_the_hpa_reader(self):
        from dailyclean_ops.cluster import KubernetesHpaReader

        public = [name for name in dir(KubernetesHpaReader) if not name.startswith("_")]
        # The reader must not expose any write verb.
        for verb in ("patch", "update", "create", "delete", "replace"):
            self.assertFalse(
                any(verb in name for name in public),
                f"HPA reader must stay read-only, found {verb}",
            )


if __name__ == "__main__":
    unittest.main()
