import asyncio
import logging
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from kubernetes.client.exceptions import ApiException

from app.config import SANDBOX_NAMESPACE, SANDBOX_TEMPLATE_NAME
from app.dependencies import WORKSPACE_TIMEOUT_SECONDS, create_sandbox, require_any_auth, require_api_key
from app.models.schemas import ExecuteRequest
from app.services.k8s import get_k8s_custom_api, get_sandbox_status, watch_sandbox_until_ready
from utils.db import create_workspace_record, delete_workspace_record, get_workspaces_by_user_id, update_workspace_pod_name, verify_workspace_ownership

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/workspaces",
    tags=["workspaces"],
)

CLAIM_API_GROUP = "extensions.agents.x-k8s.io"
CLAIM_API_VERSION = "v1alpha1"
CLAIM_PLURAL = "sandboxclaims"


def _get_user_id(request: Request) -> uuid.UUID:
    """Resolve user_id from whichever auth method was used."""
    uid = getattr(request.state, "user_id", None) or getattr(request.state, "api_key_user_id", None)
    if not uid:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return uuid.UUID(uid)


async def _verify_ownership(request: Request, claim_name: str):
    """Raise 404 if the authenticated user does not own the claim."""
    user_id = _get_user_id(request)
    if not await verify_workspace_ownership(user_id, claim_name):
        raise HTTPException(status_code=404, detail="Workspace not found for this user")


@router.post("", dependencies=[Depends(require_api_key)])
async def create_workspace(request: Request):
    user_id = _get_user_id(request)

    claim_name = f"workspace-claim-{os.urandom(4).hex()}"
    workspace_id = uuid.uuid4()

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

    # Record ownership in the database (fire-and-forget, log on failure)
    async def _persist():
        try:
            await create_workspace_record(user_id, workspace_id, claim_name, SANDBOX_TEMPLATE_NAME)
        except Exception:
            logger.exception("Failed to persist workspace record: claim=%s user=%s", claim_name, user_id)

    asyncio.create_task(_persist())

    return {
        "workspace_id": str(workspace_id),
        "claim_name": claim_name,
        "status": "creating",
        "template_name": SANDBOX_TEMPLATE_NAME,
        "namespace": SANDBOX_NAMESPACE,
    }


@router.get("", dependencies=[Depends(require_any_auth)])
async def list_workspaces(request: Request):
    user_id = _get_user_id(request)
    workspaces = await get_workspaces_by_user_id(user_id)

    loop = asyncio.get_event_loop()

    # Query all statuses concurrently (k8s client is sync, so use executor)
    async def _get_status(ws):
        status, discovered_pod_name = await loop.run_in_executor(
            None, get_sandbox_status, ws.claim_name, SANDBOX_NAMESPACE, ws.pod_name,
        )
        # Cache pod_name on first discovery
        if discovered_pod_name and not ws.pod_name:
            asyncio.create_task(update_workspace_pod_name(ws.claim_name, discovered_pod_name))
        return {
            "workspace_id": str(ws.id),
            "claim_name": ws.claim_name,
            "template_name": ws.template_name,
            "created_at": ws.created_at.isoformat(),
            "status": status,
        }

    results = await asyncio.gather(*[_get_status(ws) for ws in workspaces])
    return list(results)


@router.get("/{claim_name}/events", dependencies=[Depends(require_api_key)])
async def workspace_events(request: Request, claim_name: str, namespace: str):
    """SSE stream that watches the sandbox until it becomes ready."""
    await _verify_ownership(request, claim_name)

    def event_stream():
        for sse_chunk in watch_sandbox_until_ready(
            claim_name=claim_name,
            namespace=namespace,
            timeout_seconds=WORKSPACE_TIMEOUT_SECONDS,
        ):
            yield sse_chunk

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/{claim_name}/execute", dependencies=[Depends(require_api_key)])
async def exec_command(request: Request, claim_name: str, req: ExecuteRequest):
    await _verify_ownership(request, claim_name)

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


@router.delete("/{claim_name}", dependencies=[Depends(require_any_auth)])
async def delete_workspace(request: Request, claim_name: str, namespace: str = SANDBOX_NAMESPACE):
    await _verify_ownership(request, claim_name)
    user_id = _get_user_id(request)

    # Delete the k8s claim
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

    # Remove from database
    await delete_workspace_record(user_id, claim_name)

    return {"deleted": True}
