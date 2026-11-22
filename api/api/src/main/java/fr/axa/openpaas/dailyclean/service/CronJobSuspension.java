package fr.axa.openpaas.dailyclean.service;

import fr.axa.openpaas.dailyclean.model.Workload;
import io.fabric8.kubernetes.api.model.ObjectMeta;
import io.fabric8.kubernetes.api.model.batch.v1.CronJob;
import io.fabric8.kubernetes.api.model.batch.v1.CronJobBuilder;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Optional;

/**
 * The suspension hand-over contract for managed CronJobs.
 *
 * <p>This class holds the pure decision logic and patch construction shared by
 * the API and (mirrored in) the Python job. It performs no I/O: given a
 * {@link CronJob} it returns an {@link Action} and, when a write is warranted,
 * the minimal strategic-merge patch body. Callers apply that patch with a
 * resourceVersion precondition and re-read on conflict.
 *
 * <h2>Control boundary</h2>
 * <ul>
 *   <li>A CronJob is managed only when labelled
 *       {@code axa.com/dailyclean=true}. Absent or {@code false} means "not
 *       managed" — never suspend it.</li>
 *   <li>{@code stop} sets {@code spec.suspend=true} and records
 *       {@code managed-by=dailyclean}, the object {@code uid} and a
 *       {@code suspended-at} timestamp as annotations.</li>
 *   <li>A CronJob that is <em>already</em> suspended stays untouched and
 *       DailyClean never gains the right to resume it.</li>
 *   <li>{@code start} resumes only a suspension whose record still names the
 *       same {@code metadata.uid}. If the object was recreated or a human
 *       changed the suspension, the record is stale and is not used.</li>
 *   <li>Suspending only stops <em>future</em> triggers. Running Jobs are not
 *       terminated, and on resume Kubernetes may create catch-up Jobs governed
 *       by {@code startingDeadlineSeconds}, {@code concurrencyPolicy} and the
 *       history limits. Those fields are never modified here.</li>
 * </ul>
 */
public final class CronJobSuspension {

    public static final String OPT_IN_LABEL = "axa.com/dailyclean";
    public static final String OWNER_VALUE = "dailyclean";

    public static final String MANAGED_BY_ANNOTATION = "axa.com/dailyclean-suspend-managed-by";
    public static final String SUSPENDED_UID_ANNOTATION = "axa.com/dailyclean-suspend-uid";
    public static final String SUSPENDED_AT_ANNOTATION = "axa.com/dailyclean-suspend-at";

    private CronJobSuspension() {
    }

    /**
     * The outcome of evaluating a stop or start decision for one CronJob.
     */
    public enum Action {
        SUSPEND,
        ALREADY_MANAGED,
        ALREADY_SUSPENDED_EXTERNALLY,
        SKIPPED_NOT_OPTED_IN,
        RESUMED,
        CLEAR_STALE_RECORD,
        SKIPPED_NOT_OWNED,
        SKIPPED_STALE_UID,
        SKIPPED_NOT_SUSPENDED,
        SKIPPED_MISSING_UID,
        SKIPPED_MISSING_PRECONDITION
    }

    /**
     * A decision plus the merge-patch body to send, if any.
     */
    public record Decision(Action action, Map<String, Object> patch, String detail) {

        public boolean shouldWrite() {
            return patch != null;
        }

        public static Decision of(Action action, String detail) {
            return new Decision(action, null, detail);
        }
    }

    // -----------------------------------------------------------------
    // Optional helpers
    // -----------------------------------------------------------------

    public static boolean isOptedIn(CronJob cronJob) {
        Map<String, String> labels = labelsOf(cronJob);
        String value = labels == null ? null : labels.get(OPT_IN_LABEL);
        return value != null && "true".equalsIgnoreCase(value.trim());
    }

    public static boolean isSuspended(CronJob cronJob) {
        return cronJob.getSpec() != null && Boolean.TRUE.equals(cronJob.getSpec().getSuspend());
    }

    public static Optional<SuspensionRecord> readRecord(CronJob cronJob) {
        Map<String, String> annotations = annotationsOf(cronJob);
        if (annotations == null) {
            return Optional.empty();
        }
        String managedBy = annotations.get(MANAGED_BY_ANNOTATION);
        if (managedBy == null || managedBy.isBlank()) {
            return Optional.empty();
        }
        return Optional.of(new SuspensionRecord(
                managedBy,
                annotations.get(SUSPENDED_UID_ANNOTATION),
                annotations.get(SUSPENDED_AT_ANNOTATION)));
    }

    public record SuspensionRecord(String managedBy, String uid, String suspendedAt) {
        public boolean ownedByDailyclean() {
            return OWNER_VALUE.equals(managedBy);
        }

        public boolean matchesUid(String currentUid) {
            return currentUid != null && currentUid.equals(uid);
        }
    }

    // -----------------------------------------------------------------
    // Decisions
    // -----------------------------------------------------------------

    public static Decision decideStop(CronJob cronJob, Instant now) {
        if (!isOptedIn(cronJob)) {
            return Decision.of(Action.SKIPPED_NOT_OPTED_IN,
                    "CronJob is not opted in via " + OPT_IN_LABEL + "=true");
        }
        String uid = uidOf(cronJob);
        String resourceVersion = resourceVersionOf(cronJob);

        if (!isSuspended(cronJob)) {
            if (uid == null) {
                return Decision.of(Action.SKIPPED_MISSING_UID,
                        "CronJob has no metadata.uid; ownership cannot be recorded");
            }
            if (resourceVersion == null) {
                return Decision.of(Action.SKIPPED_MISSING_PRECONDITION,
                        "CronJob has no metadata.resourceVersion; refusing an unguarded write");
            }
            return new Decision(Action.SUSPEND,
                    suspendPatch(resourceVersion, uid, now),
                    "suspension taken by dailyclean");
        }

        Optional<SuspensionRecord> record = readRecord(cronJob);
        if (record.isPresent()
                && record.get().ownedByDailyclean()
                && record.get().matchesUid(uid)) {
            return Decision.of(Action.ALREADY_MANAGED,
                    "already suspended by dailyclean; no write");
        }
        return Decision.of(Action.ALREADY_SUSPENDED_EXTERNALLY,
                "already suspended by someone else; ownership not taken");
    }

    public static Decision decideStart(CronJob cronJob, Instant now) {
        if (!isOptedIn(cronJob)) {
            return Decision.of(Action.SKIPPED_NOT_OPTED_IN,
                    "CronJob is not opted in via " + OPT_IN_LABEL + "=true");
        }
        String uid = uidOf(cronJob);
        String resourceVersion = resourceVersionOf(cronJob);
        Optional<SuspensionRecord> record = readRecord(cronJob);

        if (isSuspended(cronJob)) {
            if (record.isEmpty() || !record.get().ownedByDailyclean()) {
                return Decision.of(Action.SKIPPED_NOT_OWNED,
                        "CronJob is suspended but not by dailyclean; leaving it untouched");
            }
            if (!record.get().matchesUid(uid)) {
                return Decision.of(Action.SKIPPED_STALE_UID,
                        "ownership record belongs to another metadata.uid; not resuming");
            }
            if (resourceVersion == null) {
                return Decision.of(Action.SKIPPED_MISSING_PRECONDITION,
                        "CronJob has no metadata.resourceVersion; refusing an unguarded write");
            }
            return new Decision(Action.RESUMED,
                    resumePatch(resourceVersion),
                    "resuming a suspension still owned by dailyclean");
        }

        if (record.isPresent() && record.get().ownedByDailyclean()) {
            if (resourceVersion == null) {
                return Decision.of(Action.SKIPPED_MISSING_PRECONDITION,
                        "stale record found but no metadata.resourceVersion to guard its removal");
            }
            return new Decision(Action.CLEAR_STALE_RECORD,
                    clearRecordPatch(resourceVersion),
                    "CronJob is no longer suspended by us; clearing the stale ownership record");
        }
        return Decision.of(Action.SKIPPED_NOT_SUSPENDED,
                "CronJob is not suspended; nothing to resume");
    }

    // -----------------------------------------------------------------
    // Patch construction (minimal, agreed fields only)
    // -----------------------------------------------------------------

    static Map<String, Object> suspendPatch(String resourceVersion, String uid, Instant now) {
        Map<String, Object> annotations = new LinkedHashMap<>();
        annotations.put(MANAGED_BY_ANNOTATION, OWNER_VALUE);
        annotations.put(SUSPENDED_UID_ANNOTATION, uid);
        annotations.put(SUSPENDED_AT_ANNOTATION, now.toString());

        Map<String, Object> metadata = new LinkedHashMap<>();
        metadata.put("resourceVersion", resourceVersion);
        metadata.put("annotations", annotations);

        Map<String, Object> spec = new LinkedHashMap<>();
        spec.put("suspend", true);

        Map<String, Object> patch = new LinkedHashMap<>();
        patch.put("metadata", metadata);
        patch.put("spec", spec);
        return patch;
    }

    static Map<String, Object> resumePatch(String resourceVersion) {
        Map<String, Object> spec = new LinkedHashMap<>();
        spec.put("suspend", false);

        Map<String, Object> patch = new LinkedHashMap<>();
        patch.put("metadata", metadataWithClearedRecord(resourceVersion));
        patch.put("spec", spec);
        return patch;
    }

    static Map<String, Object> clearRecordPatch(String resourceVersion) {
        Map<String, Object> patch = new LinkedHashMap<>();
        patch.put("metadata", metadataWithClearedRecord(resourceVersion));
        return patch;
    }

    private static Map<String, Object> metadataWithClearedRecord(String resourceVersion) {
        Map<String, Object> annotations = new LinkedHashMap<>();
        annotations.put(MANAGED_BY_ANNOTATION, null);
        annotations.put(SUSPENDED_UID_ANNOTATION, null);
        annotations.put(SUSPENDED_AT_ANNOTATION, null);

        Map<String, Object> metadata = new LinkedHashMap<>();
        metadata.put("resourceVersion", resourceVersion);
        metadata.put("annotations", annotations);
        return metadata;
    }

    /**
     * Build a CronJob carrying the given merge-patch body, for use with
     * {@code resource.patch(PatchContext.of(PatchType.STRATEGIC_MERGE), cronJob)}.
     * Only the fields present in the patch are serialised.
     */
    public static CronJob asPatchObject(String name, Map<String, Object> patch) {
        CronJobBuilder builder = new CronJobBuilder().withNewMetadata().withName(name).endMetadata();

        @SuppressWarnings("unchecked")
        Map<String, Object> metadata = (Map<String, Object>) patch.get("metadata");
        if (metadata != null) {
            builder.editMetadata()
                    .withResourceVersion((String) metadata.get("resourceVersion"))
                    .addToAnnotations(toStringMap(metadata.get("annotations")))
                    .endMetadata();
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> spec = (Map<String, Object>) patch.get("spec");
        if (spec != null && spec.containsKey("suspend")) {
            builder.editSpec().withSuspend((Boolean) spec.get("suspend")).endSpec();
        }
        return builder.build();
    }

    @SuppressWarnings("unchecked")
    private static Map<String, String> toStringMap(Object annotations) {
        if (annotations == null) {
            return Map.of();
        }
        return (Map<String, String>) annotations;
    }

    // -----------------------------------------------------------------
    // Metadata helpers
    // -----------------------------------------------------------------

    private static ObjectMeta metadataOf(CronJob cronJob) {
        return cronJob == null ? null : cronJob.getMetadata();
    }

    private static Map<String, String> labelsOf(CronJob cronJob) {
        ObjectMeta metadata = metadataOf(cronJob);
        return metadata == null ? null : metadata.getLabels();
    }

    private static Map<String, String> annotationsOf(CronJob cronJob) {
        ObjectMeta metadata = metadataOf(cronJob);
        return metadata == null ? null : metadata.getAnnotations();
    }

    private static String uidOf(CronJob cronJob) {
        ObjectMeta metadata = metadataOf(cronJob);
        return metadata == null ? null : metadata.getUid();
    }

    private static String resourceVersionOf(CronJob cronJob) {
        ObjectMeta metadata = metadataOf(cronJob);
        return metadata == null ? null : metadata.getResourceVersion();
    }

    /**
     * Project a CronJob into the {@link Workload} type reported by /status.
     */
    public static Workload.TypeEnum workloadType() {
        return Workload.TypeEnum.CRONJOB;
    }
}
