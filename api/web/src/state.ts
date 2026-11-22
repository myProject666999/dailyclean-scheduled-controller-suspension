import { State, Workload } from "./types/api";

/** A CronJob's "started"/"stopped" is its suspension, never its replicas. */
export const isScheduled = (workload: Workload) =>
  workload.type === "CRONJOB";

export const isSupported = (workload: Workload) =>
  workload.type !== "UNSUPPORTED";

export const computeState = (workloads: Workload[]): State => {
  const managed = workloads.filter(
    (w) => !computeIsFunction(w) && isSupported(w) && computeIsDailyCleaned(w),
  );

  // Replica-based workloads keep the historical rules unchanged.
  const replicaInProgress = managed.find(
    (w) => !isScheduled(w) && w.current === 0 && w.target > 0,
  );
  if (replicaInProgress) return "IN_PROGRESS";

  const stopped = managed.find((w) =>
    isScheduled(w) ? Boolean(w.suspended) : w.target === 0,
  );
  return stopped != null ? "STOPPED" : "STARTED";
};

export const computeIsFunction = (workload: Workload) => {
  return (
    workload &&
    workload.labels &&
    workload.labels["axa.com/function"] === "true"
  );
};

export const computeIsDailyCleaned = (workload: Workload) => {
  return workload.isDailycleaned;
};
