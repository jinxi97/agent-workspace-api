import json
import os
import uuid

from agentic_sandbox import SandboxClient
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from kubernetes.client.exceptions import ApiException

from app.config import SANDBOX_API_URL, SANDBOX_NAMESPACE, SANDBOX_TEMPLATE_NAME
from app.dependencies import WORKSPACE_TIMEOUT_SECONDS, get_sandbox_or_404, workspaces
from app.models.schemas import ExecuteRequest
from app.services.k8s import get_k8s_custom_api, watch_sandbox_until_ready

# router = APIRouter(dependencies=[Depends(require_api_key)])
router = APIRouter(tags=["workspaces"])

CLAIM_API_GROUP = "extensions.agents.x-k8s.io"
CLAIM_API_VERSION = "v1alpha1"
CLAIM_PLURAL = "sandboxclaims"


@router.post("/workspace")
def create_workspace():
    workspace_id = str(uuid.uuid4())
    claim_name = f"sandbox-claim-{os.urandom(4).hex()}"

    body = {
        "apiVersion": f"{CLAIM_API_GROUP}/{CLAIM_API_VERSION}",
        "kind": "SandboxClaim",
        "metadata": {
            "name": claim_name,
            "namespace": SANDBOX_NAMESPACE,
            "labels": {"app": "agent-sandbox-workload"},
        },
        "spec": {"sandboxTemplateRef": {"name": SANDBOX_TEMPLATE_NAME}},
    }

    api = get_k8s_custom_api()
    try:
        api.create_namespaced_custom_object(
            group=CLAIM_API_GROUP,
            version=CLAIM_API_VERSION,
            namespace=SANDBOX_NAMESPACE,
            plural=CLAIM_PLURAL,
            body=body,
        )
    except ApiException as exc:
        detail = exc.body or str(exc)
        if exc.status == 409:
            raise HTTPException(status_code=409, detail=f"Claim already exists: {claim_name}") from exc
        raise HTTPException(status_code=500, detail=f"Failed to create sandbox claim: {detail}") from exc

    return {
        "workspace_id": workspace_id,
        "status": "creating",
        "claim_name": claim_name,
        "template_name": SANDBOX_TEMPLATE_NAME,
        "namespace": SANDBOX_NAMESPACE,
    }


@router.get("/workspace/{workspace_id}/events")
def workspace_events(
    workspace_id: str,
    claim_name: str,
    template_name: str,
    namespace: str,
):
    """SSE stream that watches the sandbox until it becomes ready.

    The client opens this connection after POST /workspace, passing back
    the claim_name, template_name, and namespace from the POST response.

    Events:
        event: status
        data: {"status": "creating"}

        event: status
        data: {"status": "ready", "sandbox": {...}}

        event: status
        data: {"status": "failed", "detail": "..."}
    """
    # Already finalized from a previous connection?
    if workspace_id in workspaces:
        def already_ready():
            yield f"event: status\ndata: {json.dumps({'status': 'ready'})}\n\n"
        return StreamingResponse(already_ready(), media_type="text/event-stream")

    def event_stream():
        for sse_chunk in watch_sandbox_until_ready(
            claim_name=claim_name,
            namespace=namespace,
            timeout_seconds=WORKSPACE_TIMEOUT_SECONDS,
        ):
            yield sse_chunk

            # On ready, finalize the SandboxClient into the workspaces dict.
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


@router.post("/execute")
def exec_command(req: ExecuteRequest):
    sandbox = get_sandbox_or_404(req.workspace_id)
    result = sandbox.run(req.command)
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
    }


@router.delete("/workspaces/{workspace_id}")
def delete_workspace(workspace_id: str):
    sandbox = workspaces.pop(workspace_id, None)
    if sandbox:
        sandbox.__exit__(None, None, None)
    return {"deleted": True}
