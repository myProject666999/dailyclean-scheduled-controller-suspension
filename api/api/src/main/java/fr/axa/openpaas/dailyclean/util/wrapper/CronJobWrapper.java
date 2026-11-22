package fr.axa.openpaas.dailyclean.util.wrapper;

import fr.axa.openpaas.dailyclean.model.Workload;
import io.fabric8.kubernetes.api.model.ObjectMeta;
import io.fabric8.kubernetes.api.model.PodTemplateSpec;
import io.fabric8.kubernetes.api.model.batch.v1.CronJob;
import io.fabric8.kubernetes.api.model.batch.v1.CronJobSpec;
import io.fabric8.kubernetes.api.model.batch.v1.CronJobStatus;

/**
 * Wrapper around a managed CronJob.
 *
 * <p>A CronJob does not have replicas: DailyClean reasons about whether its
 * triggers are suspended ({@code spec.suspend}). To keep the {@code Workload}
 * payload uniform, {@link #getReplicas()} and {@link #getReadyReplicas()} are a
 * binary projection (1 when triggers are active, 0 when suspended) that is
 * <em>derived from the suspension</em>, never from a replica count. The
 * explicit {@link #getSuspended()} flag is authoritative for status.
 */
public class CronJobWrapper implements IWorkloadWrapper<CronJobStatus, CronJobSpec> {

    private final CronJob workload;

    public CronJobWrapper(CronJob workload) {
        this.workload = workload;
    }

    @Override
    public ObjectMeta getMetadata() {
        return workload.getMetadata();
    }

    @Override
    public Workload.TypeEnum getType() {
        return Workload.TypeEnum.CRONJOB;
    }

    @Override
    public CronJobStatus getStatus() {
        return workload.getStatus();
    }

    @Override
    public boolean isSuspended() {
        return workload.getSpec() != null && Boolean.TRUE.equals(workload.getSpec().getSuspend());
    }

    @Override
    public int getReplicas() {
        return isSuspended() ? 0 : 1;
    }

    @Override
    public int getReadyReplicas() {
        return getReplicas();
    }

    @Override
    public CronJobSpec getSpec() {
        return workload.getSpec();
    }

    @Override
    public PodTemplateSpec getPodTemplateSpec() {
        CronJobSpec spec = getSpec();
        if (spec == null || spec.getJobTemplate() == null || spec.getJobTemplate().getSpec() == null) {
            return null;
        }
        return spec.getJobTemplate().getSpec().getTemplate();
    }

    /**
     * CronJobs are managed only when explicitly opted in. The historical
     * default (participate) that applies to Deployments must not silently
     * start suspending user CronJobs after an upgrade.
     */
    @Override
    public boolean isDailycleaned(Boolean dailycleaned) {
        return dailycleaned != null && dailycleaned;
    }
}
