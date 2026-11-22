package fr.axa.openpaas.dailyclean.configuration;

import fr.axa.openpaas.dailyclean.model.Container;
import fr.axa.openpaas.dailyclean.model.Error;
import fr.axa.openpaas.dailyclean.model.Resource;
import fr.axa.openpaas.dailyclean.model.StartStopResponse;
import fr.axa.openpaas.dailyclean.model.Status;
import fr.axa.openpaas.dailyclean.model.TimeRange;
import fr.axa.openpaas.dailyclean.model.Workload;
import io.quarkus.runtime.annotations.RegisterForReflection;

@RegisterForReflection(targets={
        Container.class,
        Error.class,
        Resource.class,
        StartStopResponse.class,
        Status.class,
        TimeRange.class,
        Workload.class})
public class MyReflectionConfiguration {
}
