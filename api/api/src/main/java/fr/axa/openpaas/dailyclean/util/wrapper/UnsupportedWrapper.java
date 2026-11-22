package fr.axa.openpaas.dailyclean.util.wrapper;

import fr.axa.openpaas.dailyclean.model.Workload;
import io.fabric8.kubernetes.api.model.ObjectMeta;
import io.fabric8.kubernetes.api.model.PodTemplateSpec;

/**
 * Represents an object DailyClean can list but cannot manage with a replica or
 * suspension based hand-over.
 *
 * <p>It is surfaced to clients with {@link Workload.TypeEnum#UNSUPPORTED} so a
 * UI never invents a replica count or pretends it suspended something. Typical
 * cases: an opted-in CronJob missing {@code spec.schedule} or
 * {@code metadata.uid}, or a scale target whose kind has no supported
 * convention.
 */
public class UnsupportedWrapper implements IWorkloadWrapper<Object, Object> {

    private final ObjectMeta metadata;
    private final String reason;

    public UnsupportedWrapper(ObjectMeta metadata, String reason) {
        this.metadata = metadata;
        this.reason = reason;
    }

    @Override
    public ObjectMeta getMetadata() {
        return metadata;
    }

    @Override
    public Workload.TypeEnum getType() {
        return Workload.TypeEnum.UNSUPPORTED;
    }

    @Override
    public Object getStatus() {
        return null;
    }

    @Override
    public int getReplicas() {
        return 0;
    }

    @Override
    public int getReadyReplicas() {
        return 0;
    }

    @Override
    public Object getSpec() {
        return null;
    }

    @Override
    public PodTemplateSpec getPodTemplateSpec() {
        return null;
    }

    @Override
    public boolean isSuspended() {
        return false;
    }

    @Override
    public boolean isDailycleaned(Boolean dailycleaned) {
        return dailycleaned != null && dailycleaned;
    }

    public String getReason() {
        return reason;
    }
}
