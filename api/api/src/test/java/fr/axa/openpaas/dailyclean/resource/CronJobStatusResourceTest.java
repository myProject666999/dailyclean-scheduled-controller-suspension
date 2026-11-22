package fr.axa.openpaas.dailyclean.resource;

import fr.axa.openpaas.dailyclean.model.Status;
import fr.axa.openpaas.dailyclean.model.Workload;
import io.fabric8.kubernetes.api.model.ObjectMeta;
import io.fabric8.kubernetes.api.model.batch.v1.CronJob;
import io.fabric8.kubernetes.api.model.batch.v1.CronJobBuilder;
import io.fabric8.kubernetes.client.KubernetesClient;
import io.quarkus.test.junit.QuarkusTest;
import io.quarkus.test.kubernetes.client.KubernetesServer;
import io.quarkus.test.kubernetes.client.KubernetesTestServer;
import io.quarkus.test.kubernetes.client.WithKubernetesTestServer;
import io.restassured.response.Response;
import io.restassured.response.ValidatableResponse;
import io.restassured.response.ValidatableResponseOptions;
import jakarta.ws.rs.core.MediaType;
import org.apache.http.HttpStatus;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.util.Arrays;
import java.util.HashMap;

import static fr.axa.openpaas.dailyclean.model.Status.StateEnum.IN_PROGRESS;
import static fr.axa.openpaas.dailyclean.model.Status.StateEnum.STARTED;
import static fr.axa.openpaas.dailyclean.model.Status.StateEnum.STOPPED;
import static io.restassured.RestAssured.given;
import static org.hamcrest.CoreMatchers.is;
import static org.hamcrest.Matchers.nullValue;

/**
 * Verifies that /status judges CronJobs by suspension (spec.suspend), never by
 * a reinterpreted replica count, and that CronJobs are listed only when they
 * opted in.
 */
@WithKubernetesTestServer
@QuarkusTest
public class CronJobStatusResourceTest {

    private static final String DAILYCLEAN_LABEL_NAME = "axa.com/dailyclean";
    private static final String CRONJOB_JSON_PATH = "workloads.find { workload -> workload.id == '%s' }.%s";

    @KubernetesTestServer
    KubernetesServer mockServer;

    @BeforeEach
    public void before() {
        KubernetesClient client = mockServer.getClient();
        final String namespace = client.getNamespace();
        client.apps().deployments().inNamespace(namespace).delete();
        client.apps().statefulSets().inNamespace(namespace).delete();
        client.batch().v1().cronjobs().inNamespace(namespace).delete();
    }

    @Test
    public void optedInRunningCronJobCountsAsStarted() {
        createCronJob("report", false, "true");

        ValidatableResponse response = getStatus();

        response.statusCode(HttpStatus.SC_OK)
                .body("state", is(STARTED.value()));
        assertCronJob(response, "report", false, true);
    }

    @Test
    public void optedInSuspendedCronJobCountsAsStopped() {
        createCronJob("report", true, "true");

        ValidatableResponse response = getStatus();

        response.statusCode(HttpStatus.SC_OK)
                .body("state", is(STOPPED.value()));
        assertCronJob(response, "report", true, true);
    }

    @Test
    public void unlabelledCronJobIsNotListed() {
        createCronJob("plain", true, null);

        ValidatableResponse response = getStatus();
        response.statusCode(HttpStatus.SC_OK)
                .body("workloads.size()", is(0))
                .body("state", is(STOPPED.value()));
    }

    @Test
    public void explicitlyDisabledCronJobIsNotListed() {
        createCronJob("disabled", false, "false");

        getStatus().statusCode(HttpStatus.SC_OK)
                .body("workloads.size()", is(0));
    }

    @Test
    public void dailycleanControlCronJobsAreNeverListed() {
        // Even if mislabelled, DailyClean's own schedulers must not be treated
        // as user workloads (suspending them would disable DailyClean).
        createCronJob(fr.axa.openpaas.dailyclean.util.KubernetesUtils.getCronName(
                fr.axa.openpaas.dailyclean.service.KubernetesArgument.STOP), false, "true");
        createCronJob(fr.axa.openpaas.dailyclean.util.KubernetesUtils.getCronName(
                fr.axa.openpaas.dailyclean.service.KubernetesArgument.START), false, "true");

        getStatus().statusCode(HttpStatus.SC_OK)
                .body("workloads.size()", is(0));
    }

    @Test
    public void mixedDeploymentAndCronJobReportInProgress() {
        KubernetesClient client = mockServer.getClient();
        final String namespace = client.getNamespace();
        io.fabric8.kubernetes.api.model.apps.Deployment deployment = new io.fabric8.kubernetes.api.model.apps.Deployment();
        deployment.setMetadata(new ObjectMeta());
        deployment.getMetadata().setName("api");
        deployment.getMetadata().setLabels(new HashMap<>());
        deployment.getMetadata().getLabels().put(DAILYCLEAN_LABEL_NAME, "true");
        io.fabric8.kubernetes.api.model.apps.DeploymentStatus status =
                new io.fabric8.kubernetes.api.model.apps.DeploymentStatus();
        status.setReplicas(0);
        status.setReadyReplicas(0);
        deployment.setStatus(status);
        io.fabric8.kubernetes.api.model.apps.DeploymentSpec spec =
                new io.fabric8.kubernetes.api.model.apps.DeploymentSpec();
        spec.setTemplate(new io.fabric8.kubernetes.api.model.PodTemplateSpec());
        deployment.setSpec(spec);
        client.apps().deployments().inNamespace(namespace).create(deployment);

        createCronJob("report", false, "true");

        getStatus().statusCode(HttpStatus.SC_OK)
                .body("state", is(IN_PROGRESS.value()))
                .body("workloads.size()", is(2));
    }

    private ValidatableResponse getStatus() {
        return given()
                .contentType(MediaType.APPLICATION_JSON)
                .when()
                .get("/status")
                .then();
    }

    private void assertCronJob(ValidatableResponse response, String id,
                               boolean suspended, boolean isDailycleaned) {
        response.body(path(id, "type"), is(Workload.TypeEnum.CRONJOB.value()))
                .body(path(id, "suspended"), is(suspended))
                .body(path(id, "isDailycleaned"), is(isDailycleaned))
                .body(path(id, "annotations"), is(new HashMap<>()));
    }

    private static String path(String id, String fieldName) {
        return String.format(CRONJOB_JSON_PATH, id, fieldName);
    }

    private void createCronJob(String name, boolean suspend, String dailyclean) {
        KubernetesClient client = mockServer.getClient();
        final String namespace = client.getNamespace();
        CronJobBuilder builder = new CronJobBuilder()
                .withNewMetadata().withName(name).endMetadata()
                .withNewSpec()
                    .withSchedule("0 2 * * *")
                    .withSuspend(suspend)
                    .endSpec();
        CronJob cronJob = builder.build();
        if (dailyclean != null) {
            cronJob.getMetadata().getLabels().put(DAILYCLEAN_LABEL_NAME, dailyclean);
        }
        client.batch().v1().cronjobs().inNamespace(namespace).create(cronJob);
    }
}
