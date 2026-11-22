package fr.axa.openpaas.dailyclean.service;

import io.fabric8.kubernetes.api.model.ObjectMeta;
import io.fabric8.kubernetes.api.model.batch.v1.CronJob;
import io.fabric8.kubernetes.api.model.batch.v1.CronJobBuilder;
import io.fabric8.kubernetes.api.model.batch.v1.CronJobSpec;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.Map;

import static fr.axa.openpaas.dailyclean.service.CronJobSuspension.Action;
import static fr.axa.openpaas.dailyclean.service.CronJobSuspension.Decision;
import static org.hamcrest.MatcherAssert.assertThat;
import static org.hamcrest.Matchers.contains;
import static org.hamcrest.Matchers.containsInAnyOrder;
import static org.hamcrest.Matchers.is;
import static org.hamcrest.Matchers.nullValue;

class CronJobSuspensionTest {

    private static final String UID = "uid-1";
    private static final String RV = "7";
    private static final Instant NOW = Instant.parse("2026-11-23T01:00:00Z");

    private static CronJob cronJob(boolean suspend, String optIn, Map<String, String> annotations) {
        CronJobBuilder builder = new CronJobBuilder()
                .withNewMetadata()
                    .withName("report")
                    .withNamespace("test")
                    .withUid(UID)
                    .withResourceVersion(RV)
                    .addToLabels("app", "report")
                    .addToAnnotations(annotations)
                    .endMetadata()
                .withNewSpec()
                    .withSchedule("0 2 * * *")
                    .withConcurrencyPolicy("Forbid")
                    .withSuspend(suspend)
                    .endSpec();
        CronJob cronJob = builder.build();
        if (optIn != null) {
            cronJob.getMetadata().getLabels().put(CronJobSuspension.OPT_IN_LABEL, optIn);
        }
        return cronJob;
    }

    private static CronJob optedIn(boolean suspend) {
        return cronJob(suspend, "true", Map.of());
    }

    private static Map<String, String> ownedRecord(String uid) {
        return Map.of(
                CronJobSuspension.MANAGED_BY_ANNOTATION, CronJobSuspension.OWNER_VALUE,
                CronJobSuspension.SUSPENDED_UID_ANNOTATION, uid,
                CronJobSuspension.SUSPENDED_AT_ANNOTATION, NOW.toString());
    }

    // ------------------------------------------------------------------
    // Opt-in
    // ------------------------------------------------------------------

    @Test
    void cronjob_is_not_managed_by_default() {
        CronJob notLabelled = cronJob(false, null, Map.of());
        assertThat(CronJobSuspension.isOptedIn(notLabelled), is(false));
        assertThat(CronJobSuspension.decideStop(notLabelled, NOW).action(),
                is(Action.SKIPPED_NOT_OPTED_IN));

        CronJob disabled = cronJob(false, "false", Map.of());
        assertThat(CronJobSuspension.decideStop(disabled, NOW).action(),
                is(Action.SKIPPED_NOT_OPTED_IN));
        assertThat(CronJobSuspension.decideStart(disabled, NOW).action(),
                is(Action.SKIPPED_NOT_OPTED_IN));
    }

    @Test
    void explicit_true_is_accepted_case_insensitively() {
        assertThat(CronJobSuspension.isOptedIn(cronJob(false, "TRUE", Map.of())), is(true));
        assertThat(CronJobSuspension.isOptedIn(cronJob(false, " true ", Map.of())), is(true));
    }

    // ------------------------------------------------------------------
    // Stop
    // ------------------------------------------------------------------

    @Test
    void stop_suspends_and_records_ownership() {
        Decision decision = CronJobSuspension.decideStop(optedIn(false), NOW);

        assertThat(decision.action(), is(Action.SUSPEND));
        assertThat(decision.shouldWrite(), is(true));

        Map<String, Object> patch = decision.patch();
        assertThat(patch.keySet(), containsInAnyOrder("metadata", "spec"));

        @SuppressWarnings("unchecked")
        Map<String, Object> metadata = (Map<String, Object>) patch.get("metadata");
        assertThat(metadata.get("resourceVersion"), is(RV));

        @SuppressWarnings("unchecked")
        Map<String, String> annotations = (Map<String, String>) metadata.get("annotations");
        assertThat(annotations.get(CronJobSuspension.MANAGED_BY_ANNOTATION),
                is(CronJobSuspension.OWNER_VALUE));
        assertThat(annotations.get(CronJobSuspension.SUSPENDED_UID_ANNOTATION), is(UID));
        assertThat(annotations.get(CronJobSuspension.SUSPENDED_AT_ANNOTATION), is(NOW.toString()));

        @SuppressWarnings("unchecked")
        Map<String, Object> spec = (Map<String, Object>) patch.get("spec");
        assertThat(spec, is(Map.of("suspend", true)));
    }

    @Test
    void stop_patch_only_touches_agreed_fields() {
        Decision decision = CronJobSuspension.decideStop(optedIn(false), NOW);
        @SuppressWarnings("unchecked")
        Map<String, Object> spec = (Map<String, Object>) decision.patch().get("spec");
        assertThat(spec.size(), is(1));
        assertThat(spec.containsKey("schedule"), is(false));
        assertThat(spec.containsKey("concurrencyPolicy"), is(false));
        assertThat(spec.containsKey("startingDeadlineSeconds"), is(false));
        assertThat(spec.containsKey("successfulJobsHistoryLimit"), is(false));
    }

    @Test
    void stop_leaves_an_already_externally_suspended_cronjob_alone() {
        Decision decision = CronJobSuspension.decideStop(optedIn(true), NOW);
        assertThat(decision.action(), is(Action.ALREADY_SUSPENDED_EXTERNALLY));
        assertThat(decision.shouldWrite(), is(false));
    }

    @Test
    void stop_is_idempotent_when_already_managed() {
        CronJob managed = cronJob(true, "true", ownedRecord(UID));
        Decision decision = CronJobSuspension.decideStop(managed, NOW);
        assertThat(decision.action(), is(Action.ALREADY_MANAGED));
        assertThat(decision.shouldWrite(), is(false));
    }

    @Test
    void stop_refuses_to_reclaim_a_stale_record() {
        CronJob rebuilt = cronJob(true, "true", ownedRecord("uid-old"));
        Decision decision = CronJobSuspension.decideStop(rebuilt, NOW);
        assertThat(decision.action(), is(Action.ALREADY_SUSPENDED_EXTERNALLY));
        assertThat(decision.shouldWrite(), is(false));
    }

    @Test
    void stop_without_uid_is_refused() {
        CronJob noUid = new CronJobBuilder()
                .withNewMetadata().withName("report").withResourceVersion(RV)
                    .addToLabels(CronJobSuspension.OPT_IN_LABEL, "true")
                    .endMetadata()
                .withNewSpec().withSchedule("0 2 * * *").withSuspend(false).endSpec()
                .build();

        Decision decision = CronJobSuspension.decideStop(noUid, NOW);
        assertThat(decision.action(), is(Action.SKIPPED_MISSING_UID));
        assertThat(decision.shouldWrite(), is(false));
    }

    @Test
    void stop_without_resource_version_is_refused() {
        CronJob noRv = new CronJobBuilder()
                .withNewMetadata().withName("report").withUid(UID)
                    .addToLabels(CronJobSuspension.OPT_IN_LABEL, "true")
                    .endMetadata()
                .withNewSpec().withSchedule("0 2 * * *").withSuspend(false).endSpec()
                .build();

        Decision decision = CronJobSuspension.decideStop(noRv, NOW);
        assertThat(decision.action(), is(Action.SKIPPED_MISSING_PRECONDITION));
        assertThat(decision.shouldWrite(), is(false));
    }

    // ------------------------------------------------------------------
    // Start
    // ------------------------------------------------------------------

    @Test
    void start_resumes_a_suspension_we_own() {
        CronJob managed = cronJob(true, "true", ownedRecord(UID));
        Decision decision = CronJobSuspension.decideStart(managed, NOW);

        assertThat(decision.action(), is(Action.RESUMED));
        assertThat(decision.shouldWrite(), is(true));

        Map<String, Object> patch = decision.patch();
        assertThat(patch.keySet(), containsInAnyOrder("metadata", "spec"));
        @SuppressWarnings("unchecked")
        Map<String, Object> spec = (Map<String, Object>) patch.get("spec");
        assertThat(spec, is(Map.of("suspend", false)));
    }

    @Test
    void start_leaves_a_foreign_suspension_alone() {
        CronJob paused = cronJob(true, "true", Map.of());
        Decision decision = CronJobSuspension.decideStart(paused, NOW);
        assertThat(decision.action(), is(Action.SKIPPED_NOT_OWNED));
        assertThat(decision.shouldWrite(), is(false));
    }

    @Test
    void start_ignores_a_recreated_uid() {
        CronJob rebuilt = cronJob(true, "true", ownedRecord("uid-original"));
        Decision decision = CronJobSuspension.decideStart(rebuilt, NOW);
        assertThat(decision.action(), is(Action.SKIPPED_STALE_UID));
        assertThat(decision.shouldWrite(), is(false));
    }

    @Test
    void start_does_not_override_a_manual_unsuspend() {
        CronJob manuallyResumed = cronJob(false, "true", ownedRecord(UID));
        Decision decision = CronJobSuspension.decideStart(manuallyResumed, NOW);
        assertThat(decision.action(), is(Action.CLEAR_STALE_RECORD));
        assertThat(decision.shouldWrite(), is(true));

        Map<String, Object> patch = decision.patch();
        assertThat(patch.containsKey("spec"), is(false));
        @SuppressWarnings("unchecked")
        Map<String, Object> metadata = (Map<String, Object>) patch.get("metadata");
        @SuppressWarnings("unchecked")
        Map<String, String> annotations = (Map<String, String>) metadata.get("annotations");
        assertThat(annotations.values(), contains(nullValue(), nullValue(), nullValue()));
    }

    @Test
    void start_without_suspension_is_a_no_op() {
        Decision decision = CronJobSuspension.decideStart(optedIn(false), NOW);
        assertThat(decision.action(), is(Action.SKIPPED_NOT_SUSPENDED));
        assertThat(decision.shouldWrite(), is(false));
    }

    @Test
    void start_resume_patch_carries_the_resource_version_precondition() {
        CronJob managed = cronJob(true, "true", ownedRecord(UID));
        @SuppressWarnings("unchecked")
        Map<String, Object> metadata =
                (Map<String, Object>) CronJobSuspension.decideStart(managed, NOW).patch().get("metadata");
        assertThat(metadata.get("resourceVersion"), is(RV));
    }

    // ------------------------------------------------------------------
    // Patch object adapter
    // ------------------------------------------------------------------

    @Test
    void as_patch_object_serialises_only_the_agreed_fields() {
        Decision decision = CronJobSuspension.decideStop(optedIn(false), NOW);
        CronJob patchObject = CronJobSuspension.asPatchObject("report", decision.patch());

        assertThat(patchObject.getMetadata().getName(), is("report"));
        assertThat(patchObject.getMetadata().getResourceVersion(), is(RV));
        assertThat(patchObject.getMetadata().getAnnotations()
                .get(CronJobSuspension.SUSPENDED_UID_ANNOTATION), is(UID));
        CronJobSpec spec = patchObject.getSpec();
        assertThat(spec.getSuspend(), is(true));
        assertThat(spec.getSchedule(), is(nullValue()));
        assertThat(spec.getConcurrencyPolicy(), is(nullValue()));
    }

    @Test
    void clear_record_patch_object_has_no_spec() {
        CronJob manuallyResumed = cronJob(false, "true", ownedRecord(UID));
        Decision decision = CronJobSuspension.decideStart(manuallyResumed, NOW);
        CronJob patchObject = CronJobSuspension.asPatchObject("report", decision.patch());
        assertThat(patchObject.getSpec(), is(nullValue()));
    }
}
