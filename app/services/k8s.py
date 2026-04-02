import json
import logging
import uuid
from collections.abc import Generator

from fastapi import HTTPException
from kubernetes import client, config, watch
from kubernetes.client.exceptions import ApiException

from app.config import SNAPSHOT_STORAGE_CONFIG_NAME

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


def ensure_snapshot_policy(api: client.CustomObjectsApi, snapshot_group: str, namespace: str) -> None:
    """Create a PodSnapshotPolicy scoped to this snapshot group (idempotent)."""
    policy_name = f"psp-{snapshot_group}"
    body = {
        "apiVersion": "podsnapshot.gke.io/v1alpha1",
        "kind": "PodSnapshotPolicy",
        "metadata": {
            "name": policy_name,
            "namespace": namespace,
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
            namespace=namespace,
            plural="podsnapshotpolicies",
            body=body,
        )
    except ApiException as exc:
        if exc.status == 409:
            return  # already exists
        raise


def find_latest_ready_snapshot(
    api: client.CustomObjectsApi,
    snapshot_group: str,
    namespace: str,
) -> str:
    """Return the name of the latest ready PodSnapshot for the given group.

    Raises HTTPException(404) if none is found.
    """
    triggers = api.list_namespaced_custom_object(
        group="podsnapshot.gke.io",
        version="v1alpha1",
        namespace=namespace,
        plural="podsnapshotmanualtriggers",
        label_selector=f"snapshot-group={snapshot_group}",
    )
    best_name: str | None = None
    best_time: str = ""
    for trigger in triggers.get("items", []):
        snap_name = (
            trigger.get("status", {}).get("snapshotCreated", {}).get("name")
        )
        if not snap_name:
            continue
        try:
            snap = api.get_namespaced_custom_object(
                group="podsnapshot.gke.io",
                version="v1alpha1",
                namespace=namespace,
                plural="podsnapshots",
                name=snap_name,
            )
        except ApiException:
            continue
        if not is_snapshot_ready(snap):
            continue
        created = snap.get("metadata", {}).get("creationTimestamp", "")
        if created > best_time:
            best_time = created
            best_name = snap_name

    if not best_name:
        raise HTTPException(
            status_code=404,
            detail=f"No ready snapshot found for workspace: {snapshot_group}",
        )
    return best_name


def create_restore_template(
    api: client.CustomObjectsApi,
    base_template_name: str,
    snapshot_group: str,
    namespace: str,
    snapshot_name: str,
) -> str:
    """Clone a SandboxTemplate with the snapshot-group label and restore annotation.

    Each restore creates a unique template so that the ``podsnapshot.gke.io/ps-name``
    annotation points to the correct snapshot.

    Returns the name of the newly created template.
    """
    base = api.get_namespaced_custom_object(
        group="extensions.agents.x-k8s.io",
        version="v1alpha1",
        namespace=namespace,
        plural="sandboxtemplates",
        name=base_template_name,
    )

    restore_name = f"restore-{snapshot_group}-{uuid.uuid4().hex[:8]}"
    pod_template = base.get("spec", {}).get("podTemplate", {})
    pod_meta = pod_template.get("metadata", {})
    labels = {**(pod_meta.get("labels", {})), "snapshot-group": snapshot_group}
    annotations = {
        **(pod_meta.get("annotations", {})),
        "podsnapshot.gke.io/ps-name": snapshot_name,
    }

    body = {
        "apiVersion": "extensions.agents.x-k8s.io/v1alpha1",
        "kind": "SandboxTemplate",
        "metadata": {
            "name": restore_name,
            "namespace": namespace,
        },
        "spec": {
            "podTemplate": {
                "metadata": {"labels": labels, "annotations": annotations},
                "spec": pod_template.get("spec", {}),
            },
        },
    }

    api.create_namespaced_custom_object(
        group="extensions.agents.x-k8s.io",
        version="v1alpha1",
        namespace=namespace,
        plural="sandboxtemplates",
        body=body,
    )

    return restore_name


def get_sandbox_status(claim_name: str, namespace: str, pod_name: str | None = None) -> tuple[str, str | None]:
    """Return the Pod phase and pod_name for a sandbox claim.

    If pod_name is provided (cached), skips the Sandbox lookup.
    Returns (status, pod_name) — pod_name may be discovered on first call.

    Possible status values: "Not Found", "Creating", "Pending", "Running",
    "Succeeded", "Failed", "Deleted", "Unknown".
    """
    # If pod_name not cached, look it up from the Sandbox resource
    if not pod_name:
        api = get_k8s_custom_api()
        try:
            sandbox_obj = api.get_namespaced_custom_object(
                group=SANDBOX_API_GROUP,
                version=SANDBOX_API_VERSION,
                namespace=namespace,
                plural=SANDBOX_PLURAL,
                name=claim_name,
            )
        except ApiException as exc:
            if exc.status == 404:
                return "Not Found", None
            return "Unknown", None

        metadata = sandbox_obj.get("metadata", {})
        pod_name = metadata.get("annotations", {}).get(POD_NAME_ANNOTATION, metadata.get("name"))
        if not pod_name:
            return "Creating", None

    # Pod is the source of truth
    core_api = get_k8s_core_api()
    try:
        pod = core_api.read_namespaced_pod(name=pod_name, namespace=namespace)
        return pod.status.phase or "Unknown", pod_name
    except ApiException as exc:
        if exc.status == 404:
            return "Deleted", pod_name
        return "Unknown", pod_name


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


def watch_snapshot_until_ready(
    trigger_name: str,
    namespace: str,
    timeout_seconds: int = 300,
) -> Generator[str, None, None]:
    """Watch a snapshot trigger and yield SSE events until the snapshot is ready.

    Two-phase watch:
      1. Watch the PodSnapshotManualTrigger until status.snapshotCreated.name appears.
      2. Watch the PodSnapshot until it becomes ready.

    Yields SSE-formatted strings like watch_sandbox_until_ready.
    """
    api = get_k8s_custom_api()
    w = watch.Watch()
    logger.info("SSE watch: watching snapshot trigger %s in %s", trigger_name, namespace)

    # Fail fast if the trigger doesn't exist.
    try:
        trigger = api.get_namespaced_custom_object(
            group="podsnapshot.gke.io",
            version="v1alpha1",
            namespace=namespace,
            plural="podsnapshotmanualtriggers",
            name=trigger_name,
        )
    except ApiException as exc:
        if exc.status == 404:
            yield _sse_event("status", {
                "status": "failed",
                "detail": f"Snapshot trigger not found: {trigger_name}",
            })
            return
        raise

    yield _sse_event("status", {"status": "snapshotting", "trigger_name": trigger_name})

    # Check if the trigger already has a snapshot created.
    snapshot_name = (
        trigger.get("status", {}).get("snapshotCreated", {}).get("name")
    )

    # Phase 1: Watch the trigger until snapshotCreated.name appears.
    if not snapshot_name:
        try:
            for event in w.stream(
                func=api.list_namespaced_custom_object,
                namespace=namespace,
                group="podsnapshot.gke.io",
                version="v1alpha1",
                plural="podsnapshotmanualtriggers",
                field_selector=f"metadata.name={trigger_name}",
                timeout_seconds=timeout_seconds,
            ):
                event_type = event.get("type")
                if event_type not in ("ADDED", "MODIFIED"):
                    continue

                trigger_obj = event["object"]
                snapshot_name = (
                    trigger_obj.get("status", {}).get("snapshotCreated", {}).get("name")
                )
                if snapshot_name:
                    logger.info("SSE watch: trigger %s created snapshot %s", trigger_name, snapshot_name)
                    break

                yield _sse_event("status", {"status": "snapshotting"})

        except Exception:
            logger.exception("SSE watch: error watching trigger %s", trigger_name)
            yield _sse_event("status", {
                "status": "failed",
                "detail": f"Watch error for trigger {trigger_name}",
            })
            return

    if not snapshot_name:
        yield _sse_event("status", {
            "status": "failed",
            "detail": f"Trigger {trigger_name} did not produce a snapshot within {timeout_seconds}s",
        })
        return

    # Phase 2: Watch the PodSnapshot until it's ready.
    # First check if it's already ready.
    try:
        snapshot_obj = api.get_namespaced_custom_object(
            group="podsnapshot.gke.io",
            version="v1alpha1",
            namespace=namespace,
            plural="podsnapshots",
            name=snapshot_name,
        )
        if is_snapshot_ready(snapshot_obj):
            yield _sse_event("status", {"status": "ready", "snapshot_name": snapshot_name})
            return
    except ApiException as exc:
        if exc.status != 404:
            raise
        # Snapshot not yet created as an object — fall through to watch

    w2 = watch.Watch()
    try:
        for event in w2.stream(
            func=api.list_namespaced_custom_object,
            namespace=namespace,
            group="podsnapshot.gke.io",
            version="v1alpha1",
            plural="podsnapshots",
            field_selector=f"metadata.name={snapshot_name}",
            timeout_seconds=timeout_seconds,
        ):
            event_type = event.get("type")
            if event_type not in ("ADDED", "MODIFIED"):
                continue

            snapshot_obj = event["object"]
            if is_snapshot_ready(snapshot_obj):
                yield _sse_event("status", {"status": "ready", "snapshot_name": snapshot_name})
                logger.info("SSE watch: snapshot %s is ready", snapshot_name)
                return

            yield _sse_event("status", {"status": "snapshotting"})

    except Exception:
        logger.exception("SSE watch: error watching snapshot %s", snapshot_name)
        yield _sse_event("status", {
            "status": "failed",
            "detail": f"Watch error for snapshot {snapshot_name}",
        })
        return

    yield _sse_event("status", {
        "status": "failed",
        "detail": f"Snapshot {snapshot_name} not ready within {timeout_seconds}s",
    })


def require_snapshot_exists(api: client.CustomObjectsApi, snapshot_group: str, namespace: str) -> None:
    """Raise 404 if no ready snapshot exists for the given snapshot group."""
    try:
        triggers = api.list_namespaced_custom_object(
            group="podsnapshot.gke.io",
            version="v1alpha1",
            namespace=namespace,
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
                namespace=namespace,
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
