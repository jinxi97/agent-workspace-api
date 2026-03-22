import json
import uuid

from agentic_sandbox import SandboxClient
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from kubernetes.client.exceptions import ApiException

from app.config import SANDBOX_API_URL, SANDBOX_TEMPLATE_NAME, SNAPSHOT_NAMESPACE
from app.dependencies import RESTORE_TIMEOUT_SECONDS, get_sandbox_or_404, workspaces
from app.models.schemas import SnapshotRestoreRequest, SnapshotTriggerRequest
from app.services.k8s import (
    create_restore_template,
    ensure_snapshot_policy,
    get_k8s_core_api,
    get_k8s_custom_api,
    is_snapshot_ready,
    require_snapshot_exists,
    watch_sandbox_until_ready,
)

router = APIRouter(prefix="/snapshots", tags=["snapshots"])


@router.post("/triggers")
def create_snapshot_trigger(req: SnapshotTriggerRequest):
    sandbox = get_sandbox_or_404(req.workspace_id)
    target_pod = sandbox.pod_name
    if not target_pod:
        raise HTTPException(
            status_code=400,
            detail="Workspace has no resolved pod_name yet",
        )

    snapshot_group = req.workspace_id

    # 1. Label the pod with the snapshot-group (= workspace_id).
    core_api = get_k8s_core_api()
    try:
        core_api.patch_namespaced_pod(
            name=target_pod,
            namespace=SNAPSHOT_NAMESPACE,
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
        ensure_snapshot_policy(api, snapshot_group)
    except ApiException as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create snapshot policy: {exc.body or str(exc)}",
        ) from exc

    # 3. Create the manual trigger.
    trigger_name = f"{target_pod}-snapshot-{uuid.uuid4().hex[:8]}"
    body = {
        "apiVersion": "podsnapshot.gke.io/v1alpha1",
        "kind": "PodSnapshotManualTrigger",
        "metadata": {
            "name": trigger_name,
            "namespace": SNAPSHOT_NAMESPACE,
            "labels": {"snapshot-group": snapshot_group},
        },
        "spec": {
            "targetPod": target_pod,
        },
    }

    try:
        created = api.create_namespaced_custom_object(
            group="podsnapshot.gke.io",
            version="v1alpha1",
            namespace=SNAPSHOT_NAMESPACE,
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
        "target_pod": created.get("spec", {}).get("targetPod", target_pod),
        "api_version": created.get("apiVersion", "podsnapshot.gke.io/v1alpha1"),
        "kind": created.get("kind", "PodSnapshotManualTrigger"),
    }


@router.delete("/triggers/{trigger_name}")
def delete_snapshot_trigger(trigger_name: str):
    api = get_k8s_custom_api()
    try:
        api.delete_namespaced_custom_object(
            group="podsnapshot.gke.io",
            version="v1alpha1",
            namespace=SNAPSHOT_NAMESPACE,
            plural="podsnapshotmanualtriggers",
            name=trigger_name,
        )
    except ApiException as exc:
        if exc.status == 404:
            raise HTTPException(status_code=404, detail=f"Trigger not found: {trigger_name}") from exc
        raise HTTPException(status_code=500, detail=exc.body or str(exc)) from exc

    return {"deleted": True}


@router.get("/status")
def get_snapshot_status(trigger_name: str):
    api = get_k8s_custom_api()
    try:
        trigger = api.get_namespaced_custom_object(
            group="podsnapshot.gke.io",
            version="v1alpha1",
            namespace=SNAPSHOT_NAMESPACE,
            plural="podsnapshotmanualtriggers",
            name=trigger_name,
        )
    except ApiException as exc:
        if exc.status == 404:
            raise HTTPException(status_code=404, detail=f"Trigger not found: {trigger_name}") from exc
        raise HTTPException(status_code=500, detail=exc.body or str(exc)) from exc

    trigger_status = trigger.get("status", {})
    snapshot_created = trigger_status.get("snapshotCreated")
    if not snapshot_created:
        return {"ready": False, "snapshot_name": None}
    snapshot_name = snapshot_created.get("name")
    if not snapshot_name:
        return {"ready": False, "snapshot_name": None}

    try:
        snapshot = api.get_namespaced_custom_object(
            group="podsnapshot.gke.io",
            version="v1alpha1",
            namespace=SNAPSHOT_NAMESPACE,
            plural="podsnapshots",
            name=snapshot_name,
        )
    except ApiException as exc:
        if exc.status == 404:
            return {"ready": False, "snapshot_name": snapshot_name}
        raise HTTPException(status_code=500, detail=exc.body or str(exc)) from exc

    return {
        "ready": is_snapshot_ready(snapshot),
        "snapshot_name": snapshot_name,
    }


@router.post("/restore")
def restore_from_snapshot(req: SnapshotRestoreRequest):
    """Restore a new workspace from the latest snapshot of the given workspace."""
    api = get_k8s_custom_api()
    snapshot_group = req.workspace_id

    # 0. Fail fast if no snapshot has ever been created for this workspace.
    require_snapshot_exists(api, snapshot_group)

    # 1. Ensure the snapshot-group policy exists (idempotent).
    try:
        ensure_snapshot_policy(api, snapshot_group)
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
            "namespace": SNAPSHOT_NAMESPACE,
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
            namespace=SNAPSHOT_NAMESPACE,
            plural="sandboxclaims",
            body=body,
        )
    except ApiException as exc:
        detail = exc.body or str(exc)
        if exc.status == 409:
            raise HTTPException(status_code=409, detail=f"sandboxclaim already exists: {claim_name}") from exc
        raise HTTPException(status_code=500, detail=f"failed to create sandboxclaim: {detail}") from exc

    # 4. Return immediately — caller uses GET /restore/{workspace_id}/events to stream status.
    workspace_id = str(uuid.uuid4())

    return {
        "workspace_id": workspace_id,
        "status": "restoring",
        "claim_name": claim_name,
        "template_name": restore_template_name,
        "namespace": SNAPSHOT_NAMESPACE,
    }


@router.get("/restore/{workspace_id}/events")
def restore_events(
    workspace_id: str,
    claim_name: str,
    template_name: str,
    namespace: str,
):
    """SSE stream that watches the restored sandbox until it becomes ready.

    The client opens this connection after POST /restore, passing back
    the claim_name, template_name, and namespace from the POST response.

    Events:
        event: status
        data: {"status": "restoring"}

        event: status
        data: {"status": "ready", "sandbox": {...}}

        event: status
        data: {"status": "failed", "detail": "..."}
    """
    if workspace_id in workspaces:
        def already_ready():
            yield f"event: status\ndata: {json.dumps({'status': 'ready'})}\n\n"
        return StreamingResponse(already_ready(), media_type="text/event-stream")

    def event_stream():
        for sse_chunk in watch_sandbox_until_ready(
            claim_name=claim_name,
            namespace=namespace,
            timeout_seconds=RESTORE_TIMEOUT_SECONDS,
        ):
            yield sse_chunk

            if '"status": "ready"' in sse_chunk:
                for line in sse_chunk.strip().splitlines():
                    if line.startswith("data: "):
                        data = json.loads(line[6:])
                        sandbox_info = data.get("sandbox", {})
                        break

                sandbox = SandboxClient(
                    template_name=template_name,
                    namespace=namespace,
                    api_url=SANDBOX_API_URL,
                )
                sandbox.claim_name = claim_name
                sandbox.sandbox_name = sandbox_info.get("sandbox_name")
                sandbox.annotations = sandbox_info.get("annotations", {})
                sandbox.pod_name = sandbox_info.get("pod_name", sandbox.sandbox_name)

                workspaces[workspace_id] = sandbox

    return StreamingResponse(event_stream(), media_type="text/event-stream")
