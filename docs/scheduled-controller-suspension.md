# Scheduled-controller suspension & HPA hand-over

This document is the contract for letting DailyClean stop and start
**CronJobs** and for the **HPA boundary** around restoring a scale target.
It complements the existing replica-based stop/start of Deployments and
StatefulSets; it does **not** re-implement replica recording (see
`run-20261123-*-dailyclean-replica-restore-record`).

Everything below is implemented and unit-tested offline:

| Concern | Java (API + /status) | Python (job) |
| --- | --- | --- |
| CronJob stop/start decisions & patches | `service/CronJobSuspension.java` | `dailyclean_ops/cronjob.py` |
| HPA boundary & clamping | `service/HpaBoundary.java` | `dailyclean_ops/hpa.py` |
| Ordering / partial failure | (mirrored) | `dailyclean_ops/handover.py` |
| Client adapters | — | `dailyclean_ops/cluster.py` |

## 1. CronJob participation is opt-in

A CronJob is managed **only** when it carries:

```yaml
metadata:
  labels:
    axa.com/dailyclean: "true"     # case-insensitive "true"
```

Absent or `false` means **not managed** — DailyClean never suspends it. This is
deliberately stricter than Deployments (whose historical default is
"participate") so that **upgrading DailyClean never starts suspending CronJobs
that were previously unmanaged**.

`/status` only lists opted-in CronJobs (`KubernetesService.getManagedCronJobWrappers`).

## 2. Stop

For each opted-in CronJob that is **not** suspended:

1. DailyClean sets `spec.suspend: true`.
2. It records ownership in three annotations
   (contract version 1):

   | Annotation | Value | Role |
   | --- | --- | --- |
   | `axa.com/dailyclean-suspend-managed-by` | `dailyclean` | owner marker (**authoritative**) |
   | `axa.com/dailyclean-suspend-uid` | `metadata.uid` | object identity (**authoritative**) |
   | `axa.com/dailyclean-suspend-at` | RFC3339 instant | informational only |

A CronJob that is **already** suspended is left completely untouched and
DailyClean does **not** acquire the right to resume it (no record is written).
A CronJob already suspended by *us* (same UID) is a no-op (`ALREADY_MANAGED`), so
repeated stops never rewrite the object.

### Minimal, guarded patch

The write is a **strategic-merge** patch containing only `spec.suspend` plus the
three annotations and a `metadata.resourceVersion` precondition:

```json
{
  "metadata": {
    "resourceVersion": "7",
    "annotations": {
      "axa.com/dailyclean-suspend-managed-by": "dailyclean",
      "axa.com/dailyclean-suspend-uid": "0f1c…",
      "axa.com/dailyclean-suspend-at": "2026-11-23T01:00:00Z"
    }
  },
  "spec": { "suspend": true }
}
```

* Only agreed fields are written. `schedule`, `concurrencyPolicy`,
  `startingDeadlineSeconds`, `successfulJobsHistoryLimit`,
  `failedJobsHistoryLimit`, `timeZone`, other annotations and labels are **left
  untouched**.
* The whole read object is **never** submitted.
* The `resourceVersion` turns the patch into an optimistic-concurrency update. On
  **409** the object is re-read and the decision recomputed from scratch
  (bounded to 3 attempts); the old decision is never replayed.

### What "stop" does and does not do

* `spec.suspend: true` **only stops future triggers**.
* Jobs **already running are not terminated** — DailyClean never touches `Job`
  objects here.
* No job history is pruned, no schedule is rewritten, no catch-up run is forced.

## 3. Start

For each opted-in CronJob, resume **only** when all of these hold:

1. it is currently suspended,
2. the record's `managed-by` is `dailyclean`, and
3. the record's `uid` equals the **live** `metadata.uid`.

Then DailyClean writes a guarded patch with `spec.suspend: false` and removes
the three annotations. Otherwise:

| Situation | Result | Write? |
| --- | --- | --- |
| Suspended by someone else (no/foreign record) | `SKIPPED_NOT_OWNED` | no |
| Record names a different UID (recreated object) | `SKIPPED_STALE_UID` | no |
| We suspended it, user manually unsuspended | `CLEAR_STALE_RECORD` (annotations only) | yes |
| Running, no record | `SKIPPED_NOT_SUSPENDED` | no |

A **recreated** CronJob (delete → create) gets a new `uid`; the old record can
never resume it. A user who unsuspends manually is never re-suspended by an old
record (start only clears our annotations, it does not flip `suspend` back).

### Resume semantics & operator-visible risk

On resume Kubernetes **may create Jobs for triggers missed while suspended**,
governed by:

* `startingDeadlineSeconds` — if set, missed triggers older than the deadline are
  skipped; **unset** + a long suspension can cause the controller to schedule at
  most one immediate catch-up (count bounded by missed-but-within-deadline runs);
* `concurrencyPolicy` — `Forbid`/`Replace` may suppress or replace a run if one
  is already active;
* `successfulJobsHistoryLimit` / `failedJobsHistoryLimit` — unaffected by DailyClean.

DailyClean **preserves these fields as-is** and never forces an extra run.
Operators should confirm the catch-up behaviour for their schedule before
resuming a long suspension. This is standard Kubernetes behaviour, not something
DailyClean can promise to suppress.

## 4. HPA hand-over for a restored target

HPAs have **no generic `suspend` field**. DailyClean therefore:

* never fabricates a `suspend` field,
* never assumes every cluster accepts `minReplicas: 0`,
* never deletes/recreates an HPA.

The only convention relied on is the upstream HPA controller behaviour: when the
target's `spec.replicas` is `0` and the HPA's `minReplicas` is non-zero, the HPA
reports `ScalingActive=False` / reason `ScalingDisabled` and stops driving the
target. Scaling the target to 0 is thus a hand-over of the scaling decision to
*nobody*. `HpaBoundary` **reads** the HPA only; it never writes one.

### Support classification (`HpaBoundary.classifySupport`)

| Case | Support | Restores? |
| --- | --- | --- |
| `scaleTargetRef.kind` ∈ {Deployment, StatefulSet}, `minReplicas ≥ 1` | `SUPPORTED` | yes (full control asserted) |
| same kinds, `minReplicas = 0` | `RESTRICTED` | yes, but **no** promise another controller won't raise it |
| any other kind (e.g. `Rollout`) | `UNSUPPORTED` | no — explicitly rejected |

### Restoring replicas (clamp)

Given the **current** HPA range `[minReplicas, maxReplicas]` and an optional
candidate (the recorded replica count from the replica-restore feature):

| min | max | candidate | restored |
| --- | --- | --- | --- |
| 3 | 10 | 7 | **7** |
| 3 | 10 | 20 | **10** |
| 3 | 10 | none | **3** (`max(min,1)`) |
| 0 | 10 | none | **1** (RESTRICTED) |

`clampReplicas(candidate, min, max)` = `candidate<min ? min : candidate>max ? max : candidate`,
or `max(min,1)` when there is no candidate.

### Target recognition & conflicts

* Targets are matched by `scaleTargetRef` **group / kind / name** — never by
  labels.
* **Multiple HPAs for one target → `CONFLICT`**, no takeover. Intersecting their
  ranges (`previewIntersection`) is reported as a diagnostic only; it is **not**
  proof that several scaling controllers can safely share one target.

### Observation after restore

After scaling back up, `observeAfterRestore(hpa)` reads the HPA:

* `ScalingActive=True` → `SCALING_ACTIVE`,
* `ScalingDisabled` still present → `STILL_DISABLED` (controller hasn't caught
  up yet; informational, caller may re-queue),
* no condition → `UNKNOWN`.

Observation never writes an HPA and never by itself revokes a restoration.

## 5. Ordering and partial failure

```
stop :  1. suspend managed CronJobs   ->  2. scale targets down (0) [+ replica record]
start:  1. restore targets (HPA clamp) ->  2. resume managed CronJobs  ->  3. observe HPAs
```

Rules:

* **start** does not resume CronJobs until target restoration **fully
  completes**. If restore is partial/failed, CronJob resume and HPA observation
  are `SKIPPED`, and the result reports `control_restored = false` — DailyClean
  never claims control was restored when a prerequisite step did not finish.
* **stop** suspends CronJobs first (so no new trigger fires during teardown);
  a CronJob that fails to suspend is reported but does not block scaling down
  (`fully_stopped` reflects the teardown result).
* A failed CronJob resume (`CONFLICT`/`ERROR`) means `control_restored = false`.

`HandoverResult` exposes: `success`, `control_restored`, `fully_stopped`, and a
per-phase `summary()`.

## 6. `/status`

* CronJob workloads are judged by **suspension** (`spec.suspend`), never by a
  replica count.
* `Workload.type` gains `CRONJOB` and `UNSUPPORTED`.
* `Workload.suspended` is present for CronJobs only.
* `UNSUPPORTED` objects (e.g. an opted-in CronJob with no spec) are reported but
  excluded from the started/stopped tally and the overall state.
* `swagger.yml` and the React types/UI are updated; unknown enum values on an
  older decoder fail only the `type` field (see `WorkloadCompatibilityTest`) —
  they do not corrupt the rest of the payload.

## 7. Offline verification

No cluster is required.

**Java** (`mvn test`):
* `CronJobSuspensionTest` — opt-in, already-externally-suspended, idempotent,
  stale UID, manual unsuspend, missing uid/resourceVersion, minimal patch,
  patch-object serialisation.
* `HpaBoundaryTest` — the three clamp examples, `minReplicas=0` restricted,
  unsupported kind, multi-HPA conflict + preview, invalid range, observation.
* `CronJobStatusResourceTest` — suspended↔stopped, opt-in filtering, mixed state.
* `WorkloadCompatibilityTest` — new enum values and old-client behaviour.

**Python** (`python3 -m unittest discover -s tests -t .` in `job/dailyclean`):
* `test_cronjob.py` — full stop/start matrix with an in-memory `FakeCronJobApi`
  that records every patch and enforces the `resourceVersion` precondition;
  asserts only agreed fields change and `Job` objects are never touched.
* `test_hpa.py` — clamp examples, conflict, restricted/unsupported.
* `test_handover.py` — ordering (CronJob-before-teardown, restore-before-resume-
  before-observe), skipped-on-partial, control_restored.
* `test_cluster.py` — the client adapters with recording doubles (label selector,
  patch bodies, scale sub-resource, HPA matching by targetRef).
