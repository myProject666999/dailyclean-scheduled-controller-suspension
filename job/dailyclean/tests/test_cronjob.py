from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dailyclean_ops.annotations import (  # noqa: E402
    MANAGED_BY_ANNOTATION,
    OWNER_VALUE,
    SUSPENDED_AT_ANNOTATION,
    SUSPENDED_UID_ANNOTATION,
)
from dailyclean_ops.cronjob import (  # noqa: E402
    CronJobAction,
    apply_start,
    apply_stop,
    build_resume_patch,
    build_suspend_patch,
    decide_start,
    decide_stop,
    opted_in_names,
)
from tests.fakes import FakeCronJobApi, FakeJobApi, make_cronjob  # noqa: E402

NOW = "2026-11-23T01:00:00Z"


def owned_annotations(uid: str, at: str = NOW) -> dict:
    return {
        MANAGED_BY_ANNOTATION: OWNER_VALUE,
        SUSPENDED_UID_ANNOTATION: uid,
        SUSPENDED_AT_ANNOTATION: at,
    }


class StopTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeCronJobApi()

    def test_suspends_an_unparticipating_cronjob(self):
        # Not opted in -> never touched, even if previously running.
        self.api.add(make_cronjob("plain", dailyclean=None))
        outcome = apply_stop(self.api, "plain", NOW)
        self.assertIs(outcome.action, CronJobAction.SKIPPED_NOT_OPTED_IN)
        self.assertEqual(outcome.writes, 0)
        self.assertEqual(self.api.patches, [])
        self.assertFalse(self.api.raw("plain")["spec"]["suspend"])

    def test_label_false_is_not_participating(self):
        self.api.add(make_cronjob("disabled", dailyclean="false"))
        outcome = apply_stop(self.api, "disabled", NOW)
        self.assertIs(outcome.action, CronJobAction.SKIPPED_NOT_OPTED_IN)
        self.assertEqual(self.api.patches, [])

    def test_suspends_and_records_ownership_for_an_opted_in_cronjob(self):
        self.api.add(make_cronjob("report", uid="uid-a", resource_version="7"))
        outcome = apply_stop(self.api, "report", NOW)

        self.assertIs(outcome.action, CronJobAction.SUSPENDED)
        self.assertEqual(outcome.writes, 1)
        self.assertEqual(len(self.api.patches), 1)

        patch = self.api.patches[0].body
        self.assertEqual(
            patch,
            {
                "metadata": {
                    "resourceVersion": "7",
                    "annotations": {
                        MANAGED_BY_ANNOTATION: OWNER_VALUE,
                        SUSPENDED_UID_ANNOTATION: "uid-a",
                        SUSPENDED_AT_ANNOTATION: NOW,
                    },
                },
                "spec": {"suspend": True},
            },
        )

    def test_stop_patch_only_touches_the_agreed_fields(self):
        self.api.add(
            make_cronjob(
                "report",
                schedule="*/5 * * * *",
                concurrency_policy="Allow",
                starting_deadline_seconds=120,
                successful_jobs_history_limit=9,
                failed_jobs_history_limit=4,
            )
        )
        apply_stop(self.api, "report", NOW)
        patch = self.api.patches[0].body

        self.assertEqual(set(patch), {"metadata", "spec"})
        self.assertEqual(set(patch["spec"]), {"suspend"})
        self.assertEqual(
            set(patch["metadata"]), {"resourceVersion", "annotations"}
        )

        current = self.api.raw("report")
        # Untouched scheduling / concurrency semantics.
        self.assertEqual(current["spec"]["schedule"], "*/5 * * * *")
        self.assertEqual(current["spec"]["concurrencyPolicy"], "Allow")
        self.assertEqual(current["spec"]["startingDeadlineSeconds"], 120)
        self.assertEqual(current["spec"]["successfulJobsHistoryLimit"], 9)
        self.assertEqual(current["spec"]["failedJobsHistoryLimit"], 4)
        # Other user annotations and labels survive the merge patch.
        self.assertEqual(current["metadata"]["labels"]["app"], "report")

    def test_preserves_user_annotations_during_merge(self):
        self.api.add(
            make_cronjob(
                "report",
                annotations={"team/owner": "data", "cost-center": "42"},
            )
        )
        apply_stop(self.api, "report", NOW)
        annotations = self.api.raw("report")["metadata"]["annotations"]
        self.assertEqual(annotations["team/owner"], "data")
        self.assertEqual(annotations["cost-center"], "42")
        self.assertIn(MANAGED_BY_ANNOTATION, annotations)

    def test_does_not_terminate_a_running_job(self):
        jobs = FakeJobApi()
        self.api.add(make_cronjob("report"))
        apply_stop(self.api, "report", NOW)
        # The stop path never received a Job API, and suspending a CronJob
        # never deletes the active Job list.
        self.assertEqual(jobs.calls, [])
        self.assertEqual(len(self.api.patches), 1)

    def test_already_suspended_by_someone_else_is_left_untouched(self):
        self.api.add(make_cronjob("paused", suspend=True))
        outcome = apply_stop(self.api, "paused", NOW)

        self.assertIs(outcome.action, CronJobAction.ALREADY_SUSPENDED_EXTERNALLY)
        self.assertEqual(outcome.writes, 0)
        self.assertEqual(self.api.patches, [])
        # No ownership record -> no recovery right.
        annotations = self.api.raw("paused")["metadata"]["annotations"]
        self.assertNotIn(MANAGED_BY_ANNOTATION, annotations)

    def test_stop_is_idempotent_when_already_managed_by_us(self):
        self.api.add(
            make_cronjob("report", suspend=True, annotations=owned_annotations("uid-1"))
        )
        outcome = apply_stop(self.api, "report", NOW)
        self.assertIs(outcome.action, CronJobAction.ALREADY_MANAGED)
        self.assertEqual(outcome.writes, 0)
        self.assertEqual(self.api.patches, [])

    def test_second_stop_after_our_first_stop_does_not_rewrite(self):
        self.api.add(make_cronjob("report", uid="uid-1"))
        first = apply_stop(self.api, "report", NOW)
        self.assertIs(first.action, CronJobAction.SUSPENDED)

        second = apply_stop(self.api, "report", NOW)
        self.assertIs(second.action, CronJobAction.ALREADY_MANAGED)
        self.assertEqual(len(self.api.patches), 1)

    def test_already_suspended_with_stale_uid_is_not_reclaimed(self):
        # Suspended, but the record names a previous object.
        self.api.add(
            make_cronjob(
                "report",
                uid="uid-new",
                suspend=True,
                annotations=owned_annotations("uid-old"),
            )
        )
        outcome = apply_stop(self.api, "report", NOW)
        self.assertIs(outcome.action, CronJobAction.ALREADY_SUSPENDED_EXTERNALLY)
        self.assertEqual(self.api.patches, [])
        annotations = self.api.raw("report")["metadata"]["annotations"]
        self.assertEqual(annotations[SUSPENDED_UID_ANNOTATION], "uid-old")

    def test_missing_uid_blocks_the_stop(self):
        obj = make_cronjob("report")
        obj["metadata"].pop("uid")
        self.api.add(obj)
        outcome = apply_stop(self.api, "report", NOW)
        self.assertIs(outcome.action, CronJobAction.SKIPPED_MISSING_UID)
        self.assertFalse(outcome.ok)
        self.assertEqual(self.api.patches, [])

    def test_missing_resource_version_blocks_the_unguarded_write(self):
        obj = make_cronjob("report")
        obj["metadata"].pop("resourceVersion")
        self.api.add(obj)
        outcome = apply_stop(self.api, "report", NOW)
        self.assertIs(outcome.action, CronJobAction.SKIPPED_MISSING_PRECONDITION)
        self.assertFalse(outcome.ok)
        self.assertEqual(self.api.patches, [])

    def test_missing_cronjob_is_reported(self):
        outcome = apply_stop(self.api, "ghost", NOW)
        self.assertIs(outcome.action, CronJobAction.MISSING)

    def test_concurrent_manual_modification_causes_reread_not_replay(self):
        self.api.add(make_cronjob("report", resource_version="1"))
        # Simulate a user unsuspending (or editing) between our read and write.
        self.api.conflict_on_next_patches = 1

        outcome = apply_stop(self.api, "report", NOW)
        self.assertIs(outcome.action, CronJobAction.SUSPENDED)
        # Two reads: the original and the re-read after the conflict.
        self.assertEqual(self.api.gets, ["report", "report"])
        self.assertEqual(len(self.api.patches), 1)
        # The applied patch carries the *fresh* resourceVersion, not the old one.
        self.assertEqual(self.api.patches[0].body["metadata"]["resourceVersion"], "2")

    def test_conflict_exhaustion_does_not_claim_success(self):
        self.api.add(make_cronjob("report"))
        self.api.conflict_on_next_patches = 3
        outcome = apply_stop(self.api, "report", NOW, max_attempts=3)
        self.assertIs(outcome.action, CronJobAction.CONFLICT)
        self.assertFalse(outcome.ok)
        self.assertEqual(self.api.patches, [])

    def test_unexpected_api_error_is_reported(self):
        outcome = apply_stop(self.api, "missing-but-listed", NOW)
        self.assertIs(outcome.action, CronJobAction.MISSING)

    def test_opted_in_names_uses_authoritative_label(self):
        objs = [
            make_cronjob("a", dailyclean="true"),
            make_cronjob("b", dailyclean="false"),
            make_cronjob("c", dailyclean=None),
            make_cronjob("d", dailyclean=" TRUE "),
        ]
        self.assertEqual(opted_in_names(objs), ["a", "d"])

    def test_opted_in_names_excludes_dailyclean_control_cronjobs(self):
        objs = [
            make_cronjob("dailyclean-start", dailyclean="true"),
            make_cronjob("dailyclean-stop", dailyclean="true"),
            make_cronjob("report", dailyclean="true"),
        ]
        self.assertEqual(opted_in_names(objs), ["report"])


class StartTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeCronJobApi()

    def test_resumes_a_suspension_we_own(self):
        self.api.add(
            make_cronjob(
                "report",
                uid="uid-1",
                suspend=True,
                resource_version="5",
                annotations=owned_annotations("uid-1"),
            )
        )
        outcome = apply_start(self.api, "report")
        self.assertIs(outcome.action, CronJobAction.RESUMED)
        self.assertEqual(outcome.writes, 1)

        patch = self.api.patches[0].body
        self.assertEqual(
            patch,
            {
                "metadata": {
                    "resourceVersion": "5",
                    "annotations": {
                        MANAGED_BY_ANNOTATION: None,
                        SUSPENDED_UID_ANNOTATION: None,
                        SUSPENDED_AT_ANNOTATION: None,
                    },
                },
                "spec": {"suspend": False},
            },
        )
        current = self.api.raw("report")
        self.assertFalse(current["spec"]["suspend"])
        self.assertNotIn(MANAGED_BY_ANNOTATION, current["metadata"]["annotations"])

    def test_start_leaves_a_foreign_suspension_alone(self):
        self.api.add(make_cronjob("paused", suspend=True))
        outcome = apply_start(self.api, "paused")
        self.assertIs(outcome.action, CronJobAction.SKIPPED_NOT_OWNED)
        self.assertEqual(self.api.patches, [])
        self.assertTrue(self.api.raw("paused")["spec"]["suspend"])

    def test_start_ignores_a_record_for_a_recreated_object(self):
        # The user deleted and recreated the CronJob, so the UID changed.
        self.api.add(
            make_cronjob(
                "report",
                uid="uid-reborn",
                suspend=True,
                annotations=owned_annotations("uid-original"),
            )
        )
        outcome = apply_start(self.api, "report")
        self.assertIs(outcome.action, CronJobAction.SKIPPED_STALE_UID)
        self.assertEqual(self.api.patches, [])
        self.assertTrue(self.api.raw("report")["spec"]["suspend"])

    def test_start_does_not_override_manual_unsuspend(self):
        # Our record is present but a user already unsuspended the CronJob.
        self.api.add(
            make_cronjob(
                "report",
                uid="uid-1",
                suspend=False,
                annotations=owned_annotations("uid-1"),
            )
        )
        outcome = apply_start(self.api, "report")
        self.assertIs(outcome.action, CronJobAction.STALE_RECORD_CLEARED)
        self.assertEqual(outcome.writes, 1)
        patch = self.api.patches[0].body
        self.assertNotIn("spec", patch)
        self.assertEqual(
            set(patch["metadata"]["annotations"]),
            {MANAGED_BY_ANNOTATION, SUSPENDED_UID_ANNOTATION, SUSPENDED_AT_ANNOTATION},
        )
        current = self.api.raw("report")
        self.assertFalse(current["spec"]["suspend"])
        self.assertNotIn(MANAGED_BY_ANNOTATION, current["metadata"]["annotations"])

    def test_start_without_suspension_or_record_is_a_no_op(self):
        self.api.add(make_cronjob("running", suspend=False))
        outcome = apply_start(self.api, "running")
        self.assertIs(outcome.action, CronJobAction.SKIPPED_NOT_SUSPENDED)
        self.assertEqual(self.api.patches, [])

    def test_repeated_start_resumes_once_then_is_a_no_op(self):
        self.api.add(
            make_cronjob(
                "report",
                suspend=True,
                annotations=owned_annotations("uid-1"),
            )
        )
        first = apply_start(self.api, "report")
        self.assertIs(first.action, CronJobAction.RESUMED)
        second = apply_start(self.api, "report")
        self.assertIs(second.action, CronJobAction.SKIPPED_NOT_SUSPENDED)
        self.assertEqual(len(self.api.patches), 1)

    def test_start_does_not_claim_control_when_patch_conflicts(self):
        self.api.add(
            make_cronjob(
                "report",
                suspend=True,
                resource_version="1",
                annotations=owned_annotations("uid-1"),
            )
        )
        self.api.conflict_on_next_patches = 5
        outcome = apply_start(self.api, "report", max_attempts=2)
        self.assertIs(outcome.action, CronJobAction.CONFLICT)
        self.assertFalse(outcome.ok)
        self.assertTrue(self.api.raw("report")["spec"]["suspend"])

    def test_resume_patch_is_minimal(self):
        obj = make_cronjob("report", suspend=True, annotations=owned_annotations("uid-1"))
        patch = build_resume_patch(obj)
        self.assertEqual(set(patch), {"metadata", "spec"})
        self.assertEqual(patch["spec"], {"suspend": False})
        self.assertNotIn("schedule", patch["spec"])
        self.assertNotIn("concurrencyPolicy", patch["spec"])

    def test_suspend_patch_requires_uid_and_resource_version(self):
        obj = make_cronjob("report")
        obj["metadata"].pop("uid")
        self.assertIsNone(build_suspend_patch(obj, NOW))
        obj = make_cronjob("report")
        obj["metadata"].pop("resourceVersion")
        self.assertIsNone(build_suspend_patch(obj, NOW))


class FullCycleTestCase(unittest.TestCase):
    def test_stop_then_start_round_trips(self):
        self.api = FakeCronJobApi()
        self.api.add(make_cronjob("report", uid="uid-1"))
        self.assertIs(apply_stop(self.api, "report", NOW).action, CronJobAction.SUSPENDED)
        self.assertIs(apply_stop(self.api, "report", NOW).action, CronJobAction.ALREADY_MANAGED)
        self.assertIs(apply_start(self.api, "report").action, CronJobAction.RESUMED)
        self.assertIs(apply_start(self.api, "report").action, CronJobAction.SKIPPED_NOT_SUSPENDED)

        current = self.api.raw("report")
        self.assertFalse(current["spec"]["suspend"])
        self.assertEqual(current["metadata"]["annotations"], {})
        self.assertEqual(current["spec"]["schedule"], "0 2 * * *")
        self.assertEqual(current["spec"]["concurrencyPolicy"], "Forbid")
        self.assertEqual(len(self.api.patches), 2)

    def test_decision_helpers_are_pure(self):
        obj = make_cronjob("report")
        before = repr(obj)
        decide_stop(obj, NOW)
        decide_start(obj)
        self.assertEqual(repr(obj), before)


if __name__ == "__main__":
    unittest.main()
