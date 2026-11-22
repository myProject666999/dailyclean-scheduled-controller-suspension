"""Thin adapters between the ``kubernetes`` client and the offline modules.

Everything that touches a client lives here.  The adapters only build the
request objects the contract modules decide on; they never invent fields.
They take an already-configured ``ApiClient`` so the same code can run against
a real cluster, the recording test doubles in ``tests/``, or the in-memory
client used during manual smoke tests.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from .annotations import OPT_IN_LABEL
from .cronjob import CronJobClient
from .hpa import ScaleTargetRef, read_target_ref


def _exceptions():
    # Imported lazily so the pure contract modules stay importable (and
    # testable) without the full ``kubernetes`` dependency tree present.
    from kubernetes import client

    return client.exceptions


def utc_now_rfc3339() -> str:
    """RFC3339 timestamp with second precision, always in UTC."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


class KubernetesCronJobClient(CronJobClient):
    """``CronJobClient`` backed by ``BatchV1Api``."""

    def __init__(self, api: Any, namespace: str):
        self._api = api
        self._namespace = namespace

    def list_cron_jobs(self) -> Sequence[Any]:
        # Only opted-in CronJobs are ever candidates; the label selector keeps
        # the server round-trip small and makes the contract obvious.
        return self._api.list_namespaced_cron_job(
            namespace=self._namespace,
            label_selector=f"{OPT_IN_LABEL}=true",
        ).items

    def get_cron_job(self, name: str) -> Optional[Any]:
        try:
            return self._api.read_namespaced_cron_job(
                name=name, namespace=self._namespace
            )
        except _exceptions().ApiException as error:
            if error.status == 404:
                return None
            raise

    def patch_cron_job(self, name: str, patch: Mapping[str, Any]) -> Any:
        # Strategic merge patch: only the keys in ``patch`` are written, and a
        # stale metadata.resourceVersion turns the request into a 409.
        return self._api.patch_namespaced_cron_job(
            name=name,
            namespace=self._namespace,
            body=dict(patch),
        )


class KubernetesHpaReader:
    """Read-only HPA access; DailyClean never writes an HPA."""

    def __init__(self, api: Any, namespace: str):
        self._api = api
        self._namespace = namespace

    def list_hpas(self) -> Sequence[Any]:
        return self._api.list_namespaced_horizontal_pod_autoscaler(
            namespace=self._namespace
        ).items

    def hpas_for(self, target: ScaleTargetRef) -> list[Any]:
        matches = []
        for hpa in self.list_hpas():
            ref = read_target_ref(hpa)
            if ref is not None and ref.key == target.key:
                matches.append(hpa)
        return matches


class KubernetesScaleClient:
    """Scale sub-resource access for Deployments and StatefulSets.

    The replica *recording* (annotation read/write) belongs to the
    replica-restore feature; this adapter only exposes the scale read/write so
    the hand-over can restore to a decided value, and never builds a full
    object replacement.
    """

    def __init__(self, apps_api: Any, namespace: str):
        self._api = apps_api
        self._namespace = namespace

    def read_replicas(self, target: ScaleTargetRef) -> Optional[int]:
        if target.kind == "Deployment":
            obj = self._api.read_namespaced_deployment(
                name=target.name, namespace=self._namespace
            )
        elif target.kind == "StatefulSet":
            obj = self._api.read_namespaced_stateful_set(
                name=target.name, namespace=self._namespace
            )
        else:
            return None
        spec = getattr(obj, "spec", None)
        return getattr(spec, "replicas", None)

    def scale(self, target: ScaleTargetRef, replicas: int) -> None:
        """Scale via the scale sub-resource with a guarded merge patch."""

        if target.kind == "Deployment":
            current = self._api.read_namespaced_deployment(
                name=target.name, namespace=self._namespace
            )
            api = self._api.patch_namespaced_deployment_scale
        elif target.kind == "StatefulSet":
            current = self._api.read_namespaced_stateful_set(
                name=target.name, namespace=self._namespace
            )
            api = self._api.patch_namespaced_stateful_set_scale
        else:
            raise ValueError(f"unsupported scale target kind {target.kind!r}")

        resource_version = getattr(
            getattr(current, "metadata", None), "resource_version", None
        )
        body = {
            "spec": {"replicas": replicas},
            "metadata": {},
        }
        if resource_version:
            body["metadata"]["resourceVersion"] = resource_version
        api(name=target.name, namespace=self._namespace, body=body)


__all__ = [
    "KubernetesCronJobClient",
    "KubernetesHpaReader",
    "KubernetesScaleClient",
    "utc_now_rfc3339",
]
