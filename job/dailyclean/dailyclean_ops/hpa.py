"""HPA control boundary around restoring a scale target.

HorizontalPodAutoscalers have **no** generic ``suspend`` field, so DailyClean
never fakes one, never deletes/recreates an HPA and never relies on every
cluster accepting ``minReplicas: 0``.

The only convention used here is the one implemented by the upstream
Kubernetes HPA controller (``reconcileAutoscaler``): when the scale target's
current ``spec.replicas`` is ``0`` and the HPA's ``minReplicas`` is non-zero,
the controller treats autoscaling as disabled for that target
(``ScalingActive=False`` / reason ``ScalingDisabled``) and keeps
``desiredReplicas`` at ``0``.  Scaling a Deployment/StatefulSet to zero is
therefore a native hand-over of the scaling decision to *nobody*, not to
DailyClean.  This module only *reads* the HPA to check that the convention
holds before the target is scaled back up; it never writes an HPA.

Restoration is a clamp into the HPA's *current* ``[minReplicas, maxReplicas]``:

    min=3 max=10 candidate=7  -> 7
    min=3 max=10 candidate=20 -> 10
    min=3 max=10 no candidate -> 3      (max(minReplicas, 1))

Ownership is decided from ``scaleTargetRef`` (group / kind / name), never from
labels.  If more than one HPA points at the same target, that is reported as a
conflict: intersecting their ranges is *not* a promise that two controllers
can safely share one target, so no silent takeover happens.

Limits of the promise: even with the convention holding, another actor
(HPA with ``minReplicas: 0``, KEDA, a GitOps reconciler, an operator) can
raise the replicas again.  A ``RESTRICTED`` decision says exactly that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence

from .annotations import get_field

#: Target kinds whose scale is governed by the HPA controller convention.
SUPPORTED_TARGET_KINDS = ("Deployment", "StatefulSet")

#: Reason the HPA controller uses once it stops autoscaling a zeroed target.
SCALING_DISABLED_REASON = "ScalingDisabled"


class HpaSupport(str, Enum):
    SUPPORTED = "SUPPORTED"
    RESTRICTED = "RESTRICTED"
    UNSUPPORTED = "UNSUPPORTED"


class BoundaryDecision(str, Enum):
    ALLOW = "ALLOW"
    RESTRICTED = "RESTRICTED"
    CONFLICT = "CONFLICT"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class ScaleTargetRef:
    group: str
    kind: str
    name: str
    api_version: Optional[str] = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.group, self.kind, self.name)


def split_api_version(api_version: Optional[str]) -> tuple[str, str]:
    """``apps/v1`` -> ``("apps", "v1")``; ``v1`` -> ``("", "v1")``."""

    if not api_version:
        return ("", "")
    if "/" in api_version:
        group, version = api_version.split("/", 1)
        return (group, version)
    return ("", api_version)


def _scale_target_ref(obj: Any) -> Any:
    return get_field(get_field(obj, "spec"), "scale_target_ref") or get_field(
        get_field(obj, "spec"), "scaleTargetRef"
    )


def read_target_ref(hpa: Any) -> Optional[ScaleTargetRef]:
    ref = _scale_target_ref(hpa)
    if ref is None:
        return None
    api_version = get_field(ref, "api_version") or get_field(ref, "apiVersion")
    kind = get_field(ref, "kind")
    name = get_field(ref, "name")
    if not kind or not name:
        return None
    group, _ = split_api_version(api_version)
    return ScaleTargetRef(
        group=group, kind=kind, name=name, api_version=api_version
    )


def hpa_name(hpa: Any) -> Optional[str]:
    return get_field(get_field(hpa, "metadata"), "name")


def _spec(hpa: Any) -> Any:
    return get_field(hpa, "spec")


def min_replicas(hpa: Any, default: int = 1) -> int:
    value = get_field(_spec(hpa), "min_replicas")
    if value is None:
        value = get_field(_spec(hpa), "minReplicas")
    return default if value is None else int(value)


def max_replicas(hpa: Any) -> Optional[int]:
    value = get_field(_spec(hpa), "max_replicas")
    if value is None:
        value = get_field(_spec(hpa), "maxReplicas")
    return None if value is None else int(value)


def _conditions(hpa: Any) -> Sequence[Any]:
    return get_field(get_field(hpa, "status"), "conditions") or ()


def read_scaling_active(hpa: Any) -> Optional[tuple[str, str]]:
    """Return ``(status, reason)`` of the ``ScalingActive`` condition."""

    for condition in _conditions(hpa):
        if get_field(condition, "type") == "ScalingActive":
            return (
                str(get_field(condition, "status", "")),
                str(get_field(condition, "reason", "")),
            )
    return None


def is_scaling_disabled(hpa: Any) -> bool:
    condition = read_scaling_active(hpa)
    return condition is not None and condition[1] == SCALING_DISABLED_REASON


def hpas_for_target(
    hpas: Iterable[Any], target: ScaleTargetRef
) -> list[Any]:
    matches = []
    for hpa in hpas:
        ref = read_target_ref(hpa)
        if ref is not None and ref.key == target.key:
            matches.append(hpa)
    return matches


# ---------------------------------------------------------------------------
# Replica clamping
# ---------------------------------------------------------------------------


def clamp_replicas(candidate: Optional[int], minimum: int, maximum: int) -> int:
    """Clamp ``candidate`` into ``[minimum, maximum]``.

    With no candidate, use ``max(minimum, 1)``.  ``maximum`` is assumed to be
    at least ``minimum`` (invalid HPA specs are rejected before this point).
    """

    if candidate is None:
        return max(minimum, 1)
    if candidate < minimum:
        return minimum
    if candidate > maximum:
        return maximum
    return candidate


@dataclass(frozen=True)
class HpaBoundary:
    """The result of evaluating the HPA boundary for one scale target."""

    decision: BoundaryDecision
    target: ScaleTargetRef
    hpa_names: tuple[str, ...] = ()
    support: HpaSupport = HpaSupport.SUPPORTED
    replicas: Optional[int] = None
    reason: str = ""
    #: Optional diagnostic only; never used to authorise a takeover.
    preview_intersection: Optional[tuple[int, int]] = None

    @property
    def may_restore(self) -> bool:
        return self.decision in (BoundaryDecision.ALLOW, BoundaryDecision.RESTRICTED)

    @property
    def claims_full_control(self) -> bool:
        return self.decision is BoundaryDecision.ALLOW


def evaluate_boundary(
    hpas: Iterable[Any],
    target: ScaleTargetRef,
    candidate: Optional[int],
) -> HpaBoundary:
    matches = hpas_for_target(hpas, target)
    names = tuple(sorted(name for name in (hpa_name(h) for h in matches) if name))

    if not matches:
        replicas = candidate if candidate and candidate > 0 else 1
        return HpaBoundary(
            decision=BoundaryDecision.ALLOW,
            target=target,
            replicas=replicas,
            reason="no HPA targets this workload; restoring the candidate replicas",
        )

    if len(matches) > 1:
        preview = _intersect(matches)
        return HpaBoundary(
            decision=BoundaryDecision.CONFLICT,
            target=target,
            hpa_names=names,
            support=HpaSupport.RESTRICTED,
            reason=(
                "multiple HPAs target the same workload "
                f"({', '.join(names)}); refusing to take over. Intersecting their "
                "ranges is not proof that several scaling controllers can safely "
                "share one target"
            ),
            preview_intersection=preview,
        )

    hpa = matches[0]
    support, support_reason = classify_support(hpa, target)
    if support is HpaSupport.UNSUPPORTED:
        return HpaBoundary(
            decision=BoundaryDecision.UNSUPPORTED,
            target=target,
            hpa_names=names,
            support=support,
            reason=support_reason,
        )

    minimum = min_replicas(hpa)
    maximum = max_replicas(hpa)
    if maximum is None:
        return HpaBoundary(
            decision=BoundaryDecision.UNSUPPORTED,
            target=target,
            hpa_names=names,
            support=HpaSupport.UNSUPPORTED,
            reason="HPA has no maxReplicas; cannot compute a legal restore range",
        )
    if maximum < 1 or minimum < 0 or maximum < minimum:
        return HpaBoundary(
            decision=BoundaryDecision.UNSUPPORTED,
            target=target,
            hpa_names=names,
            support=HpaSupport.UNSUPPORTED,
            reason=(
                "HPA range is not restorable: "
                f"minReplicas={minimum}, maxReplicas={maximum}"
            ),
        )

    replicas = clamp_replicas(candidate, minimum, maximum)

    if support is HpaSupport.RESTRICTED:
        return HpaBoundary(
            decision=BoundaryDecision.RESTRICTED,
            target=target,
            hpa_names=names,
            support=support,
            replicas=replicas,
            reason=support_reason,
        )

    return HpaBoundary(
        decision=BoundaryDecision.ALLOW,
        target=target,
        hpa_names=names,
        support=support,
        replicas=replicas,
        reason=(
            "HPA will resume from the restored replicas "
            f"(clamped to [{minimum}, {maximum}])"
        ),
    )


def _intersect(hpas: Sequence[Any]) -> Optional[tuple[int, int]]:
    lows = []
    highs = []
    for hpa in hpas:
        maximum = max_replicas(hpa)
        if maximum is None:
            return None
        lows.append(min_replicas(hpa))
        highs.append(maximum)
    low = max(lows)
    high = min(highs)
    if low > high:
        return None
    return (low, high)


def classify_support(hpa: Any, target: ScaleTargetRef) -> tuple[HpaSupport, str]:
    """Classify whether the HPA's implicit-disable convention may be relied on."""

    if target.kind not in SUPPORTED_TARGET_KINDS:
        return (
            HpaSupport.UNSUPPORTED,
            f"scaleTargetRef kind {target.kind!r} is not managed by dailyclean "
            f"(supported: {', '.join(SUPPORTED_TARGET_KINDS)})",
        )
    if min_replicas(hpa) <= 0:
        return (
            HpaSupport.RESTRICTED,
            "HPA minReplicas is 0, so the target=0 implicit-disable convention "
            "does not apply and the HPA may scale the target back up on its own",
        )
    return (HpaSupport.SUPPORTED, "HPA implicit-disable convention applies")


# ---------------------------------------------------------------------------
# Observation after restore
# ---------------------------------------------------------------------------


class Observation(str, Enum):
    SCALING_ACTIVE = "SCALING_ACTIVE"
    STILL_DISABLED = "STILL_DISABLED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class HpaObservation:
    name: Optional[str]
    observation: Observation
    detail: str = ""


def observe_after_restore(hpa: Any) -> HpaObservation:
    """Read-only check of the HPA after the target was scaled back up.

    This deliberately does not wait or poll; it reports what the (re-read)
    object says so a caller can decide to re-queue.  ``ScalingDisabled`` still
    present means the controller has not yet noticed the new replica count.
    """

    condition = read_scaling_active(hpa)
    if condition is None:
        return HpaObservation(
            hpa_name(hpa),
            Observation.UNKNOWN,
            "HPA has no ScalingActive condition yet",
        )
    status, reason = condition
    if reason == SCALING_DISABLED_REASON:
        return HpaObservation(
            hpa_name(hpa),
            Observation.STILL_DISABLED,
            "HPA still reports ScalingDisabled; the controller has not observed "
            "the restored replica count yet",
        )
    if status == "True":
        return HpaObservation(
            hpa_name(hpa),
            Observation.SCALING_ACTIVE,
            "HPA scaling is active again",
        )
    return HpaObservation(
        hpa_name(hpa),
        Observation.UNKNOWN,
        f"ScalingActive={status} reason={reason}",
    )


__all__ = [
    "BoundaryDecision",
    "HpaBoundary",
    "HpaObservation",
    "HpaSupport",
    "Observation",
    "SUPPORTED_TARGET_KINDS",
    "ScaleTargetRef",
    "clamp_replicas",
    "classify_support",
    "evaluate_boundary",
    "hpas_for_target",
    "is_scaling_disabled",
    "max_replicas",
    "min_replicas",
    "observe_after_restore",
    "read_scaling_active",
    "read_target_ref",
    "split_api_version",
]
