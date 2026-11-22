from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dailyclean_ops.annotations import (  # noqa: E402
    MANAGED_BY_ANNOTATION,
    OWNER_VALUE,
    SUSPENDED_UID_ANNOTATION,
)
from dailyclean_ops.handover import (  # noqa: E402
    Phase,
    StepStatus,
    TargetReport,
    execute_start,
    execute_stop,
)
from dailyclean_ops.hpa import HpaObservation, Observation  # noqa: E402
from tests.fakes import FakeCronJobApi, make_cronjob  # noqa: E402

NOW = "2026-11-23T01:00:00Z"


class RecordingRestorer:
    def __init__(
        self,
        restore: TargetReport | None = None,
        teardown: TargetReport | None = None,
        log: list[str] | None = None,
    ) -> None:
        self._restore = restore or TargetReport.none()
        self._teardown = teardown or TargetReport.none()
        self.calls: list[str] = []
        self.log = log if log is not None else []

    def restore(self) -> TargetReport:
        self.calls.append("restore")
        self.log.append("restore")
        return self._restore

    def teardown(self) -> TargetReport:
        self.calls.append("teardown")
        self.log.append("teardown")
        return self._teardown


def owned(suspend: bool = True, uid: str = "uid-1") -> dict:
    return make_cronjob(
        "report",
        uid=uid,
        suspend=suspend,
        annotations={
            MANAGED_BY_ANNOTATION: OWNER_VALUE,
            SUSPENDED_UID_ANNOTATION: uid,
        },
    )


class StopOrderingTestCase(unittest.TestCase):
    def test_cronjobs_are_suspended_before_targets_are_torn_down(self):
        log: list[str] = []
        api = FakeCronJobApi()
        api.add(make_cronjob("report"))
        original_patch = api.patch_cron_job

        def logging_patch(name, patch):
            log.append("cronjob-patch")
            return original_patch(name, patch)

        api.patch_cron_job = logging_patch
        restorer = RecordingRestorer(teardown=TargetReport.all_ok(["web"]), log=log)

        result = execute_stop(restorer, api, ["report"], NOW)

        self.assertEqual(log, ["cronjob-patch", "teardown"])
        self.assertTrue(result.fully_stopped)
        self.assertEqual(result.phase(Phase.CRONJOB_SUSPEND).status, StepStatus.COMPLETED)

    def test_a_partial_teardown_is_not_reported_as_fully_stopped(self):
        api = FakeCronJobApi()
        api.add(make_cronjob("report"))
        restorer = RecordingRestorer(
            teardown=TargetReport(total=3, succeeded=1, failed=("b", "c"))
        )
        result = execute_stop(restorer, api, ["report"], NOW)
        self.assertFalse(result.fully_stopped)
        self.assertEqual(
            result.phase(Phase.TARGET_TEARDOWN).status, StepStatus.PARTIAL
        )

    def test_failed_teardown_is_not_reported_as_fully_stopped(self):
        api = FakeCronJobApi()
        api.add(make_cronjob("report"))
        restorer = RecordingRestorer(teardown=TargetReport(total=2, succeeded=0, failed=("a", "b")))
        result = execute_stop(restorer, api, ["report"], NOW)
        self.assertFalse(result.fully_stopped)

    def test_cronjob_failure_is_visible_in_suspend_phase(self):
        api = FakeCronJobApi()
        api.add(make_cronjob("report"))
        api.conflict_on_next_patches = 10
        restorer = RecordingRestorer(teardown=TargetReport.all_ok(["web"]))
        result = execute_stop(restorer, api, ["report"], NOW, max_attempts=2)
        suspend = result.phase(Phase.CRONJOB_SUSPEND)
        self.assertEqual(suspend.status, StepStatus.FAILED)
        self.assertFalse(result.success)
        # The teardown still happened; the CronJob failure does not block it.
        self.assertTrue(result.fully_stopped)


class StartOrderingTestCase(unittest.TestCase):
    def test_restore_precedes_resume_precedes_observation(self):
        log: list[str] = []
        api = FakeCronJobApi()
        api.add(owned())
        original_patch = api.patch_cron_job

        def logging_patch(name, patch):
            log.append("cronjob-patch")
            return original_patch(name, patch)

        api.patch_cron_job = logging_patch

        def observe():
            log.append("observe")
            return [HpaObservation("web-hpa", Observation.SCALING_ACTIVE)]

        restorer = RecordingRestorer(restore=TargetReport.all_ok(["web"]), log=log)
        result = execute_start(restorer, api, ["report"], observe)

        self.assertEqual(log, ["restore", "cronjob-patch", "observe"])
        self.assertTrue(result.control_restored)
        self.assertTrue(result.success)

    def test_a_failed_restore_blocks_resume_and_does_not_claim_control(self):
        api = FakeCronJobApi()
        api.add(owned())
        observed: list[int] = []
        restorer = RecordingRestorer(
            restore=TargetReport(total=2, succeeded=1, failed=("web",))
        )
        result = execute_start(restorer, api, ["report"], lambda: observed.append(1) or [])

        self.assertFalse(result.control_restored)
        self.assertEqual(
            result.phase(Phase.CRONJOB_RESUME).status, StepStatus.SKIPPED
        )
        self.assertEqual(result.phase(Phase.HPA_OBSERVATION).status, StepStatus.SKIPPED)
        # The CronJob is still suspended: control is not claimed.
        self.assertEqual(api.patches, [])
        self.assertTrue(api.raw("report")["spec"]["suspend"])
        self.assertEqual(observed, [])

    def test_partial_restore_skips_resume(self):
        api = FakeCronJobApi()
        api.add(owned())
        restorer = RecordingRestorer(restore=TargetReport(total=3, succeeded=2, failed=("c",)))
        result = execute_start(restorer, api, ["report"], lambda: [])
        self.assertFalse(result.control_restored)
        self.assertEqual(result.phase(Phase.CRONJOB_RESUME).status, StepStatus.SKIPPED)

    def test_resume_conflict_means_control_not_restored(self):
        api = FakeCronJobApi()
        api.add(owned(uid="uid-1"))
        api.conflict_on_next_patches = 10
        restorer = RecordingRestorer(restore=TargetReport.all_ok(["web"]))
        result = execute_start(restorer, api, ["report"], lambda: [], max_attempts=2)
        self.assertFalse(result.control_restored)
        self.assertEqual(result.phase(Phase.CRONJOB_RESUME).status, StepStatus.FAILED)
        self.assertTrue(api.raw("report")["spec"]["suspend"])

    def test_still_disabled_observation_is_informational(self):
        api = FakeCronJobApi()
        api.add(owned())
        restorer = RecordingRestorer(restore=TargetReport.all_ok(["web"]))

        def observe():
            return [HpaObservation("web-hpa", Observation.STILL_DISABLED, "controller catching up")]

        result = execute_start(restorer, api, ["report"], observe)
        # Observation is not a failure; control is still restored.
        self.assertTrue(result.control_restored)
        self.assertEqual(result.phase(Phase.HPA_OBSERVATION).status, StepStatus.COMPLETED)

    def test_unknown_observation_makes_phase_partial_but_does_not_revoke_restore(self):
        api = FakeCronJobApi()
        api.add(owned())
        restorer = RecordingRestorer(restore=TargetReport.all_ok(["web"]))
        result = execute_start(
            restorer, api, ["report"], lambda: [HpaObservation("web-hpa", Observation.UNKNOWN, "no condition")]
        )
        self.assertEqual(result.phase(Phase.HPA_OBSERVATION).status, StepStatus.PARTIAL)
        self.assertTrue(result.control_restored)
        self.assertFalse(result.success)  # an unknown observation still blocks a clean success

    def test_no_managed_work_is_a_clean_start(self):
        api = FakeCronJobApi()
        restorer = RecordingRestorer(restore=TargetReport.none())
        result = execute_start(restorer, api, [], lambda: [])
        self.assertTrue(result.control_restored)
        self.assertEqual(result.phase(Phase.CRONJOB_RESUME).status, StepStatus.COMPLETED)

    def test_summary_lists_every_phase(self):
        api = FakeCronJobApi()
        api.add(owned())
        restorer = RecordingRestorer(restore=TargetReport.all_ok(["web"]))
        result = execute_start(restorer, api, ["report"], lambda: [])
        summary = result.summary()
        self.assertIn("target_restore=COMPLETED", summary)
        self.assertIn("cronjob_resume=COMPLETED", summary)
        self.assertIn("hpa_observation=COMPLETED", summary)


class NoClusterTestCase(unittest.TestCase):
    def test_the_offline_path_never_imports_a_live_client(self):
        # The fake double stands in for BatchV1Api; if any module tried to
        # construct a real client the import at module scope would be visible.
        import dailyclean_ops.cronjob as cronjob_module

        self.assertFalse(hasattr(cronjob_module, "client"))


if __name__ == "__main__":
    unittest.main()
