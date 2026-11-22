"""Replica restoration within the HPA boundary.

The actual *recording* of replicas belongs to the replica-restore feature; this
module only answers "to what value may DailyClean restore this target?" using
the HPA control boundary in :mod:`dailyclean_ops.hpa`. It reads HPAs and never
writes one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .hpa import (
    BoundaryDecision,
    HpaBoundary,
    ScaleTargetRef,
    evaluate_boundary,
)


@dataclass(frozen=True)
class RestorePlan:
    """The decision to restore one scale target."""

    kind: str
    name: str
    replicas: Optional[int]
    boundary: Optional[HpaBoundary] = None
    detail: str = ""

    @property
    def should_restore(self) -> bool:
        return self.replicas is not None


def plan_restore(
    hpas: Iterable[Any],
    kind: str,
    name: str,
    candidate: Optional[int],
    group: str = "apps",
) -> RestorePlan:
    target = ScaleTargetRef(group=group, kind=kind, name=name)
    boundary = evaluate_boundary(hpas, target, candidate)

    if boundary.decision in (BoundaryDecision.UNSUPPORTED, BoundaryDecision.CONFLICT):
        return RestorePlan(kind, name, None, boundary, boundary.reason)

    return RestorePlan(kind, name, boundary.replicas, boundary, boundary.reason)


def plan_restore_with_reader(
    hpa_reader: Any,
    kind: str,
    name: str,
    candidate: Optional[int],
    group: str = "apps",
    log=None,
) -> RestorePlan:
    """Convenience wrapper that reads HPAs from a reader, tolerating failures.

    A failure to *list* HPAs must not block restoring other targets: the target
    is restored to the candidate (or 1) and the boundary is left unknown.
    """

    try:
        hpas = hpa_reader.list_hpas()
    except Exception as error:  # noqa: BLE001 - a read failure is per-target
        if log is not None:
            log(f"Error while listing HPAs for {kind}/{name} ({error}).")
        fallback = candidate if candidate and candidate > 0 else 1
        return RestorePlan(kind, name, fallback, None, "HPA list failed; using fallback")

    plan = plan_restore(hpas, kind, name, candidate, group)
    if log is not None:
        if not plan.should_restore:
            log(f"{kind}/{name}: {plan.boundary.decision.value} boundary ({plan.detail}); not restoring.")
        elif plan.boundary is not None and plan.boundary.decision is BoundaryDecision.RESTRICTED:
            log(f"{kind}/{name}: restricted HPA boundary ({plan.detail}).")
    return plan


__all__ = ["RestorePlan", "plan_restore", "plan_restore_with_reader"]
