import uuid

from fastapi import HTTPException
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from app.config import SNAPSHOT_NAMESPACE, SNAPSHOT_STORAGE_CONFIG_NAME


def is_snapshot_ready(snapshot: dict) -> bool:
    status = snapshot.get("status", {})
    if status.get("phase") == "AllSnapshotsAvailable":
        return True
    conditions = status.get("conditions", [])
    for cond in conditions:
        cond_type = cond.get("type")
        cond_status = cond.get("status")
        if cond_type in {"Ready", "Available"} and cond_status == "True":
            return True
    return False


def get_k8s_custom_api() -> client.CustomObjectsApi:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CustomObjectsApi()


def get_k8s_core_api() -> client.CoreV1Api:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CoreV1Api()


def ensure_snapshot_policy(api: client.CustomObjectsApi, snapshot_group: str) -> None:
    """Create a PodSnapshotPolicy scoped to this snapshot group (idempotent)."""
    policy_name = f"psp-{snapshot_group}"
    body = {
        "apiVersion": "podsnapshot.gke.io/v1alpha1",
        "kind": "PodSnapshotPolicy",
        "metadata": {
            "name": policy_name,
            "namespace": SNAPSHOT_NAMESPACE,
        },
        "spec": {
            "storageConfigName": SNAPSHOT_STORAGE_CONFIG_NAME,
            "selector": {
                "matchLabels": {
                    "snapshot-group": snapshot_group,
                },
            },
            "triggerConfig": {
                "type": "manual",
                "postCheckpoint": "resume",
            },
        },
    }
    try:
        api.create_namespaced_custom_object(
            group="podsnapshot.gke.io",
            version="v1alpha1",
            namespace=SNAPSHOT_NAMESPACE,
            plural="podsnapshotpolicies",
            body=body,
        )
    except ApiException as exc:
        if exc.status == 409:
            return  # already exists
        raise


def create_restore_template(
    api: client.CustomObjectsApi,
    base_template_name: str,
    snapshot_group: str,
) -> str:
    """Clone a SandboxTemplate with the snapshot-group label for restore.

    Returns the name of the newly created template.
    """
    base = api.get_namespaced_custom_object(
        group="extensions.agents.x-k8s.io",
        version="v1alpha1",
        namespace=SNAPSHOT_NAMESPACE,
        plural="sandboxtemplates",
        name=base_template_name,
    )

    restore_name = f"restore-{snapshot_group}"
    pod_template = base.get("spec", {}).get("podTemplate", {})
    pod_meta = pod_template.get("metadata", {})
    labels = {**(pod_meta.get("labels", {})), "snapshot-group": snapshot_group}

    body = {
        "apiVersion": "extensions.agents.x-k8s.io/v1alpha1",
        "kind": "SandboxTemplate",
        "metadata": {
            "name": restore_name,
            "namespace": SNAPSHOT_NAMESPACE,
        },
        "spec": {
            "podTemplate": {
                "metadata": {"labels": labels},
                "spec": pod_template.get("spec", {}),
            },
        },
    }

    try:
        api.create_namespaced_custom_object(
            group="extensions.agents.x-k8s.io",
            version="v1alpha1",
            namespace=SNAPSHOT_NAMESPACE,
            plural="sandboxtemplates",
            body=body,
        )
    except ApiException as exc:
        if exc.status != 409:
            raise
        # Already exists — fine for repeated restores from the same group

    return restore_name


def require_snapshot_exists(api: client.CustomObjectsApi, snapshot_group: str) -> None:
    """Raise 404 if no ready snapshot exists for the given snapshot group."""
    try:
        triggers = api.list_namespaced_custom_object(
            group="podsnapshot.gke.io",
            version="v1alpha1",
            namespace=SNAPSHOT_NAMESPACE,
            plural="podsnapshotmanualtriggers",
            label_selector=f"snapshot-group={snapshot_group}",
        )
    except ApiException as exc:
        raise HTTPException(status_code=500, detail=exc.body or str(exc)) from exc

    for trigger in triggers.get("items", []):
        snapshot_name = (
            trigger.get("status", {}).get("snapshotCreated", {}).get("name")
        )
        if not snapshot_name:
            continue
        try:
            snapshot = api.get_namespaced_custom_object(
                group="podsnapshot.gke.io",
                version="v1alpha1",
                namespace=SNAPSHOT_NAMESPACE,
                plural="podsnapshots",
                name=snapshot_name,
            )
        except ApiException:
            continue
        if is_snapshot_ready(snapshot):
            return  # at least one ready snapshot exists

    raise HTTPException(
        status_code=404,
        detail=f"No ready snapshot found for workspace: {snapshot_group}",
    )
