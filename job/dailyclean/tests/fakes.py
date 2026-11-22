"""In-memory doubles for the DailyClean offline tests.

Nothing here opens a socket.  The ``FakeCronJobApi`` emulates just enough of
``BatchV1Api`` (read / list / strategic-merge patch with a resourceVersion
precondition) so the tests can assert the exact request bodies the CLI would
send.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Optional


class FakeConflict(Exception):
    """Stands in for a 409 optimistic-concurrency failure."""

    status = 409

    def __init__(self, message: str = "the object has been modified"):
        super().__init__(message)


class FakeApiException(Exception):
    status = 500


def _deep_merge(target: dict, patch: dict) -> None:
    """Strategic-merge subset: ``None`` deletes, dicts merge, scalars replace."""

    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict):
            current = target.get(key)
            if not isinstance(current, dict):
                current = {}
                target[key] = current
            _deep_merge(current, value)
        else:
            target[key] = copy.deepcopy(value)


@dataclass
class RecordedPatch:
    name: str
    body: dict

    def body_json(self) -> str:
        return json.dumps(self.body, sort_keys=True)


class FakeCronJobApi:
    """Records every write so tests can assert only agreed fields change."""

    def __init__(self) -> None:
        self._objects: dict[str, dict] = {}
        self.patches: list[RecordedPatch] = []
        self.gets: list[str] = []
        self.lists: int = 0
        #: When set, the next patches fail with a conflict.
        self.conflict_on_next_patches: int = 0
        #: When set, bump the resourceVersion between read and patch once.
        self.bump_on_next_patch: bool = False

    # -- seeding -----------------------------------------------------------
    def add(self, obj: dict) -> None:
        self._objects[obj["metadata"]["name"]] = copy.deepcopy(obj)

    def get(self, name: str) -> Optional[dict]:
        obj = self._objects.get(name)
        return copy.deepcopy(obj) if obj is not None else None

    def raw(self, name: str) -> Optional[dict]:
        obj = self._objects.get(name)
        return copy.deepcopy(obj) if obj is not None else None

    # -- CronJobClient protocol -------------------------------------------
    def list_cron_jobs(self) -> list[dict]:
        self.lists += 1
        return [copy.deepcopy(o) for o in self._objects.values()]

    def get_cron_job(self, name: str) -> Optional[dict]:
        self.gets.append(name)
        return self.get(name)

    def patch_cron_job(self, name: str, patch: dict) -> dict:
        if self.conflict_on_next_patches > 0:
            self.conflict_on_next_patches -= 1
            # Simulate a concurrent writer bumping the version.
            self._objects[name]["metadata"]["resourceVersion"] = str(
                int(self._objects[name]["metadata"].get("resourceVersion", "1")) + 1
            )
            raise FakeConflict("simulated concurrent modification")

        current = self._objects.get(name)
        if current is None:
            raise FakeApiException("not found")

        expected = patch.get("metadata", {}).get("resourceVersion")
        actual = current["metadata"].get("resourceVersion")
        if expected is not None and expected != actual:
            raise FakeConflict(
                f"resourceVersion {expected!r} != current {actual!r}"
            )

        self.patches.append(RecordedPatch(name, copy.deepcopy(patch)))
        _deep_merge(current, copy.deepcopy(patch))
        if self.bump_on_next_patch:
            self.bump_on_next_patch = False
            current["metadata"]["resourceVersion"] = str(int(actual) + 1)
        return copy.deepcopy(current)


class FakeJobApi:
    """Fails loudly if anything ever touches a Job while suspending a CronJob."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def _fail(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("call", (args, kwargs)))
        raise AssertionError("DailyClean must not touch Job objects")

    delete_namespaced_job = _fail
    patch_namespaced_job = _fail
    create_namespaced_job = _fail
    read_namespaced_job = _fail
    list_namespaced_job = _fail


# ---------------------------------------------------------------------------
# Object builders (plain dicts / attribute objects in the kubernetes shape)
# ---------------------------------------------------------------------------


def make_cronjob(
    name: str = "nightly-report",
    *,
    uid: str = "uid-1",
    resource_version: str = "10",
    suspend: bool = False,
    dailyclean: Optional[str] = "true",
    annotations: Optional[dict] = None,
    schedule: str = "0 2 * * *",
    concurrency_policy: str = "Forbid",
    starting_deadline_seconds: Optional[int] = None,
    successful_jobs_history_limit: int = 3,
    failed_jobs_history_limit: int = 1,
    time_zone: str = "Europe/Paris",
    extra_labels: Optional[dict] = None,
) -> dict:
    labels = {"app": "report"}
    if dailyclean is not None:
        labels["axa.com/dailyclean"] = dailyclean
    if extra_labels:
        labels.update(extra_labels)

    spec: dict[str, Any] = {
        "schedule": schedule,
        "concurrencyPolicy": concurrency_policy,
        "successfulJobsHistoryLimit": successful_jobs_history_limit,
        "failedJobsHistoryLimit": failed_jobs_history_limit,
        "suspend": suspend,
        "timeZone": time_zone,
        "jobTemplate": {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "report", "image": "report:1"}],
                        "restartPolicy": "Never",
                    }
                }
            }
        },
    }
    if starting_deadline_seconds is not None:
        spec["startingDeadlineSeconds"] = starting_deadline_seconds

    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {
            "name": name,
            "namespace": "test",
            "uid": uid,
            "resourceVersion": resource_version,
            "labels": labels,
            "annotations": annotations or {},
        },
        "spec": spec,
    }
