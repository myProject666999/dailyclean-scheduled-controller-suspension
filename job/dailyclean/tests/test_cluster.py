"""Tests for the client adapters.

The adapters are exercised with recording doubles that mimic the kubernetes
client's ``BatchV1Api`` / ``AutoscalingV2Api`` shapes. No socket is opened: the
doubles assert the exact request arguments the real client would receive.
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dailyclean_ops.cluster import (  # noqa: E402
    KubernetesCronJobClient,
    KubernetesHpaReader,
    KubernetesScaleClient,
    utc_now_rfc3339,
)
from dailyclean_ops.cronjob import apply_start, apply_stop  # noqa: E402
from dailyclean_ops.hpa import ScaleTargetRef  # noqa: E402
from tests.fakes import FakeConflict  # noqa: E402
from tests.test_cronjob import owned_annotations  # noqa: E402


class _ListResult:
    def __init__(self, items):
        self.items = items


class RecordingBatchApi:
    """Mimics ``BatchV1Api`` surface used by ``KubernetesCronJobClient``."""

    def __init__(self, objects):
        self.objects = {o.metadata.name: o for o in objects}
        self.calls = []
        self.patched_bodies = []

    def list_namespaced_cron_job(self, namespace, label_selector):
        self.calls.append(("list", namespace, label_selector))
        return _ListResult(list(self.objects.values()))

    def read_namespaced_cron_job(self, name, namespace):
        self.calls.append(("read", name, namespace))
        return self.objects[name]

    def patch_namespaced_cron_job(self, name, namespace, body):
        self.calls.append(("patch", name, namespace))
        self.patched_bodies.append(body)
        obj = self.objects[name]
        if body.get("metadata", {}).get("resourceVersion") not in (None, obj.metadata.resource_version):
            raise FakeConflict()
        if "spec" in body and "suspend" in body["spec"]:
            obj.spec.suspend = body["spec"]["suspend"]
        for key, value in body.get("metadata", {}).get("annotations", {}).items():
            if value is None:
                obj.metadata.annotations.pop(key, None)
            else:
                obj.metadata.annotations[key] = value
        obj.metadata.resource_version = str(int(obj.metadata.resource_version) + 1)
        return obj


def _cronjob(name="report", *, suspend=False, uid="uid-1", rv="1", annotations=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            uid=uid,
            resource_version=rv,
            labels={"axa.com/dailyclean": "true"},
            annotations=dict(annotations or {}),
        ),
        spec=SimpleNamespace(suspend=suspend),
    )


class AdapterTestCase(unittest.TestCase):
    def test_list_uses_the_opt_in_label_selector(self):
        api = RecordingBatchApi([_cronjob()])
        client = KubernetesCronJobClient(api, "test")
        client.list_cron_jobs()
        self.assertEqual(api.calls, [("list", "test", "axa.com/dailyclean=true")])

    def test_stop_via_adapter_sends_only_the_agreed_patch(self):
        api = RecordingBatchApi([_cronjob(rv="4")])
        client = KubernetesCronJobClient(api, "test")

        outcome = apply_stop(client, "report", "2026-11-23T01:00:00Z")

        self.assertEqual(outcome.writes, 1)
        body = api.patched_bodies[0]
        self.assertEqual(set(body), {"metadata", "spec"})
        self.assertEqual(body["spec"], {"suspend": True})
        self.assertEqual(body["metadata"]["resourceVersion"], "4")
        self.assertEqual(
            body["metadata"]["annotations"]["axa.com/dailyclean-suspend-uid"], "uid-1"
        )

    def test_start_via_adapter_clears_the_record(self):
        api = RecordingBatchApi(
            [_cronjob(suspend=True, annotations=owned_annotations("uid-1"))]
        )
        client = KubernetesCronJobClient(api, "test")

        outcome = apply_start(client, "report")

        self.assertEqual(outcome.writes, 1)
        body = api.patched_bodies[0]
        self.assertEqual(body["spec"], {"suspend": False})
        self.assertEqual(
            body["metadata"]["annotations"],
            {
                "axa.com/dailyclean-suspend-managed-by": None,
                "axa.com/dailyclean-suspend-uid": None,
                "axa.com/dailyclean-suspend-at": None,
            },
        )

    def test_read_404_is_reported_as_missing(self):
        class NotFound(Exception):
            status = 404

        class Api:
            def read_namespaced_cron_job(self, name, namespace):
                raise NotFound()

        client = KubernetesCronJobClient(Api(), "test")
        # Simulate the client.exceptions import by patching the module helper.
        import dailyclean_ops.cluster as cluster

        original = cluster._exceptions
        cluster._exceptions = lambda: SimpleNamespace(ApiException=NotFound)
        try:
            self.assertIsNone(client.get_cron_job("ghost"))
        finally:
            cluster._exceptions = original


class ScaleAdapterTestCase(unittest.TestCase):
    def test_scale_patch_targets_only_spec_replicas_and_carries_resource_version(self):
        calls = []

        class AppsApi:
            def read_namespaced_deployment(self, name, namespace):
                return SimpleNamespace(metadata=SimpleNamespace(resource_version="9"))

            def patch_namespaced_deployment_scale(self, name, namespace, body):
                calls.append((name, namespace, body))
                return body

            def patch_namespaced_stateful_set_scale(self, name, namespace, body):
                calls.append((name, namespace, body))
                return body

        client = KubernetesScaleClient(AppsApi(), "test")
        client.scale(ScaleTargetRef("apps", "Deployment", "web"), 3)

        self.assertEqual(len(calls), 1)
        name, namespace, body = calls[0]
        self.assertEqual((name, namespace), ("web", "test"))
        self.assertEqual(body["spec"], {"replicas": 3})
        self.assertEqual(body["metadata"], {"resourceVersion": "9"})

    def test_unsupported_kind_is_rejected_by_the_adapter(self):
        client = KubernetesScaleClient(object(), "test")
        with self.assertRaises(ValueError):
            client.scale(ScaleTargetRef("argoproj.io", "Rollout", "web"), 3)


class HpaReaderTestCase(unittest.TestCase):
    def test_hpas_for_matches_on_target_ref_not_labels(self):
        class AutoscalingApi:
            def list_namespaced_horizontal_pod_autoscaler(self, namespace):
                return _ListResult([
                    SimpleNamespace(
                        metadata=SimpleNamespace(name="web", labels={"wrong": "label"}),
                        spec=SimpleNamespace(
                            scale_target_ref=SimpleNamespace(
                                api_version="apps/v1", kind="Deployment", name="web"
                            )
                        ),
                    ),
                    SimpleNamespace(
                        metadata=SimpleNamespace(name="other"),
                        spec=SimpleNamespace(
                            scale_target_ref=SimpleNamespace(
                                api_version="apps/v1", kind="Deployment", name="api"
                            )
                        ),
                    ),
                ])

        reader = KubernetesHpaReader(AutoscalingApi(), "test")
        matches = reader.hpas_for(ScaleTargetRef("apps", "Deployment", "web"))
        self.assertEqual([m.metadata.name for m in matches], ["web"])


class TimestampTestCase(unittest.TestCase):
    def test_timestamp_is_utc_rfc3339(self):
        value = utc_now_rfc3339()
        self.assertTrue(value.endswith("Z"))
        self.assertNotIn("+00:00", value)
        self.assertNotIn(".", value)  # no sub-second precision


if __name__ == "__main__":
    unittest.main()
