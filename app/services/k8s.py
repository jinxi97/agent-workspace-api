import json
import logging
import uuid
from collections.abc import Generator

from fastapi import HTTPException
from kubernetes import client, config, watch
from kubernetes.client.exceptions import ApiException

from app.config import SNAPSHOT_NAMESPACE, SNAPSHOT_STORAGE_CONFIG_NAME

logger = logging.getLogger(__name__)

SANDBOX_API_GROUP = "agents.x-k8s.io"
SANDBOX_API_VERSION = "v1alpha1"
SANDBOX_PLURAL = "sandboxes"
POD_NAME_ANNOTATION = "agents.x-k8s.io/pod-name"


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


def check_sandbox_status(
    api: client.CustomObjectsApi,
    claim_name: str,
    namespace: str,
) -> tuple[str, dict | None]:
    """Non-blocking check of whether a Sandbox is ready.

    Returns (status_str, sandbox_obj) where status_str is one of:
      - "restoring" — sandbox not yet created or not ready
      - "ready"     — sandbox is ready, sandbox_obj contains the resource
    """
    try:
        sandbox_obj = api.get_namespaced_custom_object(
            group="agents.x-k8s.io",
            version="v1alpha1",
            namespace=namespace,
            plural="sandboxes",
            name=claim_name,
        )
    except ApiException as exc:
        if exc.status == 404:
            return "restoring", None
        raise

    conditions = sandbox_obj.get("status", {}).get("conditions", [])
    for cond in conditions:
        if cond.get("type") == "Ready" and cond.get("status") == "True":
            return "ready", sandbox_obj

    return "restoring", None


def watch_sandbox_until_ready(
    claim_name: str,
    namespace: str,
    timeout_seconds: int = 300,
) -> Generator[str, None, None]:
    """Watch a Sandbox resource and yield SSE-formatted events until ready or timeout.

    Yields strings in SSE format:
        event: status\ndata: {"status": "creating", ...}\n\n
        event: status\ndata: {"status": "ready", "sandbox": {...}}\n\n

    The "sandbox" field in the "ready" event contains metadata needed to
    finalize the SandboxClient (sandbox_name, pod_name, annotations).
    """
    api = get_k8s_custom_api()
    w = watch.Watch()
    logger.info("SSE watch: watching sandbox %s in %s", claim_name, namespace)

    # Fail fast if the claim doesn't exist.
    try:
        api.get_namespaced_custom_object(
            group="extensions.agents.x-k8s.io",
            version="v1alpha1",
            namespace=namespace,
            plural="sandboxclaims",
            name=claim_name,
        )
    except ApiException as exc:
        if exc.status == 404:
            yield _sse_event("status", {
                "status": "failed",
                "detail": f"Sandbox claim not found: {claim_name}",
            })
            return
        raise

    # Send initial "creating" event immediately so the client knows the stream is alive.
    yield _sse_event("status", {"status": "creating", "claim_name": claim_name})

    try:
        for event in w.stream(
            func=api.list_namespaced_custom_object,
            namespace=namespace,
            group=SANDBOX_API_GROUP,
            version=SANDBOX_API_VERSION,
            plural=SANDBOX_PLURAL,
            field_selector=f"metadata.name={claim_name}",
            timeout_seconds=timeout_seconds,
        ):
            event_type = event.get("type")
            if event_type not in ("ADDED", "MODIFIED"):
                continue

            sandbox_obj = event["object"]
            conditions = sandbox_obj.get("status", {}).get("conditions", [])
            is_ready = any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in conditions
            )

            if is_ready:
                metadata = sandbox_obj.get("metadata", {})
                annotations = metadata.get("annotations", {})
                sandbox_info = {
                    "sandbox_name": metadata.get("name"),
                    "pod_name": annotations.get(POD_NAME_ANNOTATION, metadata.get("name")),
                    "annotations": annotations,
                }
                yield _sse_event("status", {"status": "ready", "sandbox": sandbox_info})
                logger.info("SSE watch: sandbox %s is ready", claim_name)
                return

            # Still creating — send a heartbeat so the client knows we're alive.
            yield _sse_event("status", {"status": "creating"})

    except Exception:
        logger.exception("SSE watch: error watching sandbox %s", claim_name)
        yield _sse_event("status", {
            "status": "failed",
            "detail": f"Watch error for sandbox {claim_name}",
        })
        return

    # If we get here, the watch timed out without becoming ready.
    yield _sse_event("status", {
        "status": "failed",
        "detail": f"Sandbox {claim_name} not ready within {timeout_seconds}s",
    })


def _sse_event(event_name: str, data: dict) -> str:
    """Format a dict as an SSE event string."""
    return f"event: {event_name}\ndata: {json.dumps(data)}\n\n"


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
