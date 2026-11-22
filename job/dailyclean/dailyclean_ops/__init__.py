"""Offline-testable helpers used by the DailyClean job.

The modules in this package never talk to a real cluster by themselves: they
accept Kubernetes objects and API facades as arguments.  This keeps the
suspension / HPA hand-over contract testable with plain in-memory doubles.
"""

from .annotations import (  # noqa: F401
    MANAGED_BY_ANNOTATION,
    OPT_IN_LABEL,
    OWNER_VALUE,
    SUSPENDED_AT_ANNOTATION,
    SUSPENDED_UID_ANNOTATION,
)
