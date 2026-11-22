from kubernetes import client, config
import os
import sys
import time

from dailyclean_ops.cluster import (
    KubernetesCronJobClient,
    KubernetesHpaReader,
    utc_now_rfc3339,
)
from dailyclean_ops.cronjob import apply_start, apply_stop, opted_in_names
from dailyclean_ops.restore import plan_restore_with_reader


def managed_cron_jobs(aApiClient, namespace):
    """Return the names of CronJobs explicitly opted in to DailyClean.

    Participation is opt-in (``axa.com/dailyclean: "true"``), unlike the
    historical Deployment default, so an upgrade never starts suspending
    previously unmanaged scheduled jobs.
    """
    cronjob_client = KubernetesCronJobClient(client.BatchV1Api(aApiClient), namespace)
    try:
        return opted_in_names(cronjob_client.list_cron_jobs())
    except client.exceptions.ApiException as e:
        print('Error while listing CronJobs (%s).\n' % e)
        return []


def suspend_cron_jobs(aApiClient, namespace):
    """Suspend (never delete) opted-in CronJobs; running Jobs are untouched."""
    cronjob_client = KubernetesCronJobClient(client.BatchV1Api(aApiClient), namespace)
    now = utc_now_rfc3339()
    outcomes = [apply_stop(cronjob_client, name, now) for name in managed_cron_jobs(aApiClient, namespace)]
    for outcome in outcomes:
        print('CronJob %s: %s %s' % (outcome.name, outcome.action.value, outcome.detail))
    return outcomes


def resume_cron_jobs(aApiClient, namespace):
    """Resume only the suspensions DailyClean still owns (same UID)."""
    cronjob_client = KubernetesCronJobClient(client.BatchV1Api(aApiClient), namespace)
    outcomes = [apply_start(cronjob_client, name) for name in managed_cron_jobs(aApiClient, namespace)]
    for outcome in outcomes:
        print('CronJob %s: %s %s' % (outcome.name, outcome.action.value, outcome.detail))
    return outcomes


def main():
    if len(sys.argv) > 2 and sys.argv[2] == 'local':
        aConfig = client.Configuration()
        config.load_kube_config(client_configuration=aConfig)
        aConfig.verify_ssl = False
        aApiClient = client.ApiClient(aConfig)
    else:
        config.load_incluster_config()
        aApiClient = client.ApiClient()

    namespace = os.environ.get('CURRENT_NAMESPACE')

    if sys.argv[1] == 'start':
        start(aApiClient, namespace)
    elif sys.argv[1] == 'stop':
        stop(aApiClient, namespace)

def manage_kustomizations_suspend_status(aApiClient, namespace, suspend):
    coreV1Api_instance = client.CoreV1Api(aApiClient)
    customObjectsApi_instance = client.CustomObjectsApi(aApiClient)

    try:
        flux_sa = coreV1Api_instance.read_namespaced_service_account(name='flux', namespace=namespace)
    except client.exceptions.ApiException as e:
        print('Service account flux not found or error (%s).\n' % e)
        return

    print('Service account flux found in namespace %s' % namespace)

    if not (flux_sa.metadata.labels and 'kustomize.toolkit.fluxcd.io/namespace' in flux_sa.metadata.labels):
        print('Label kustomize.toolkit.fluxcd.io/namespace not found on flux service account')
        return

    flux_namespace = flux_sa.metadata.labels['kustomize.toolkit.fluxcd.io/namespace']
    print('Label kustomize.toolkit.fluxcd.io/namespace: %s' % flux_namespace)

    # List and update Kustomization objects
    try:
        kustomizations = customObjectsApi_instance.list_namespaced_custom_object(
            group='kustomize.toolkit.fluxcd.io',
            version='v1',
            namespace=flux_namespace,
            plural='kustomizations'
        )
        print('\nKustomization objects targeting namespace %s:' % namespace)

        for kustomization in kustomizations.get('items', []):
            target_namespace = kustomization.get('spec', {}).get('targetNamespace', '')
            if target_namespace != namespace:
                continue

            kustomization_name = kustomization['metadata']['name']
            print('  - %s' % kustomization_name)

            current_suspend = kustomization.get('spec', {}).get('suspend', False)
            needs_update = (suspend and not current_suspend) or (not suspend and current_suspend)

            if not needs_update:
                continue

            action = 'suspending' if suspend else 'unsuspending'
            status = 'running' if not current_suspend else 'suspended'
            print('    Kustomization %s is %s, %s...' % (kustomization_name, status, action))

            try:
                patch_body = {'spec': {'suspend': True if suspend else None}}
                customObjectsApi_instance.patch_namespaced_custom_object(
                    group='kustomize.toolkit.fluxcd.io',
                    version='v1',
                    namespace=flux_namespace,
                    plural='kustomizations',
                    name=kustomization_name,
                    body=patch_body
                )
                print('    Kustomization %s %s successfully' % (kustomization_name, 'suspended' if suspend else 'unsuspended'))
            except client.exceptions.ApiException as e:
                print('    Error while %s Kustomization %s (%s)' % (action, kustomization_name, e))

    except client.exceptions.ApiException as e:
        print('Error while listing Kustomizations (%s).\n' % e)


def start(aApiClient, namespace):
    appsV1Api_instance = client.AppsV1Api(aApiClient)
    hpa_reader = KubernetesHpaReader(client.AutoscalingV2Api(aApiClient), namespace)

    manage_kustomizations_suspend_status(aApiClient, namespace, suspend=False)

    # Restore the workloads first; only resume CronJobs once that step is done,
    # so a partially restored namespace never has its schedulers firing again
    # while its replicas are still down.
    start_deployments(appsV1Api_instance, namespace, hpa_reader)
    resume_cron_jobs(aApiClient, namespace)


def start_deployments(appsV1Api_instance, namespace, hpa_reader):
    try:
        nodailyclean_deployments = appsV1Api_instance.list_namespaced_deployment(namespace=namespace, watch=False, label_selector='axa.com/dailyclean=false')
    except client.exceptions.ApiException as e:
        print('Error while listing disabled dailyclean Deployments (%s).\n' % e)
    else:
        for deployment in nodailyclean_deployments.items:
            print('Dailyclean disabled for deployment %s.' % deployment.metadata.name)

    try:
        deployments = appsV1Api_instance.list_namespaced_deployment(namespace=namespace, watch=False, label_selector='axa.com/dailyclean!=false')
    except client.exceptions.ApiException as e:
        print('Error while listing Deployments (%s).\n' % e)
    else:
        for deployment in deployments.items:
            plan = plan_restore_with_reader(
                hpa_reader, 'Deployment', deployment.metadata.name, None, log=print)
            if not plan.should_restore:
                continue
            deployment.spec.replicas = plan.replicas
            appsV1Api_instance.patch_namespaced_deployment(namespace=namespace, name=deployment.metadata.name, body=deployment)
            print('Deployment %s replicas set to %d.' % (deployment.metadata.name, plan.replicas))

    try:
        stateful_sets = appsV1Api_instance.list_namespaced_stateful_set(namespace=namespace, watch=False, label_selector='axa.com/dailyclean=true')
    except client.exceptions.ApiException as e:
        print('Error while listing StatefulSet (%s).\n' % e)
    else:
        for stateful_set in stateful_sets.items:
            plan = plan_restore_with_reader(
                hpa_reader, 'StatefulSet', stateful_set.metadata.name, None, log=print)
            if not plan.should_restore:
                continue
            stateful_set.spec.replicas = plan.replicas
            appsV1Api_instance.patch_namespaced_stateful_set(namespace=namespace, name=stateful_set.metadata.name, body=stateful_set)
            print('StatefulSet %s replicas set to %d.' % (stateful_set.metadata.name, plan.replicas))


def stop(aApiClient, namespace):
    appsV1Api_instance = client.AppsV1Api(aApiClient)

    manage_kustomizations_suspend_status(aApiClient, namespace, suspend=True)

    # Suspend opt-in CronJobs first so no new trigger fires during teardown.
    # This does not terminate Jobs that are already running, and it never
    # deletes jobs or rewrites schedules; see dailyclean_ops.cronjob.
    suspend_cron_jobs(aApiClient, namespace)

    try:
        appsV1Api_instance.read_namespaced_deployment(namespace=namespace, name='flux')
    except client.exceptions.ApiException as e:
        print('No flux deployed. Skipping')
    else:
        while appsV1Api_instance.read_namespaced_deployment(namespace=namespace, name='flux').spec.replicas != 0:
            print('Trying to set deployment flux replicas to 0.')
            flux_deployment = appsV1Api_instance.read_namespaced_deployment(namespace=namespace, name='flux')
            flux_deployment.spec.replicas = 0
            appsV1Api_instance.patch_namespaced_deployment(namespace=namespace,name='flux', body=flux_deployment)
            time.sleep(10)
        print('Deployment flux replicas set to 0.')

    try:
        helm_deployment = appsV1Api_instance.read_namespaced_deployment(namespace=namespace, name='helm-operator')
    except client.exceptions.ApiException as e:
        print('No helm-operator deployed. Skipping.')
    else:
        helm_deployment.spec.replicas = 0
        appsV1Api_instance.patch_namespaced_deployment(namespace=namespace, name='helm-operator', body=helm_deployment)
        print('Deployment helm-operator replicas set to 0.')

    try:
        nodailyclean_deployments = appsV1Api_instance.list_namespaced_deployment(namespace=namespace, watch=False, label_selector='axa.com/dailyclean=false')
    except client.exceptions.ApiException as e:
        print('Error while listing disabled dailyclean Deployments (%s).\n' % e)
    else:
        for deployment in nodailyclean_deployments.items:
            print('Dailyclean disabled for deployment %s.' % deployment.metadata.name)

    try:
        deployments = appsV1Api_instance.list_namespaced_deployment(namespace=namespace, watch=False, label_selector='axa.com/dailyclean!=false')
    except client.exceptions.ApiException as e:
        print('Error while listing Deployments (%s).\n' % e)
    else:
        for deployment in deployments.items:
            if deployment.metadata.name == 'flux' or deployment.metadata.name == 'helm-operator':
                continue
            deployment.spec.replicas = 0
            appsV1Api_instance.patch_namespaced_deployment(namespace=namespace, name=deployment.metadata.name, body=deployment)
            print('Deployment %s replicas set to 0.' % deployment.metadata.name)

    try:
        stateful_sets = appsV1Api_instance.list_namespaced_stateful_set(namespace=namespace, watch=False, label_selector='axa.com/dailyclean=true')
    except client.exceptions.ApiException as e:
        print('Error while listing StatefulSet (%s).\n' % e)
    else:
        for stateful_set in stateful_sets.items:
            stateful_set.spec.replicas = 0
            appsV1Api_instance.patch_namespaced_stateful_set(namespace=namespace, name=stateful_set.metadata.name, body=stateful_set)
            print('StatefulSet %s replicas set to 0.' % stateful_set.metadata.name)


if __name__ == '__main__':
    main()