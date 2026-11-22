from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dailyclean_ops.hpa import (  # noqa: E402
    SCALING_DISABLED_REASON,
    BoundaryDecision,
    HpaSupport,
    Observation,
    ScaleTargetRef,
    clamp_replicas,
    classify_support,
    evaluate_boundary,
    hpas_for_target,
    observe_after_restore,
    read_target_ref,
)

DEPLOYMENT = ScaleTargetRef(group="apps", kind="Deployment", name="web")


def make_hpa(
    name: str = "web-hpa",
    *,
    kind: str = "Deployment",
    target: str = "web",
    api_version: str = "apps/v1",
    min_replicas: int = 3,
    max_replicas: int = 10,
    metrics=None,
    scaling_active=None,
    reason=None,
) -> dict:
    conditions = []
    if scaling_active is not None:
        conditions.append({"type": "ScalingActive", "status": scaling_active, "reason": reason or "ValidMetricFound"})
    return {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": name, "namespace": "test"},
        "spec": {
            "scaleTargetRef": {"apiVersion": api_version, "kind": kind, "name": target},
            "minReplicas": min_replicas,
            "maxReplicas": max_replicas,
            "metrics": metrics
            if metrics is not None
            else [{"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 60}}}],
        },
        "status": {"conditions": conditions},
    }


class TargetRefTestCase(unittest.TestCase):
    def test_reads_group_kind_name_not_labels(self):
        hpa = make_hpa()
        hpa["metadata"]["labels"] = {"app": "something-else"}
        ref = read_target_ref(hpa)
        self.assertEqual(ref.key, ("apps", "Deployment", "web"))

    def test_core_group_is_empty(self):
        hpa = make_hpa(api_version="v1", kind="ReplicationController")
        ref = read_target_ref(hpa)
        self.assertEqual(ref.group, "")
        self.assertEqual(ref.kind, "ReplicationController")

    def test_scale_kind_disambiguates_same_name(self):
        deployments = [make_hpa(kind="Deployment", target="web")]
        stateful = ScaleTargetRef(group="apps", kind="StatefulSet", name="web")
        self.assertEqual(hpas_for_target(deployments, stateful), [])
        self.assertEqual(len(hpas_for_target(deployments, DEPLOYMENT)), 1)


class ClampTestCase(unittest.TestCase):
    def test_work_items_examples(self):
        # The three worked examples from the contract.
        self.assertEqual(clamp_replicas(7, 3, 10), 7)
        self.assertEqual(clamp_replicas(20, 3, 10), 10)
        self.assertEqual(clamp_replicas(None, 3, 10), 3)

    def test_below_minimum_clamps_up(self):
        self.assertEqual(clamp_replicas(1, 3, 10), 3)

    def test_no_candidate_uses_at_least_one(self):
        self.assertEqual(clamp_replicas(None, 0, 5), 1)
        self.assertEqual(clamp_replicas(None, 2, 5), 2)


class BoundaryTestCase(unittest.TestCase):
    def test_supported_hpa_clamps_candidate(self):
        boundary = evaluate_boundary([make_hpa()], DEPLOYMENT, 7)
        self.assertIs(boundary.decision, BoundaryDecision.ALLOW)
        self.assertIs(boundary.support, HpaSupport.SUPPORTED)
        self.assertEqual(boundary.replicas, 7)
        self.assertTrue(boundary.may_restore)
        self.assertTrue(boundary.claims_full_control)

    def test_supported_hpa_caps_high_candidate(self):
        boundary = evaluate_boundary([make_hpa()], DEPLOYMENT, 20)
        self.assertEqual(boundary.replicas, 10)
        self.assertIs(boundary.decision, BoundaryDecision.ALLOW)

    def test_supported_hpa_without_candidate_uses_min(self):
        boundary = evaluate_boundary([make_hpa(min_replicas=3)], DEPLOYMENT, None)
        self.assertEqual(boundary.replicas, 3)

    def test_no_hpa_uses_candidate_directly(self):
        boundary = evaluate_boundary([], DEPLOYMENT, 4)
        self.assertIs(boundary.decision, BoundaryDecision.ALLOW)
        self.assertEqual(boundary.replicas, 4)

    def test_no_hpa_no_candidate_uses_one(self):
        boundary = evaluate_boundary([], DEPLOYMENT, None)
        self.assertEqual(boundary.replicas, 1)

    def test_min_replicas_zero_is_restricted_not_faked(self):
        boundary = evaluate_boundary([make_hpa(min_replicas=0)], DEPLOYMENT, None)
        self.assertIs(boundary.decision, BoundaryDecision.RESTRICTED)
        self.assertIs(boundary.support, HpaSupport.RESTRICTED)
        self.assertFalse(boundary.claims_full_control)
        self.assertTrue(boundary.may_restore)
        # Still produces a legal restore value, but does not promise control.
        self.assertEqual(boundary.replicas, 1)
        self.assertIn("may scale the target back up", boundary.reason)

    def test_unsupported_kind_is_rejected(self):
        ref = ScaleTargetRef(group="argoproj.io", kind="Rollout", name="web")
        boundary = evaluate_boundary([make_hpa(kind="Rollout", api_version="argoproj.io/v1alpha1")], ref, None)
        self.assertIs(boundary.decision, BoundaryDecision.UNSUPPORTED)
        self.assertIs(boundary.support, HpaSupport.UNSUPPORTED)
        self.assertFalse(boundary.may_restore)
        self.assertIsNone(boundary.replicas)

    def test_multiple_hpas_same_target_conflict(self):
        hpas = [
            make_hpa(name="web-a", min_replicas=2, max_replicas=6),
            make_hpa(name="web-b", min_replicas=3, max_replicas=10),
        ]
        boundary = evaluate_boundary(hpas, DEPLOYMENT, 4)
        self.assertIs(boundary.decision, BoundaryDecision.CONFLICT)
        self.assertFalse(boundary.may_restore)
        self.assertEqual(boundary.hpa_names, ("web-a", "web-b"))
        # The intersection is a diagnostic only.
        self.assertEqual(boundary.preview_intersection, (3, 6))
        self.assertIn("not proof", boundary.reason)

    def test_multiple_hpas_with_empty_intersection_still_conflict(self):
        hpas = [
            make_hpa(name="a", min_replicas=2, max_replicas=4),
            make_hpa(name="b", min_replicas=8, max_replicas=10),
        ]
        boundary = evaluate_boundary(hpas, DEPLOYMENT, 4)
        self.assertIs(boundary.decision, BoundaryDecision.CONFLICT)
        self.assertIsNone(boundary.preview_intersection)

    def test_invalid_range_is_unsupported(self):
        boundary = evaluate_boundary([make_hpa(min_replicas=10, max_replicas=3)], DEPLOYMENT, 5)
        self.assertIs(boundary.decision, BoundaryDecision.UNSUPPORTED)
        self.assertIsNone(boundary.replicas)

    def test_hpa_without_max_is_unsupported(self):
        hpa = make_hpa()
        del hpa["spec"]["maxReplicas"]
        boundary = evaluate_boundary([hpa], DEPLOYMENT, 5)
        self.assertIs(boundary.decision, BoundaryDecision.UNSUPPORTED)

    def test_hpa_pointing_elsewhere_is_not_matched(self):
        other = make_hpa(name="other", target="api")
        boundary = evaluate_boundary([other], DEPLOYMENT, 5)
        # No HPA targets *this* workload, so the candidate passes through.
        self.assertIs(boundary.decision, BoundaryDecision.ALLOW)
        self.assertEqual(boundary.replicas, 5)
        self.assertEqual(boundary.hpa_names, ())

    def test_classify_support_flags_custom_metrics_still_supported(self):
        hpa = make_hpa(
            metrics=[
                {"type": "External", "external": {"metric": {"name": "queue"}, "target": {"type": "Value", "value": "10"}}}
            ]
        )
        support, _ = classify_support(hpa, DEPLOYMENT)
        self.assertIs(support, HpaSupport.SUPPORTED)


class ObservationTestCase(unittest.TestCase):
    def test_scaling_disabled_after_restore_is_reported_as_not_yet_caught_up(self):
        hpa = make_hpa(scaling_active="False", reason=SCALING_DISABLED_REASON)
        observation = observe_after_restore(hpa)
        self.assertIs(observation.observation, Observation.STILL_DISABLED)
        self.assertIn("has not observed", observation.detail)

    def test_scaling_active_again(self):
        hpa = make_hpa(scaling_active="True", reason="ValidMetricFound")
        observation = observe_after_restore(hpa)
        self.assertIs(observation.observation, Observation.SCALING_ACTIVE)

    def test_no_conditions_is_unknown(self):
        hpa = make_hpa(scaling_active=None)
        observation = observe_after_restore(hpa)
        self.assertIs(observation.observation, Observation.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
