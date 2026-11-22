package fr.axa.openpaas.dailyclean.resource;

import fr.axa.openpaas.dailyclean.model.Status;
import fr.axa.openpaas.dailyclean.model.Workload;
import fr.axa.openpaas.dailyclean.service.KubernetesService;

import jakarta.ws.rs.GET;
import jakarta.ws.rs.Path;
import jakarta.ws.rs.Produces;
import jakarta.ws.rs.core.MediaType;

import java.util.List;

@Path("/status")
public class StatusResources {

    private final KubernetesService kubernetesService;

    public StatusResources(final KubernetesService kubernetesService) {
        this.kubernetesService = kubernetesService;
    }

    @GET
    @Produces(MediaType.APPLICATION_JSON)
    public Status getStatus() {
        Status status = new Status();
        status.setNamespace(kubernetesService.getNamespace());
        status.setWorkloads(kubernetesService.getWorkloads());

        var workloads = status.getWorkloads();
        long started = workloads.stream()
                .filter(workload -> Boolean.TRUE.equals(workload.getIsDailycleaned()) && isStarted(workload))
                .count();
        long stopped = workloads.stream()
                .filter(workload -> Boolean.TRUE.equals(workload.getIsDailycleaned()) && isStopped(workload))
                .count();

        Status.StateEnum state;
        if(started == 0) {
            state = Status.StateEnum.STOPPED;
        } else if(stopped == 0) {
            state = Status.StateEnum.STARTED;
        } else {
            state = Status.StateEnum.IN_PROGRESS;
        }
        status.setState(state);

        return status;
    }

    /**
     * A workload counts as started when its replicas are ready (Deployment,
     * StatefulSet) or when its triggers are not suspended (CronJob).
     */
    static boolean isStarted(Workload workload) {
        if (workload.getType() == Workload.TypeEnum.CRONJOB) {
            return !Boolean.TRUE.equals(workload.getSuspended());
        }
        return scaledOrUnknown(workload) && workload.getCurrent().intValue() >= 1;
    }

    /**
     * A workload counts as stopped when it has no ready replicas (Deployment,
     * StatefulSet) or its triggers are suspended (CronJob).
     */
    static boolean isStopped(Workload workload) {
        if (workload.getType() == Workload.TypeEnum.CRONJOB) {
            return Boolean.TRUE.equals(workload.getSuspended());
        }
        return scaledOrUnknown(workload) && workload.getCurrent().intValue() == 0;
    }

    private static boolean scaledOrUnknown(Workload workload) {
        return workload.getCurrent() != null
                && workload.getType() != Workload.TypeEnum.UNSUPPORTED;
    }

    /**
     * Exposed for callers that want to classify the list without re-deriving
     * the rules (e.g. tests, diagnostics).
     */
    public static Status.StateEnum stateOf(List<Workload> workloads) {
        long started = workloads.stream()
                .filter(workload -> Boolean.TRUE.equals(workload.getIsDailycleaned()) && isStarted(workload))
                .count();
        long stopped = workloads.stream()
                .filter(workload -> Boolean.TRUE.equals(workload.getIsDailycleaned()) && isStopped(workload))
                .count();
        if (started == 0) {
            return Status.StateEnum.STOPPED;
        }
        if (stopped == 0) {
            return Status.StateEnum.STARTED;
        }
        return Status.StateEnum.IN_PROGRESS;
    }
}
