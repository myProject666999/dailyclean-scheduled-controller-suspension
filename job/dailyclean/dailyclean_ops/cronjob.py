"""Managed CronJob suspension hand-over.

DailyClean never deletes or edits a user CronJob's schedule.  When a CronJob is
explicitly opted in (``axa.com/dailyclean: "true"``) ``stop`` records a
suspension it owns and sets ``spec.suspend: true``; ``start`` resumes only the
same object (same ``metadata.uid``) while the record still proves ownership.

Kubernetes semantics that this module relies on and deliberately preserves:

* ``spec.suspend: true`` only stops *future* triggers.  Jobs already running
  are not terminated by DailyClean, and this module never deletes jobs.
* On resume, the CronJob controller may create a Job for a schedule it missed
  while suspended, subject to ``startingDeadlineSeconds``,
  ``concurrencyPolicy`` and ``successfulJobsHistoryLimit`` /
  ``failedJobsHistoryLimit``.  DailyClean leaves all of those fields untouched
  and never forces a catch-up run, prunes history or rewrites the schedule.
  Re-creating a missed trigger is standard Kubernetes behaviour and must be
  confirmed by the operator before resuming.
* Writes are strategic-merge patches carrying ``metadata.resourceVersion`` as
  an optimistic-concurrency precondition.  A concurrent manual change makes
  the patch fail with HTTP 409; the object is then re-read and re-evaluated
  from scratch instead of replaying the old decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Sequence

from .annotations import (
    get_field,
    is_opted_in,
    name_of,
    read_suspension_record,
    resource_version_of,
    suspension_annotation_clear_patch,
    suspension_annotation_patch,
    uid_of,
)


class CronJobAction(str, Enum):
    SUSPENDED = "SUSPENDED"
    ALREADY_MANAGED = "ALREADY_MANAGED"
    ALREADY_SUSPENDED_EXTERNALLY = "ALREADY_SUSPENDED_EXTERNALLY"
    SKIPPED_NOT_OPTED_IN = "SKIPPED_NOT_OPTED_IN"
    SKIPPED_NOT_SUSPENDED = "SKIPPED_NOT_SUSPENDED"
    SKIPPED_NOT_OWNED = "SKIPPED_NOT_OWNED"
    SKIPPED_STALE_UID = "SKIPPED_STALE_UID"
    RESUMED = "RESUMED"
    STALE_RECORD_CLEARED = "STALE_RECORD_CLEARED"
    SKIPPED_MISSING_PRECONDITION = "SKIPPED_MISSING_PRECONDITION"
    SKIPPED_MISSING_UID = "SKIPPED_MISSING_UID"
    CONFLICT = "CONFLICT"
    MISSING = "MISSING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class CronJobOutcome:
    name: Optional[str]
    action: CronJobAction
    detail: str = ""
    writes: int = 0

    @property
    def ok(self) -> bool:
        return self.action not in (
            CronJobAction.CONFLICT,
            CronJobAction.ERROR,
            CronJobAction.SKIPPED_MISSING_PRECONDITION,
            CronJobAction.SKIPPED_MISSING_UID,
        )


class CronJobClient(Protocol):
    """The subset of the Kubernetes batch API used by this module."""

    def list_cron_jobs(self) -> Sequence[Any]:
        ...

    def get_cron_job(self, name: str) -> Optional[Any]:
        ...

    def patch_cron_job(self, name: str, patch: Mapping[str, Any]) -> Any:
        ...


def _spec_of(obj: Any) -> Any:
    return get_field(obj, "spec")


def _is_suspended(obj: Any) -> bool:
    return bool(get_field(_spec_of(obj), "suspend", False))


def is_conflict_error(error: BaseException) -> bool:
    """Recognise an optimistic-concurrency failure from a real or fake client."""

    status = getattr(error, "status", None)
    if status == 409:
        return True
    if str(status) == "409":
        return True
    name = type(error).__name__.lower()
    return "conflict" in name


# ---------------------------------------------------------------------------
# Patch construction
# ---------------------------------------------------------------------------


def build_suspend_patch(obj: Any, suspended_at: str) -> Optional[dict[str, Any]]:
    """Build the minimal patch that suspends ``obj`` and records ownership."""

    resource_version = resource_version_of(obj)
    uid = uid_of(obj)
    if not resource_version:
        return None
    if not uid:
        return None
    return {
        "metadata": {
            "resourceVersion": resource_version,
            "annotations": suspension_annotation_patch(uid, suspended_at),
        },
        "spec": {"suspend": True},
    }


def build_resume_patch(obj: Any) -> Optional[dict[str, Any]]:
    """Build the minimal patch that lifts *our* suspension of ``obj``."""

    resource_version = resource_version_of(obj)
    if not resource_version:
        return None
    return {
        "metadata": {
            "resourceVersion": resource_version,
            "annotations": suspension_annotation_clear_patch(),
        },
        "spec": {"suspend": False},
    }


def build_clear_stale_record_patch(obj: Any) -> Optional[dict[str, Any]]:
    """Remove a stale ownership record without touching ``spec``."""

    resource_version = resource_version_of(obj)
    if not resource_version:
        return None
    return {
        "metadata": {
            "resourceVersion": resource_version,
            "annotations": suspension_annotation_clear_patch(),
        }
    }


# ---------------------------------------------------------------------------
# Decisions (pure, no I/O)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StopDecision:
    action: CronJobAction
    detail: str = ""

    @property
    def should_write(self) -> bool:
        return self.action is CronJobAction.SUSPENDED


def decide_stop(obj: Any, suspended_at: str) -> StopDecision:
    if not is_opted_in(obj):
        return StopDecision(
            CronJobAction.SKIPPED_NOT_OPTED_IN,
            "CronJob is not opted in via axa.com/dailyclean=true",
        )
    if not _is_suspended(obj):
        if not uid_of(obj):
            return StopDecision(
                CronJobAction.SKIPPED_MISSING_UID,
                "CronJob has no metadata.uid; ownership cannot be recorded",
            )
        if not resource_version_of(obj):
            return StopDecision(
                CronJobAction.SKIPPED_MISSING_PRECONDITION,
                "CronJob has no metadata.resourceVersion; refusing an unguarded write",
            )
        return StopDecision(CronJobAction.SUSPENDED, "suspension taken by dailyclean")
    record = read_suspension_record(obj)
    if (
        record is not None
        and record.is_owned_by_dailyclean
        and record.matches_uid(uid_of(obj))
    ):
        return StopDecision(
            CronJobAction.ALREADY_MANAGED,
            "already suspended by dailyclean; no write",
        )
    return StopDecision(
        CronJobAction.ALREADY_SUSPENDED_EXTERNALLY,
        "already suspended by someone else; ownership not taken",
    )


@dataclass(frozen=True)
class StartDecision:
    action: CronJobAction
    detail: str = ""

    @property
    def should_write(self) -> bool:
        return self.action in (
            CronJobAction.RESUMED,
            CronJobAction.STALE_RECORD_CLEARED,
        )


def decide_start(obj: Any) -> StartDecision:
    if not is_opted_in(obj):
        return StartDecision(
            CronJobAction.SKIPPED_NOT_OPTED_IN,
            "CronJob is not opted in via axa.com/dailyclean=true",
        )
    record = read_suspension_record(obj)
    suspended = _is_suspended(obj)
    if suspended:
        if record is None or not record.is_owned_by_dailyclean:
            return StartDecision(
                CronJobAction.SKIPPED_NOT_OWNED,
                "CronJob is suspended but not by dailyclean; leaving it untouched",
            )
        if not record.matches_uid(uid_of(obj)):
            return StartDecision(
                CronJobAction.SKIPPED_STALE_UID,
                "ownership record belongs to another metadata.uid; not resuming",
            )
        if not resource_version_of(obj):
            return StartDecision(
                CronJobAction.SKIPPED_MISSING_PRECONDITION,
                "CronJob has no metadata.resourceVersion; refusing an unguarded write",
            )
        return StartDecision(
            CronJobAction.RESUMED,
            "resuming a suspension still owned by dailyclean",
        )
    if record is not None and record.is_owned_by_dailyclean:
        if not resource_version_of(obj):
            return StartDecision(
                CronJobAction.SKIPPED_MISSING_PRECONDITION,
                "stale record found but no metadata.resourceVersion to guard its removal",
            )
        return StartDecision(
            CronJobAction.STALE_RECORD_CLEARED,
            "CronJob is no longer suspended by us; clearing the stale ownership record",
        )
    return StartDecision(
        CronJobAction.SKIPPED_NOT_SUSPENDED,
        "CronJob is not suspended; nothing to resume",
    )


# ---------------------------------------------------------------------------
# Apply with bounded re-read on conflict
# ---------------------------------------------------------------------------

DEFAULT_MAX_ATTEMPTS = 3


def apply_stop(
    client: CronJobClient,
    name: str,
    suspended_at: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    log: Optional[Callable[[str], None]] = None,
) -> CronJobOutcome:
    return _apply(
        client=client,
        name=name,
        decide=lambda obj: decide_stop(obj, suspended_at),
        build_patch=lambda obj, decision: build_suspend_patch(obj, suspended_at)
        if decision.should_write
        else None,
        max_attempts=max_attempts,
        log=log,
    )


def apply_start(
    client: CronJobClient,
    name: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    log: Optional[Callable[[str], None]] = None,
) -> CronJobOutcome:
    def build(obj: Any, decision: StartDecision) -> Optional[dict[str, Any]]:
        if decision.action is CronJobAction.RESUMED:
            return build_resume_patch(obj)
        if decision.action is CronJobAction.STALE_RECORD_CLEARED:
            return build_clear_stale_record_patch(obj)
        return None

    return _apply(
        client=client,
        name=name,
        decide=decide_start,
        build_patch=build,
        max_attempts=max_attempts,
        log=log,
    )


def _apply(
    client: CronJobClient,
    name: str,
    decide: Callable[[Any], Any],
    build_patch: Callable[[Any, Any], Optional[Mapping[str, Any]]],
    max_attempts: int,
    log: Optional[Callable[[str], None]],
) -> CronJobOutcome:
    attempts = max(1, max_attempts)
    last: Optional[CronJobOutcome] = None
    for attempt in range(1, attempts + 1):
        obj = client.get_cron_job(name)
        if obj is None:
            return CronJobOutcome(name, CronJobAction.MISSING, "CronJob not found")
        decision = decide(obj)
        if not decision.should_write:
            return CronJobOutcome(name, decision.action, decision.detail, writes=0)
        patch = build_patch(obj, decision)
        if patch is None:
            return CronJobOutcome(
                name,
                CronJobAction.SKIPPED_MISSING_PRECONDITION,
                "cannot build a guarded patch",
            )
        try:
            client.patch_cron_job(name, patch)
            return CronJobOutcome(name, decision.action, decision.detail, writes=1)
        except BaseException as error:  # noqa: BLE001 - re-raised as an outcome
            if not is_conflict_error(error):
                return CronJobOutcome(
                    name, CronJobAction.ERROR, f"patch failed: {error}"
                )
            last = CronJobOutcome(
                name,
                CronJobAction.CONFLICT,
                f"resourceVersion precondition failed on attempt {attempt}",
            )
            if log is not None:
                log(
                    f"Conflict while patching CronJob {name} "
                    f"(attempt {attempt}/{attempts}); re-reading"
                )
    assert last is not None
    return last


def apply_to_all(
    client: CronJobClient,
    names: Iterable[str],
    operation: str,
    suspended_at: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    log: Optional[Callable[[str], None]] = None,
) -> list[CronJobOutcome]:
    outcomes: list[CronJobOutcome] = []
    for name in names:
        if operation == "stop":
            outcomes.append(
                apply_stop(client, name, suspended_at, max_attempts, log)
            )
        elif operation == "start":
            outcomes.append(apply_start(client, name, max_attempts, log))
        else:  # pragma: no cover - defensive
            raise ValueError(f"unknown operation {operation!r}")
    return outcomes


#: CronJobs created by DailyClean itself. They must never be treated as user
#: workloads: suspending one would disable DailyClean's own scheduler.
CONTROL_CRONJOB_NAMES = frozenset({"dailyclean-start", "dailyclean-stop"})


def is_control_cronjob(obj: Any) -> bool:
    return name_of(obj) in CONTROL_CRONJOB_NAMES


def opted_in_names(objects: Iterable[Any]) -> list[str]:
    names = [
        name_of(obj)
        for obj in objects
        if is_opted_in(obj) and not is_control_cronjob(obj)
    ]
    return [name for name in names if name]


__all__ = [
    "CronJobAction",
    "CronJobClient",
    "CronJobOutcome",
    "StartDecision",
    "StopDecision",
    "apply_start",
    "apply_stop",
    "apply_to_all",
    "build_clear_stale_record_patch",
    "build_resume_patch",
    "build_suspend_patch",
    "decide_start",
    "decide_stop",
    "is_conflict_error",
    "opted_in_names",
]
