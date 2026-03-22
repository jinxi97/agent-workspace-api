import json
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from kubernetes.client.exceptions import ApiException


from app.config import SANDBOX_NAMESPACE, SANDBOX_TEMPLATE_NAME
from app.dependencies import WORKSPACE_TIMEOUT_SECONDS, create_sandbox
from app.models.schemas import ExecuteRequest
from app.services.k8s import get_k8s_custom_api, watch_sandbox_until_ready

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

CLAIM_API_GROUP = "extensions.agents.x-k8s.io"
CLAIM_API_VERSION = "v1alpha1"
CLAIM_PLURAL = "sandboxclaims"


@router.post("")
def create_workspace():
    claim_name = f"workspace-claim-{os.urandom(4).hex()}"

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
        "claim_name": claim_name,
        "status": "creating",
        "template_name": SANDBOX_TEMPLATE_NAME,
        "namespace": SANDBOX_NAMESPACE,
    }


@router.get("/{claim_name}/events")
def workspace_events(claim_name: str, namespace: str):
    """SSE stream that watches the sandbox until it becomes ready.

    Events:
        event: status
        data: {"status": "creating"}

        event: status
        data: {"status": "ready", "sandbox": {...}}

        event: status
        data: {"status": "failed", "detail": "..."}
    """
    def event_stream():
        for sse_chunk in watch_sandbox_until_ready(
            claim_name=claim_name,
            namespace=namespace,
            timeout_seconds=WORKSPACE_TIMEOUT_SECONDS,
        ):
            yield sse_chunk

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/{claim_name}/execute")
def exec_command(claim_name: str, req: ExecuteRequest):
    sandbox = create_sandbox(claim_name, req.namespace, req.pod_name)
    try:
        result = sandbox.run(req.command)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to reach sandbox pod {req.pod_name}: {exc}",
        ) from exc
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
    }


@router.delete("/{claim_name}")
def delete_workspace(claim_name: str, namespace: str = SANDBOX_NAMESPACE):
    api = get_k8s_custom_api()
    try:
        api.delete_namespaced_custom_object(
            group=CLAIM_API_GROUP,
            version=CLAIM_API_VERSION,
            namespace=namespace,
            plural=CLAIM_PLURAL,
            name=claim_name,
        )
    except ApiException as exc:
        if exc.status == 404:
            raise HTTPException(status_code=404, detail=f"Claim not found: {claim_name}") from exc
        raise HTTPException(status_code=500, detail=exc.body or str(exc)) from exc
    return {"deleted": True}
