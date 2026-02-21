import os
import uuid

from fastapi import FastAPI, HTTPException
from agentic_sandbox import SandboxClient
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from pydantic import BaseModel, Field

app = FastAPI()

SANDBOX_TEMPLATE_NAME = os.getenv("SANDBOX_TEMPLATE_NAME", "python-runtime-template")
SANDBOX_NAMESPACE = os.getenv("SANDBOX_NAMESPACE", "pod-snapshots-ns")
SANDBOX_API_URL = os.getenv(
    "SANDBOX_API_URL",
    "http://sandbox-router-svc.default.svc.cluster.local:8080",
)

# Local sandbox API URL for development
# Remember to run 'kubectl -n default port-forward svc/sandbox-router-svc 8080:8080' before running the application
LOCAL_SANDBOX_API_URL = "http://127.0.0.1:8080"
SNAPSHOT_NAMESPACE = os.getenv("SNAPSHOT_NAMESPACE", SANDBOX_NAMESPACE)

# Store active workspaces
workspaces: dict[str, SandboxClient] = {}


class SnapshotTriggerRequest(BaseModel):
    workspace_id: str = Field(
        ...,
        min_length=1,
        description="Workspace ID returned by POST /workspaces",
    )


class SnapshotRestoreRequest(BaseModel):
    snapshot_name: str = Field(
        ...,
        min_length=1,
        description="Exact PodSnapshot name to restore from",
    )


def _is_snapshot_ready(snapshot: dict) -> bool:
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


def _get_ready_snapshot_or_400(api: client.CustomObjectsApi, snapshot_name: str) -> dict:
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
            raise HTTPException(status_code=400, detail=f"Invalid snapshot_name: {snapshot_name}") from exc
        raise HTTPException(status_code=500, detail=exc.body or str(exc)) from exc

    if not _is_snapshot_ready(snapshot):
        raise HTTPException(
            status_code=400,
            detail=f"Snapshot is not ready for restore: {snapshot_name}",
        )
    return snapshot


def _get_k8s_custom_api() -> client.CustomObjectsApi:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CustomObjectsApi()

@app.get("/")
async def root():
    return {"message": "Hello World!"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/workspaces")
def create_workspace():
    # Generate a unique workspace ID
    workspace_id = str(uuid.uuid4())
    sandbox = SandboxClient(
        template_name=SANDBOX_TEMPLATE_NAME,
        namespace=SANDBOX_NAMESPACE,
        api_url=SANDBOX_API_URL,
    )
    sandbox.__enter__()  # Start the sandbox

    # Store reference to the sandbox
    workspaces[workspace_id] = sandbox

    return {"workspace_id": workspace_id}


@app.post("/workspaces/{workspace_id}/exec")
def exec_command(workspace_id: str, command: str):
    sandbox = workspaces.get(workspace_id)
    if not sandbox:
        raise HTTPException(404, "Workspace not found")

    result = sandbox.run(command)
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code
    }


@app.delete("/workspaces/{workspace_id}")
def delete_workspace(workspace_id: str):
    sandbox = workspaces.pop(workspace_id, None)
    if sandbox:
        sandbox.__exit__(None, None, None)  # Cleanup
    return {"deleted": True}


@app.post("/snapshots/triggers")
def create_snapshot_trigger(req: SnapshotTriggerRequest):
    sandbox = workspaces.get(req.workspace_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail="Workspace not found")
    target_pod = sandbox.pod_name
    if not target_pod:
        raise HTTPException(
            status_code=400,
            detail="Workspace has no resolved pod_name yet",
        )

    trigger_name = f"{target_pod}-snapshot-{uuid.uuid4().hex[:8]}"
    body = {
        "apiVersion": "podsnapshot.gke.io/v1alpha1",
        "kind": "PodSnapshotManualTrigger",
        "metadata": {
            "name": trigger_name,
            "namespace": SNAPSHOT_NAMESPACE,
        },
        "spec": {
            "targetPod": target_pod,
        },
    }

    api = _get_k8s_custom_api()
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


@app.delete("/snapshots/triggers/{trigger_name}")
def delete_snapshot_trigger(trigger_name: str):
    api = _get_k8s_custom_api()
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


@app.get("/snapshots/status")
def get_snapshot_status(
    trigger_name: str,
):
    api = _get_k8s_custom_api()
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
    snapshot_name = snapshot_created.get("name")
    if not snapshot_name:
        return {
            "ready": False,
            "snapshot_name": None,
        }

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
            return {
                "ready": False,
                "snapshot_name": snapshot_name,
            }
        raise HTTPException(status_code=500, detail=exc.body or str(exc)) from exc

    return {
        "ready": _is_snapshot_ready(snapshot),
        "snapshot_name": snapshot_name,
    }


@app.post("/snapshots/restore")
def restore_from_snapshot(req: SnapshotRestoreRequest):
    api = _get_k8s_custom_api()
    _get_ready_snapshot_or_400(api, req.snapshot_name)

    claim_name = f"restore-{req.snapshot_name[:20]}-{uuid.uuid4().hex[:8]}"
    body = {
        "apiVersion": "extensions.agents.x-k8s.io/v1alpha1",
        "kind": "SandboxClaim",
        "metadata": {
            "name": claim_name,
            "namespace": SNAPSHOT_NAMESPACE,
            "labels": {
                "app": "agent-sandbox-workload",
            },
            "annotations": {
                "podsnapshot.gke.io/ps-name": req.snapshot_name,
            },
        },
        "spec": {
            "sandboxTemplateRef": {
                "name": SANDBOX_TEMPLATE_NAME,
            },
        },
    }

    try:
        created = api.create_namespaced_custom_object(
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

    # Attach a workspace handle to the restored claim so callers can execute commands.
    sandbox = SandboxClient(
        template_name=SANDBOX_TEMPLATE_NAME,
        namespace=SNAPSHOT_NAMESPACE,
        api_url=SANDBOX_API_URL,
    )
    sandbox.claim_name = claim_name
    try:
        sandbox._wait_for_sandbox_ready()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"restore claim created but sandbox not ready: {exc}") from exc

    workspace_id = str(uuid.uuid4())
    workspaces[workspace_id] = sandbox

    return {"workspace_id": workspace_id}
