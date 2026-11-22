"""Ordering and partial-failure contract for a stop/start hand-over.

DailyClean performs three kinds of action, in a fixed order, and never claims
that control has been restored until every prerequisite step has completed:

``stop``
    1. Suspend managed CronJobs (``spec.suspend: true``) so no *new* trigger
       fires during teardown.  Jobs already running are left alone.
    2. Scale the managed scale targets down to ``0`` (the replica *recording*
       itself lives in the replica-restore feature and is only invoked through
       :class:`TargetRestorer`; it is not reimplemented here).

``start``
    1. Restore the scale targets, using the HPA boundary clamp computed for
       each target (see :mod:`dailyclean_ops.hpa`).
    2. Only once restoration is complete, resume managed CronJobs.
    3. Observe the HPAs after restoration (read-only; never gates the claim by
       itself, but is reported so a caller can re-queue while the controller
       catches up).

The invariant enforced here is the one the caller must not break: if a step
does not fully complete, later dependent steps are **skipped** and the result
reports ``control_restored == False``.  A partially-restored namespace is
never reported as started.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Optional, Protocol, Sequence

from .cronjob import (
    DEFAULT_MAX_ATTEMPTS,
    CronJobClient,
    CronJobOutcome,
    apply_to_all,
)
from .hpa import HpaObservation, Observation


class Phase(str, Enum):
    CRONJOB_SUSPEND = "cronjob_suspend"
    CRONJOB_RESUME = "cronjob_resume"
    TARGET_RESTORE = "target_restore"
    TARGET_TEARDOWN = "target_teardown"
    HPA_OBSERVATION = "hpa_observation"


class StepStatus(str, Enum):
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class PhaseResult:
    phase: Phase
    status: StepStatus
    detail: str = ""
    objects: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.status is StepStatus.COMPLETED


@dataclass(frozen=True)
class HandoverResult:
    operation: str
    phases: tuple[PhaseResult, ...]
    cronjob_outcomes: tuple[CronJobOutcome, ...] = ()
    hpa_observations: tuple[HpaObservation, ...] = ()

    def phase(self, phase: Phase) -> Optional[PhaseResult]:
        for result in self.phases:
            if result.phase is phase:
                return result
        return None

    @property
    def success(self) -> bool:
        return all(p.complete for p in self.phases) and not self._has_bad_cronjob

    @property
    def _has_bad_cronjob(self) -> bool:
        return any(not outcome.ok for outcome in self.cronjob_outcomes)

    @property
    def control_restored(self) -> bool:
        """True only when every prerequisite step completed.

        For ``start`` this means the targets were restored *and* the managed
        CronJobs were resumed.  HPA observation is informational and does not
        by itself revoke a restoration, but a failed CronJob resume does.
        """

        if self.operation != "start":
            return False
        restore = self.phase(Phase.TARGET_RESTORE)
        resume = self.phase(Phase.CRONJOB_RESUME)
        return (
            restore is not None
            and restore.complete
            and resume is not None
            and resume.complete
            and not self._has_bad_cronjob
        )

    @property
    def fully_stopped(self) -> bool:
        if self.operation != "stop":
            return False
        teardown = self.phase(Phase.TARGET_TEARDOWN)
        return teardown is not None and teardown.complete

    def summary(self) -> str:
        parts = [f"{p.phase.value}={p.status.value}" for p in self.phases]
        return ", ".join(parts)


# ---------------------------------------------------------------------------
# Target restore / teardown abstraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetReport:
    """Outcome of restoring or scaling down the managed scale targets."""

    total: int
    succeeded: int
    failed: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @classmethod
    def none(cls) -> "TargetReport":
        return cls(total=0, succeeded=0)

    @classmethod
    def all_ok(cls, names: Iterable[str]) -> "TargetReport":
        names = tuple(names)
        return cls(total=len(names), succeeded=len(names))

    @property
    def complete(self) -> bool:
        return self.total == self.succeeded and not self.failed

    @property
    def status(self) -> StepStatus:
        if self.total == 0:
            return StepStatus.COMPLETED
        if self.complete:
            return StepStatus.COMPLETED
        if self.succeeded == 0:
            return StepStatus.FAILED
        return StepStatus.PARTIAL


class TargetRestorer(Protocol):
    """Restores / tears down replicas (owned by the replica-record feature)."""

    def restore(self) -> TargetReport:
        ...

    def teardown(self) -> TargetReport:
        ...


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def execute_stop(
    target_restorer: TargetRestorer,
    cronjob_client: CronJobClient,
    cronjob_names: Sequence[str],
    suspended_at: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    log: Optional[Callable[[str], None]] = None,
) -> HandoverResult:
    """Suspend managed CronJobs, then scale the targets down."""

    suspend_outcomes = apply_to_all(
        cronjob_client,
        cronjob_names,
        "stop",
        suspended_at,
        max_attempts,
        log,
    )
    suspend_phase = _phase_from_cronjobs(Phase.CRONJOB_SUSPEND, suspend_outcomes)

    # CronJob suspension is best-effort: a CronJob we could not suspend does
    # not block scaling the workloads down (the workloads are still stopped),
    # but it is reported.  The teardown result is what decides ``fully_stopped``.
    teardown = target_restorer.teardown()
    teardown_phase = PhaseResult(
        Phase.TARGET_TEARDOWN,
        teardown.status,
        detail=", ".join(teardown.notes) if teardown.notes else "",
        objects=tuple(teardown.failed),
    )

    return HandoverResult(
        operation="stop",
        phases=(suspend_phase, teardown_phase),
        cronjob_outcomes=tuple(suspend_outcomes),
    )


def execute_start(
    target_restorer: TargetRestorer,
    cronjob_client: CronJobClient,
    cronjob_names: Sequence[str],
    hpa_observer: Callable[[], Sequence[HpaObservation]],
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    log: Optional[Callable[[str], None]] = None,
) -> HandoverResult:
    """Restore targets, then resume CronJobs, then observe HPAs."""

    restore = target_restorer.restore()
    restore_phase = PhaseResult(
        Phase.TARGET_RESTORE,
        restore.status,
        detail=", ".join(restore.notes) if restore.notes else "",
        objects=tuple(restore.failed),
    )

    if not restore.complete:
        resume_phase = PhaseResult(
            Phase.CRONJOB_RESUME,
            StepStatus.SKIPPED,
            detail=(
                "target restore did not complete; CronJobs stay suspended and "
                "control is not claimed as restored"
            ),
        )
        observation_phase = PhaseResult(
            Phase.HPA_OBSERVATION,
            StepStatus.SKIPPED,
            detail="skipped because target restore did not complete",
        )
        return HandoverResult(
            operation="start",
            phases=(restore_phase, resume_phase, observation_phase),
        )

    resume_outcomes = apply_to_all(
        cronjob_client,
        cronjob_names,
        "start",
        0,  # unused for start
        max_attempts,
        log,
    )
    resume_phase = _phase_from_cronjobs(Phase.CRONJOB_RESUME, resume_outcomes)

    observations = tuple(hpa_observer())
    observation_phase = PhaseResult(
        Phase.HPA_OBSERVATION,
        _observation_status(observations),
        detail=", ".join(
            f"{obs.name}:{obs.observation.value}" for obs in observations if obs.name
        ),
        objects=tuple(obs.name for obs in observations if obs.name),
    )

    return HandoverResult(
        operation="start",
        phases=(restore_phase, resume_phase, observation_phase),
        cronjob_outcomes=tuple(resume_outcomes),
        hpa_observations=observations,
    )


def _phase_from_cronjobs(
    phase: Phase, outcomes: Sequence[CronJobOutcome]
) -> PhaseResult:
    bad = [o for o in outcomes if not o.ok]
    if not outcomes:
        return PhaseResult(phase, StepStatus.COMPLETED, "no managed CronJobs")
    if not bad:
        return PhaseResult(
            phase,
            StepStatus.COMPLETED,
            f"{len(outcomes)} managed CronJob(s) processed",
            objects=tuple(o.name for o in outcomes if o.name),
        )
    status = StepStatus.FAILED if len(bad) == len(outcomes) else StepStatus.PARTIAL
    return PhaseResult(
        phase,
        status,
        "; ".join(f"{o.name}:{o.action.value}:{o.detail}" for o in bad),
        objects=tuple(o.name for o in bad if o.name),
    )


def _observation_status(observations: Sequence[HpaObservation]) -> StepStatus:
    # Observation is informational.  A still-disabled HPA is expected right
    # after a restore (the controller has not caught up yet); it does not fail
    # the phase, it only means the caller may want to re-queue an observation.
    if not observations:
        return StepStatus.COMPLETED
    unknown = [o for o in observations if o.observation is Observation.UNKNOWN]
    if unknown:
        return StepStatus.PARTIAL
    return StepStatus.COMPLETED


__all__ = [
    "HandoverResult",
    "Phase",
    "PhaseResult",
    "StepStatus",
    "TargetReport",
    "TargetRestorer",
    "execute_start",
    "execute_stop",
]
