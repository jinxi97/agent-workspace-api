import json
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from kubernetes.client.exceptions import ApiException

from app.config import SANDBOX_TEMPLATE_NAME
from app.dependencies import RESTORE_TIMEOUT_SECONDS, SNAPSHOT_TIMEOUT_SECONDS
from app.models.schemas import SnapshotRestoreRequest, SnapshotTriggerRequest
from app.services.k8s import (
    create_restore_template,
    ensure_snapshot_policy,
    get_k8s_core_api,
    get_k8s_custom_api,
    require_snapshot_exists,
    watch_sandbox_until_ready,
    watch_snapshot_until_ready,
)

router = APIRouter(prefix="/snapshots", tags=["snapshots"])


@router.post("/triggers")
def create_snapshot_trigger(req: SnapshotTriggerRequest):
    snapshot_group = req.claim_name

    # 1. Label the pod with the snapshot-group (= claim_name).
    core_api = get_k8s_core_api()
    try:
        core_api.patch_namespaced_pod(
            name=req.pod_name,
            namespace=req.namespace,
            body={"metadata": {"labels": {"snapshot-group": snapshot_group}}},
        )
    except ApiException as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to label pod for snapshot: {exc.body or str(exc)}",
        ) from exc

    # 2. Create a PodSnapshotPolicy scoped to this workspace (idempotent).
    api = get_k8s_custom_api()
    try:
        ensure_snapshot_policy(api, snapshot_group, namespace=req.namespace)
    except ApiException as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create snapshot policy: {exc.body or str(exc)}",
        ) from exc

    # 3. Create the manual trigger.
    trigger_name = f"{req.pod_name}-snapshot-{uuid.uuid4().hex[:8]}"
    body = {
        "apiVersion": "podsnapshot.gke.io/v1alpha1",
        "kind": "PodSnapshotManualTrigger",
        "metadata": {
            "name": trigger_name,
            "namespace": req.namespace,
            "labels": {"snapshot-group": snapshot_group},
        },
        "spec": {
            "targetPod": req.pod_name,
        },
    }

    try:
        created = api.create_namespaced_custom_object(
            group="podsnapshot.gke.io",
            version="v1alpha1",
            namespace=req.namespace,
            plural="podsnapshotmanualtriggers",
            body=body,
        )
    except ApiException as exc:
        detail = exc.body or str(exc)
        if exc.status == 409:
            raise HTTPException(status_code=409, detail=f"trigger already exists: {trigger_name}") from exc
        raise HTTPException(status_code=500, detail=f"failed to create snapshot trigger: {detail}") from exc

    return {
        "name": created["metadata"]["name"],
        "namespace": created["metadata"]["namespace"],
        "target_pod": created.get("spec", {}).get("targetPod", req.pod_name),
        "api_version": created.get("apiVersion", "podsnapshot.gke.io/v1alpha1"),
        "kind": created.get("kind", "PodSnapshotManualTrigger"),
    }


@router.delete("/triggers/{trigger_name}")
def delete_snapshot_trigger(trigger_name: str, namespace: str):
    api = get_k8s_custom_api()
    try:
        api.delete_namespaced_custom_object(
            group="podsnapshot.gke.io",
            version="v1alpha1",
            namespace=namespace,
            plural="podsnapshotmanualtriggers",
            name=trigger_name,
        )
    except ApiException as exc:
        if exc.status == 404:
            raise HTTPException(status_code=404, detail=f"Trigger not found: {trigger_name}") from exc
        raise HTTPException(status_code=500, detail=exc.body or str(exc)) from exc

    return {"deleted": True}


@router.get("/triggers/{trigger_name}/events")
def snapshot_events(trigger_name: str, namespace: str):
    """SSE stream that watches a snapshot trigger until the snapshot is ready.

    Events:
        event: status
        data: {"status": "snapshotting"}

        event: status
        data: {"status": "ready", "snapshot_name": "..."}

        event: status
        data: {"status": "failed", "detail": "..."}
    """
    def event_stream():
        for sse_chunk in watch_snapshot_until_ready(
            trigger_name=trigger_name,
            namespace=namespace,
            timeout_seconds=SNAPSHOT_TIMEOUT_SECONDS,
        ):
            yield sse_chunk

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/restore")
def restore_from_snapshot(req: SnapshotRestoreRequest):
    """Restore a new workspace from the latest snapshot of the given workspace."""
    api = get_k8s_custom_api()
    snapshot_group = req.claim_name

    # 0. Fail fast if no snapshot has ever been created for this workspace.
    require_snapshot_exists(api, snapshot_group, namespace=req.namespace)

    # 1. Ensure the snapshot-group policy exists (idempotent).
    try:
        ensure_snapshot_policy(api, snapshot_group, namespace=req.namespace)
    except ApiException as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to ensure snapshot policy: {exc.body or str(exc)}",
        ) from exc

    # 2. Create a dynamic SandboxTemplate for restore.
    try:
        restore_template_name = create_restore_template(
            api,
            base_template_name=SANDBOX_TEMPLATE_NAME,
            snapshot_group=snapshot_group,
            namespace=req.namespace,
        )
    except ApiException as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create restore template: {exc.body or str(exc)}",
        ) from exc

    # 3. Create a SandboxClaim referencing the dynamic template.
    claim_name = f"restore-{snapshot_group[:20]}-{uuid.uuid4().hex[:8]}"
    body = {
        "apiVersion": "extensions.agents.x-k8s.io/v1alpha1",
        "kind": "SandboxClaim",
        "metadata": {
            "name": claim_name,
            "namespace": req.namespace,
            "labels": {
                "app": "agent-sandbox-workload",
                "snapshot-group": snapshot_group,
            },
        },
        "spec": {
            "sandboxTemplateRef": {
                "name": restore_template_name,
            },
        },
    }

    try:
        api.create_namespaced_custom_object(
            group="extensions.agents.x-k8s.io",
            version="v1alpha1",
            namespace=req.namespace,
            plural="sandboxclaims",
            body=body,
        )
    except ApiException as exc:
        detail = exc.body or str(exc)
        if exc.status == 409:
            raise HTTPException(status_code=409, detail=f"sandboxclaim already exists: {claim_name}") from exc
        raise HTTPException(status_code=500, detail=f"failed to create sandboxclaim: {detail}") from exc

    return {
        "claim_name": claim_name,
        "status": "restoring",
        "template_name": restore_template_name,
        "namespace": req.namespace,
    }


@router.get("/restore/{claim_name}/events")
def restore_events(claim_name: str, namespace: str):
    """SSE stream that watches the restored sandbox until it becomes ready.

    Events:
        event: status
        data: {"status": "restoring"}

        event: status
        data: {"status": "ready", "sandbox": {...}}

        event: status
        data: {"status": "failed", "detail": "..."}
    """
    def event_stream():
        for sse_chunk in watch_sandbox_until_ready(
            claim_name=claim_name,
            namespace=namespace,
            timeout_seconds=RESTORE_TIMEOUT_SECONDS,
        ):
            yield sse_chunk

    return StreamingResponse(event_stream(), media_type="text/event-stream")
