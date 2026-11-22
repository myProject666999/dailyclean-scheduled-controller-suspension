from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dailyclean_ops.restore import plan_restore, plan_restore_with_reader  # noqa: E402
from tests.test_hpa import make_hpa  # noqa: E402


class Reader:
    def __init__(self, hpas, fail=False):
        self._hpas = hpas
        self._fail = fail

    def list_hpas(self):
        if self._fail:
            raise RuntimeError("boom")
        return self._hpas


class PlanRestoreTestCase(unittest.TestCase):
    def test_no_hpa_restores_candidate(self):
        plan = plan_restore([], "Deployment", "web", candidate=4)
        self.assertTrue(plan.should_restore)
        self.assertEqual(plan.replicas, 4)

    def test_hpa_clamps_candidate(self):
        hpas = [make_hpa(name="web-hpa", target="web", min_replicas=3, max_replicas=10)]
        self.assertEqual(plan_restore(hpas, "Deployment", "web", 20).replicas, 10)
        self.assertEqual(plan_restore(hpas, "Deployment", "web", None).replicas, 3)

    def test_unsupported_kind_is_not_restored(self):
        hpas = [make_hpa(name="r-hpa", kind="Rollout", target="web", api_version="argoproj.io/v1alpha1")]
        plan = plan_restore(hpas, "Rollout", "web", 4, group="argoproj.io")
        self.assertFalse(plan.should_restore)
        self.assertIsNone(plan.replicas)

    def test_conflict_is_not_restored(self):
        hpas = [
            make_hpa(name="a", target="web", min_replicas=2, max_replicas=6),
            make_hpa(name="b", target="web", min_replicas=3, max_replicas=10),
        ]
        plan = plan_restore(hpas, "Deployment", "web", 4)
        self.assertFalse(plan.should_restore)

    def test_reader_read_failure_falls_back_to_candidate(self):
        plan = plan_restore_with_reader(Reader([], fail=True), "Deployment", "web", 5)
        self.assertTrue(plan.should_restore)
        self.assertEqual(plan.replicas, 5)

    def test_reader_read_failure_without_candidate_uses_one(self):
        plan = plan_restore_with_reader(Reader([], fail=True), "Deployment", "web", None)
        self.assertEqual(plan.replicas, 1)

    def test_log_reports_restricted_boundary(self):
        logs = []
        plan = plan_restore_with_reader(
            Reader([make_hpa(target="web", min_replicas=0, max_replicas=10)]),
            "Deployment", "web", None, log=logs.append)
        self.assertTrue(plan.should_restore)
        self.assertEqual(plan.replicas, 1)
        self.assertTrue(any("restricted" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
