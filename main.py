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
