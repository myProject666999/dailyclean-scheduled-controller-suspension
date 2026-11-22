"""Annotation and label contract shared by the API (Java) and the job (Python).

CronJob participation is opt-in through the historical DailyClean label:

    axa.com/dailyclean: "true"      -> DailyClean may suspend this CronJob
    (absent / anything else)        -> DailyClean never touches it

When DailyClean suspends a CronJob it records *who* did it and *which object*
was suspended, so that a later ``start`` can prove that it still owns the
suspension::

    axa.com/dailyclean-suspend-managed-by: "dailyclean"   # owner marker
    axa.com/dailyclean-suspend-uid:        "<metadata.uid>"  # object identity
    axa.com/dailyclean-suspend-at:         "<RFC3339 instant>"  # informational

The three annotations are version 1 of the contract.  ``suspended-at`` is
informational only; the authority to resume comes from ``managed-by`` plus a
matching ``uid``.  A record without a UID is never written, and a record whose
UID no longer matches the live object is ignored, which is how a delete /
recreate of the CronJob revokes DailyClean's recovery right.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

OPT_IN_LABEL = "axa.com/dailyclean"
OWNER_VALUE = "dailyclean"

MANAGED_BY_ANNOTATION = "axa.com/dailyclean-suspend-managed-by"
SUSPENDED_UID_ANNOTATION = "axa.com/dailyclean-suspend-uid"
SUSPENDED_AT_ANNOTATION = "axa.com/dailyclean-suspend-at"

CONTRACT_VERSION = 1

#: All annotations owned by DailyClean for the suspension hand-over.
SUSPENSION_ANNOTATIONS = (
    MANAGED_BY_ANNOTATION,
    SUSPENDED_UID_ANNOTATION,
    SUSPENDED_AT_ANNOTATION,
)


def get_field(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a kubernetes client model or a plain dict.

    The real ``kubernetes`` package models are snake_case attribute based;
    in-memory doubles and raw API JSON are camelCase.  Both spellings are
    accepted so the production code stays branch-free.
    """

    if obj is None:
        return default
    camel = _to_camel(name)
    candidates = (name, camel) if camel != name else (name,)
    if isinstance(obj, Mapping):
        for key in candidates:
            if key in obj:
                return obj[key]
        return default
    for key in candidates:
        if hasattr(obj, key):
            return getattr(obj, key)
    return default


def _to_camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


def metadata_of(obj: Any) -> Any:
    return get_field(obj, "metadata")


def labels_of(obj: Any) -> Mapping[str, str]:
    labels = get_field(metadata_of(obj), "labels")
    return labels or {}


def annotations_of(obj: Any) -> Mapping[str, str]:
    annotations = get_field(metadata_of(obj), "annotations")
    return annotations or {}


def uid_of(obj: Any) -> Optional[str]:
    return get_field(metadata_of(obj), "uid")


def name_of(obj: Any) -> Optional[str]:
    return get_field(metadata_of(obj), "name")


def resource_version_of(obj: Any) -> Optional[str]:
    return get_field(metadata_of(obj), "resource_version")


def is_opted_in(obj: Any, label: str = OPT_IN_LABEL) -> bool:
    """CronJobs participate only when the label explicitly says ``true``."""

    value = labels_of(obj).get(label)
    return isinstance(value, str) and value.strip().lower() == "true"


@dataclass(frozen=True)
class SuspensionRecord:
    """A parsed DailyClean suspension record."""

    managed_by: str
    uid: Optional[str]
    suspended_at: Optional[str]

    @property
    def is_owned_by_dailyclean(self) -> bool:
        return self.managed_by == OWNER_VALUE

    def matches_uid(self, uid: Optional[str]) -> bool:
        return bool(uid) and self.uid == uid


def read_suspension_record(obj: Any) -> Optional[SuspensionRecord]:
    annotations = annotations_of(obj)
    managed_by = annotations.get(MANAGED_BY_ANNOTATION)
    if not managed_by:
        return None
    return SuspensionRecord(
        managed_by=managed_by,
        uid=annotations.get(SUSPENDED_UID_ANNOTATION),
        suspended_at=annotations.get(SUSPENDED_AT_ANNOTATION),
    )


def suspension_annotation_patch(
    uid: str, suspended_at: str
) -> dict[str, str]:
    """Annotations written by ``stop`` (merge patch: other keys are kept)."""

    return {
        MANAGED_BY_ANNOTATION: OWNER_VALUE,
        SUSPENDED_UID_ANNOTATION: uid,
        SUSPENDED_AT_ANNOTATION: suspended_at,
    }


def suspension_annotation_clear_patch() -> dict[str, None]:
    """Annotations removed by ``start`` (``null`` deletes a merge-patch key)."""

    return {key: None for key in SUSPENSION_ANNOTATIONS}
