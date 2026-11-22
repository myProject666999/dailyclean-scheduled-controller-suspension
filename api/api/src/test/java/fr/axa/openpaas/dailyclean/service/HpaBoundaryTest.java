package fr.axa.openpaas.dailyclean.service;

import io.fabric8.kubernetes.api.model.autoscaling.v2.CrossVersionObjectReference;
import io.fabric8.kubernetes.api.model.autoscaling.v2.HorizontalPodAutoscaler;
import io.fabric8.kubernetes.api.model.autoscaling.v2.HorizontalPodAutoscalerBuilder;
import io.fabric8.kubernetes.api.model.autoscaling.v2.HorizontalPodAutoscalerCondition;
import org.junit.jupiter.api.Test;

import java.util.List;

import static fr.axa.openpaas.dailyclean.service.HpaBoundary.Decision;
import static fr.axa.openpaas.dailyclean.service.HpaBoundary.Observation;
import static fr.axa.openpaas.dailyclean.service.HpaBoundary.Support;
import static fr.axa.openpaas.dailyclean.service.HpaBoundary.TargetRef;
import static org.hamcrest.MatcherAssert.assertThat;
import static org.hamcrest.Matchers.contains;
import static org.hamcrest.Matchers.is;
import static org.hamcrest.Matchers.nullValue;

class HpaBoundaryTest {

    private static final TargetRef DEPLOYMENT = new TargetRef("apps", "Deployment", "web");

    private static HorizontalPodAutoscaler hpa(
            String name, String kind, String target, int min, int max) {
        return hpa(name, kind, target, min, max, "apps/v1");
    }

    private static HorizontalPodAutoscaler hpa(
            String name, String kind, String target, int min, int max, String apiVersion) {
        return new HorizontalPodAutoscalerBuilder()
                .withNewMetadata().withName(name).withNamespace("test").endMetadata()
                .withNewSpec()
                    .withNewScaleTargetRef()
                        .withApiVersion(apiVersion)
                        .withKind(kind)
                        .withName(target)
                        .endScaleTargetRef()
                    .withMinReplicas(min)
                    .withMaxReplicas(max)
                    .endSpec()
                .build();
    }

    // ------------------------------------------------------------------
    // Target identity
    // ------------------------------------------------------------------

    @Test
    void target_ref_is_read_from_scale_target_ref_not_labels() {
        HorizontalPodAutoscaler withLabels = new HorizontalPodAutoscalerBuilder(
                hpa("web-hpa", "Deployment", "web", 3, 10))
                .editMetadata().addToLabels("app", "unrelated").endMetadata()
                .build();
        TargetRef ref = TargetRef.from(withLabels);
        assertThat(ref, is(new TargetRef("apps", "Deployment", "web")));
    }

    @Test
    void core_group_is_empty() {
        assertThat(HpaBoundary.splitGroup("v1"), is(""));
        assertThat(HpaBoundary.splitGroup("apps/v1"), is("apps"));
    }

    @Test
    void kind_disambiguates_same_name() {
        List<HorizontalPodAutoscaler> hpas = List.of(hpa("web-deploy", "Deployment", "web", 1, 5));
        assertThat(HpaBoundary.hpasForTarget(hpas, DEPLOYMENT).size(), is(1));
        assertThat(HpaBoundary.hpasForTarget(
                hpas, new TargetRef("apps", "StatefulSet", "web")).size(), is(0));
    }

    // ------------------------------------------------------------------
    // Clamping
    // ------------------------------------------------------------------

    @Test
    void clamp_worked_examples() {
        assertThat(HpaBoundary.clampReplicas(7, 3, 10), is(7));
        assertThat(HpaBoundary.clampReplicas(20, 3, 10), is(10));
        assertThat(HpaBoundary.clampReplicas(null, 3, 10), is(3));
    }

    @Test
    void clamp_below_minimum_and_zero_minimum() {
        assertThat(HpaBoundary.clampReplicas(1, 3, 10), is(3));
        assertThat(HpaBoundary.clampReplicas(null, 0, 10), is(1));
        assertThat(HpaBoundary.clampReplicas(null, 2, 10), is(2));
    }

    // ------------------------------------------------------------------
    // Boundary evaluation
    // ------------------------------------------------------------------

    @Test
    void supported_hpa_allows_and_clamps() {
        HpaBoundary.Result result = HpaBoundary.evaluate(
                List.of(hpa("web-hpa", "Deployment", "web", 3, 10)), DEPLOYMENT, 7);
        assertThat(result.decision(), is(Decision.ALLOW));
        assertThat(result.support(), is(Support.SUPPORTED));
        assertThat(result.replicas(), is(7));
        assertThat(result.mayRestore(), is(true));
        assertThat(result.claimsFullControl(), is(true));
    }

    @Test
    void supported_hpa_caps_high_candidate_and_uses_min_without_one() {
        List<HorizontalPodAutoscaler> hpas = List.of(hpa("web-hpa", "Deployment", "web", 3, 10));
        assertThat(HpaBoundary.evaluate(hpas, DEPLOYMENT, 20).replicas(), is(10));
        assertThat(HpaBoundary.evaluate(hpas, DEPLOYMENT, null).replicas(), is(3));
    }

    @Test
    void no_hpa_passes_candidate_through() {
        HpaBoundary.Result result = HpaBoundary.evaluate(List.of(), DEPLOYMENT, 4);
        assertThat(result.decision(), is(Decision.ALLOW));
        assertThat(result.replicas(), is(4));
        assertThat(HpaBoundary.evaluate(List.of(), DEPLOYMENT, null).replicas(), is(1));
    }

    @Test
    void min_replicas_zero_is_restricted_not_faked() {
        HpaBoundary.Result result = HpaBoundary.evaluate(
                List.of(hpa("web-hpa", "Deployment", "web", 0, 10)), DEPLOYMENT, null);
        assertThat(result.decision(), is(Decision.RESTRICTED));
        assertThat(result.support(), is(Support.RESTRICTED));
        assertThat(result.claimsFullControl(), is(false));
        assertThat(result.mayRestore(), is(true));
        assertThat(result.replicas(), is(1));
    }

    @Test
    void unsupported_kind_is_rejected() {
        TargetRef rollout = new TargetRef("argoproj.io", "Rollout", "web");
        HpaBoundary.Result result = HpaBoundary.evaluate(
                List.of(hpa("web-hpa", "Rollout", "web", 2, 8, "argoproj.io/v1alpha1")),
                rollout, 4);
        assertThat(result.decision(), is(Decision.UNSUPPORTED));
        assertThat(result.mayRestore(), is(false));
        assertThat(result.replicas(), is(nullValue()));
    }

    @Test
    void multiple_hpas_conflict_with_preview_intersection() {
        List<HorizontalPodAutoscaler> hpas = List.of(
                hpa("web-a", "Deployment", "web", 2, 6),
                hpa("web-b", "Deployment", "web", 3, 10));
        HpaBoundary.Result result = HpaBoundary.evaluate(hpas, DEPLOYMENT, 4);
        assertThat(result.decision(), is(Decision.CONFLICT));
        assertThat(result.mayRestore(), is(false));
        assertThat(result.hpaNames(), contains("web-a", "web-b"));
        assertThat(result.previewIntersection(), is(new int[] {3, 6}));
    }

    @Test
    void multiple_hpas_with_empty_intersection_still_conflict() {
        List<HorizontalPodAutoscaler> hpas = List.of(
                hpa("a", "Deployment", "web", 2, 4),
                hpa("b", "Deployment", "web", 8, 10));
        HpaBoundary.Result result = HpaBoundary.evaluate(hpas, DEPLOYMENT, 4);
        assertThat(result.decision(), is(Decision.CONFLICT));
        assertThat(result.previewIntersection(), is(nullValue()));
    }

    @Test
    void invalid_range_is_unsupported() {
        HpaBoundary.Result result = HpaBoundary.evaluate(
                List.of(hpa("bad", "Deployment", "web", 10, 3)), DEPLOYMENT, 5);
        assertThat(result.decision(), is(Decision.UNSUPPORTED));
        assertThat(result.replicas(), is(nullValue()));
    }

    @Test
    void hpa_for_a_different_workload_is_ignored() {
        HpaBoundary.Result result = HpaBoundary.evaluate(
                List.of(hpa("other", "Deployment", "api", 2, 8)), DEPLOYMENT, 5);
        assertThat(result.decision(), is(Decision.ALLOW));
        assertThat(result.replicas(), is(5));
        assertThat(result.hpaNames().isEmpty(), is(true));
    }

    // ------------------------------------------------------------------
    // Observation
    // ------------------------------------------------------------------

    @Test
    void observation_reports_pending_controller_catch_up() {
        HorizontalPodAutoscaler hpa = new HorizontalPodAutoscalerBuilder(
                hpa("web-hpa", "Deployment", "web", 3, 10))
                .withNewStatus()
                    .addNewCondition()
                        .withType("ScalingActive")
                        .withStatus("False")
                        .withReason(HpaBoundary.SCALING_DISABLED_REASON)
                        .endCondition()
                    .endStatus()
                .build();
        HpaBoundary.ObservationResult result = HpaBoundary.observeAfterRestore(hpa);
        assertThat(result.observation(), is(Observation.STILL_DISABLED));
        assertThat(result.name(), is("web-hpa"));
    }

    @Test
    void observation_reports_active_again() {
        HorizontalPodAutoscaler hpa = new HorizontalPodAutoscalerBuilder(
                hpa("web-hpa", "Deployment", "web", 3, 10))
                .withNewStatus()
                    .addNewCondition().withType("ScalingActive").withStatus("True")
                        .withReason("ValidMetricFound").endCondition()
                    .endStatus()
                .build();
        assertThat(HpaBoundary.observeAfterRestore(hpa).observation(),
                is(Observation.SCALING_ACTIVE));
    }

    @Test
    void observation_without_conditions_is_unknown() {
        HorizontalPodAutoscaler hpa = hpa("web-hpa", "Deployment", "web", 3, 10);
        assertThat(HpaBoundary.observeAfterRestore(hpa).observation(), is(Observation.UNKNOWN));
    }
}
