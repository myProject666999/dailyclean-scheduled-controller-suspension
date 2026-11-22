package fr.axa.openpaas.dailyclean.service;

import io.fabric8.kubernetes.api.model.autoscaling.v2.CrossVersionObjectReference;
import io.fabric8.kubernetes.api.model.autoscaling.v2.HorizontalPodAutoscaler;
import io.fabric8.kubernetes.api.model.autoscaling.v2.HorizontalPodAutoscalerCondition;
import io.fabric8.kubernetes.api.model.autoscaling.v2.HorizontalPodAutoscalerSpec;

import java.util.ArrayList;
import java.util.List;
import java.util.Objects;
import java.util.Optional;
import java.util.Set;
import java.util.TreeSet;

/**
 * The control boundary around restoring a scale target that an
 * HorizontalPodAutoscaler also governs.
 *
 * <p>HPAs have no generic {@code suspend}. DailyClean therefore never fakes
 * one, never deletes/recreates an HPA and never assumes every cluster accepts
 * {@code minReplicas: 0}. The only convention relied upon is the upstream one:
 * a scale target at {@code spec.replicas == 0} with a non-zero HPA
 * {@code minReplicas} is treated as autoscaling-disabled by the HPA controller.
 *
 * <p>Restoration clamps a candidate into the HPA's <em>current</em> range:
 * {@code [min=3,max=10], 7 -> 7}, {@code ..., 20 -> 10}, {@code no candidate -> 3}.
 *
 * <p>Ownership is decided from {@code scaleTargetRef} group/kind/name, never
 * from labels. Multiple HPAs pointing at one target is a conflict: intersecting
 * their ranges is not proof that several controllers can safely share it, so no
 * silent takeover occurs.
 */
public final class HpaBoundary {

    public static final String SCALING_DISABLED_REASON = "ScalingDisabled";
    public static final Set<String> SUPPORTED_TARGET_KINDS = Set.of("Deployment", "StatefulSet");

    private HpaBoundary() {
    }

    public enum Support { SUPPORTED, RESTRICTED, UNSUPPORTED }

    public enum Decision { ALLOW, RESTRICTED, CONFLICT, UNSUPPORTED }

    public enum Observation { SCALING_ACTIVE, STILL_DISABLED, UNKNOWN }

    /**
     * A scale target identity resolved from an HPA's {@code scaleTargetRef}.
     */
    public record TargetRef(String group, String kind, String name) {
        public static TargetRef from(HorizontalPodAutoscaler hpa) {
            HorizontalPodAutoscalerSpec spec = hpa.getSpec();
            if (spec == null || spec.getScaleTargetRef() == null) {
                return null;
            }
            CrossVersionObjectReference ref = spec.getScaleTargetRef();
            if (ref.getKind() == null || ref.getName() == null) {
                return null;
            }
            return new TargetRef(splitGroup(ref.getApiVersion()), ref.getKind(), ref.getName());
        }
    }

    /**
     * The evaluated boundary for one target.
     */
    public record Result(
            Decision decision,
            TargetRef target,
            List<String> hpaNames,
            Support support,
            Integer replicas,
            String reason,
            int[] previewIntersection) {

        public boolean mayRestore() {
            return decision == Decision.ALLOW || decision == Decision.RESTRICTED;
        }

        public boolean claimsFullControl() {
            return decision == Decision.ALLOW;
        }
    }

    public record ObservationResult(String name, Observation observation, String detail) {
    }

    // -----------------------------------------------------------------

    public static String splitGroup(String apiVersion) {
        if (apiVersion == null || apiVersion.isEmpty()) {
            return "";
        }
        int slash = apiVersion.indexOf('/');
        return slash < 0 ? "" : apiVersion.substring(0, slash);
    }

    public static List<HorizontalPodAutoscaler> hpasForTarget(
            List<HorizontalPodAutoscaler> hpas, TargetRef target) {
        List<HorizontalPodAutoscaler> matches = new ArrayList<>();
        for (HorizontalPodAutoscaler hpa : hpas) {
            TargetRef ref = TargetRef.from(hpa);
            if (ref != null && ref.equals(target)) {
                matches.add(hpa);
            }
        }
        return matches;
    }

    public static int clampReplicas(Integer candidate, int minimum, int maximum) {
        if (candidate == null) {
            return Math.max(minimum, 1);
        }
        if (candidate < minimum) {
            return minimum;
        }
        if (candidate > maximum) {
            return maximum;
        }
        return candidate;
    }

    public static int minReplicas(HorizontalPodAutoscaler hpa) {
        Integer value = hpa.getSpec() == null ? null : hpa.getSpec().getMinReplicas();
        return value == null ? 1 : value;
    }

    public static Integer maxReplicas(HorizontalPodAutoscaler hpa) {
        return hpa.getSpec() == null ? null : hpa.getSpec().getMaxReplicas();
    }

    public static Support classifySupport(HorizontalPodAutoscaler hpa, TargetRef target) {
        if (!SUPPORTED_TARGET_KINDS.contains(target.kind())) {
            return Support.UNSUPPORTED;
        }
        if (minReplicas(hpa) <= 0) {
            return Support.RESTRICTED;
        }
        return Support.SUPPORTED;
    }

    public static Result evaluate(
            List<HorizontalPodAutoscaler> hpas, TargetRef target, Integer candidate) {
        List<HorizontalPodAutoscaler> matches = hpasForTarget(hpas, target);
        List<String> names = names(matches);

        if (matches.isEmpty()) {
            int replicas = candidate == null || candidate <= 0 ? 1 : candidate;
            return new Result(Decision.ALLOW, target, names, Support.SUPPORTED, replicas,
                    "no HPA targets this workload; restoring the candidate replicas", null);
        }

        if (matches.size() > 1) {
            int[] preview = intersect(matches);
            return new Result(Decision.CONFLICT, target, names, Support.RESTRICTED, null,
                    "multiple HPAs target the same workload (" + String.join(", ", names)
                            + "); refusing to take over. Intersecting their ranges is not proof "
                            + "that several scaling controllers can safely share one target",
                    preview);
        }

        HorizontalPodAutoscaler hpa = matches.get(0);
        Support support = classifySupport(hpa, target);
        if (support == Support.UNSUPPORTED) {
            String reason = !SUPPORTED_TARGET_KINDS.contains(target.kind())
                    ? "scaleTargetRef kind '" + target.kind() + "' is not managed by dailyclean"
                    : "HPA minReplicas or range is not restorable";
            return new Result(Decision.UNSUPPORTED, target, names, support, null, reason, null);
        }

        int minimum = minReplicas(hpa);
        Integer maximum = maxReplicas(hpa);
        if (maximum == null || maximum < 1 || minimum < 0 || maximum < minimum) {
            return new Result(Decision.UNSUPPORTED, target, names, Support.UNSUPPORTED, null,
                    "HPA range is not restorable: minReplicas=" + minimum
                            + ", maxReplicas=" + maximum, null);
        }

        int replicas = clampReplicas(candidate, minimum, maximum);
        if (support == Support.RESTRICTED) {
            return new Result(Decision.RESTRICTED, target, names, support, replicas,
                    "HPA minReplicas is 0, so the target=0 implicit-disable convention does not "
                            + "apply and the HPA may scale the target back up on its own", null);
        }
        return new Result(Decision.ALLOW, target, names, support, replicas,
                "HPA will resume from the restored replicas (clamped to ["
                        + minimum + ", " + maximum + "])", null);
    }

    public static Optional<String> scalingDisabledReason(HorizontalPodAutoscaler hpa) {
        if (hpa.getStatus() == null || hpa.getStatus().getConditions() == null) {
            return Optional.empty();
        }
        for (HorizontalPodAutoscalerCondition condition : hpa.getStatus().getConditions()) {
            if ("ScalingActive".equals(condition.getType())) {
                return Optional.ofNullable(condition.getReason());
            }
        }
        return Optional.empty();
    }

    public static ObservationResult observeAfterRestore(HorizontalPodAutoscaler hpa) {
        String name = hpa.getMetadata() == null ? null : hpa.getMetadata().getName();
        if (hpa.getStatus() == null || hpa.getStatus().getConditions() == null) {
            return new ObservationResult(name, Observation.UNKNOWN,
                    "HPA has no ScalingActive condition yet");
        }
        for (HorizontalPodAutoscalerCondition condition : hpa.getStatus().getConditions()) {
            if (!"ScalingActive".equals(condition.getType())) {
                continue;
            }
            if (SCALING_DISABLED_REASON.equals(condition.getReason())) {
                return new ObservationResult(name, Observation.STILL_DISABLED,
                        "HPA still reports ScalingDisabled; the controller has not observed "
                                + "the restored replica count yet");
            }
            if ("True".equals(condition.getStatus())) {
                return new ObservationResult(name, Observation.SCALING_ACTIVE,
                        "HPA scaling is active again");
            }
            return new ObservationResult(name, Observation.UNKNOWN,
                    "ScalingActive=" + condition.getStatus() + " reason=" + condition.getReason());
        }
        return new ObservationResult(name, Observation.UNKNOWN,
                "HPA has no ScalingActive condition yet");
    }

    private static List<String> names(List<HorizontalPodAutoscaler> hpas) {
        TreeSet<String> names = new TreeSet<>();
        for (HorizontalPodAutoscaler hpa : hpas) {
            if (hpa.getMetadata() != null && hpa.getMetadata().getName() != null) {
                names.add(hpa.getMetadata().getName());
            }
        }
        return new ArrayList<>(names);
    }

    private static int[] intersect(List<HorizontalPodAutoscaler> hpas) {
        int low = Integer.MIN_VALUE;
        int high = Integer.MAX_VALUE;
        for (HorizontalPodAutoscaler hpa : hpas) {
            Integer maximum = maxReplicas(hpa);
            if (maximum == null) {
                return null;
            }
            low = Math.max(low, minReplicas(hpa));
            high = Math.min(high, maximum);
        }
        return low > high ? null : new int[] {low, high};
    }

    /**
     * The kind/name a Workload must expose for a target so a UI can flag the
     * HPA boundary. Kept for callers that key on workload identity.
     */
    public static boolean targetsWorkload(TargetRef target, String workloadName) {
        return Objects.equals(target.name(), workloadName);
    }
}
