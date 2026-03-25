import os

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from kubernetes.client.exceptions import ApiException

from app.config import CLAUDE_AGENT_SANDBOX_TEMPLATE_NAME, SANDBOX_NAMESPACE
from app.dependencies import create_sandbox
from app.models.schemas import AnswerRequest, CreateChatRequest, SendMessageRequest
from app.services.k8s import get_k8s_custom_api

router = APIRouter()

CLAIM_API_GROUP = "extensions.agents.x-k8s.io"
CLAIM_API_VERSION = "v1alpha1"
CLAIM_PLURAL = "sandboxclaims"


@router.post("/workspaces-with-agent", status_code=201)
def create_workspace_with_agent():
    """Create a workspace using the agent sandbox template."""
    claim_name = f"agent-workspace-claim-{os.urandom(4).hex()}"

    body = {
        "apiVersion": f"{CLAIM_API_GROUP}/{CLAIM_API_VERSION}",
        "kind": "SandboxClaim",
        "metadata": {
            "name": claim_name,
            "namespace": SANDBOX_NAMESPACE,
            "labels": {"app": "agent-sandbox-workload"},
        },
        "spec": {"sandboxTemplateRef": {"name": CLAUDE_AGENT_SANDBOX_TEMPLATE_NAME}},
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
        "template_name": CLAUDE_AGENT_SANDBOX_TEMPLATE_NAME,
        "namespace": SANDBOX_NAMESPACE,
    }


def _sandbox_proxy(sandbox, method: str, path: str, **kwargs):
    """Common pattern: call sandbox._request, raise on error, return JSON."""
    try:
        response = sandbox._request(method, path, **kwargs)
        response.raise_for_status()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Error communicating with sandbox: {exc}",
        ) from exc
    return response.json()


@router.post("/workspaces/{claim_name}/chats", status_code=201)
def create_chat(claim_name: str, req: CreateChatRequest, namespace: str = Query(...), pod_name: str = Query(...)):
    sandbox = create_sandbox(claim_name, namespace, pod_name)
    return _sandbox_proxy(sandbox, "POST", "api/chats", json={"title": req.title})


@router.post("/workspaces/{claim_name}/chats/{chat_id}/messages")
def send_message(claim_name: str, chat_id: str, req: SendMessageRequest, namespace: str = Query(...), pod_name: str = Query(...)):
    sandbox = create_sandbox(claim_name, namespace, pod_name)

    try:
        response = sandbox._request(
            "POST",
            f"api/chats/{chat_id}/messages",
            json={"content": req.content},
            stream=True,
        )
        response.raise_for_status()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Error communicating with sandbox: {exc}",
        ) from exc

    return StreamingResponse(
        response.iter_content(chunk_size=None),
        media_type="text/event-stream",
    )


@router.post("/workspaces/{claim_name}/chats/{chat_id}/answer")
def answer_question(claim_name: str, chat_id: str, req: AnswerRequest, namespace: str = Query(...), pod_name: str = Query(...)):
    sandbox = create_sandbox(claim_name, namespace, pod_name)
    return _sandbox_proxy(sandbox, "POST", f"api/chats/{chat_id}/answer", json={"answers": req.answers})


@router.get("/workspaces/{claim_name}/chats")
def list_chats(claim_name: str, namespace: str = Query(...), pod_name: str = Query(...)):
    sandbox = create_sandbox(claim_name, namespace, pod_name)
    return _sandbox_proxy(sandbox, "GET", "api/chats")


@router.get("/workspaces/{claim_name}/chats/{chat_id}")
def get_chat(claim_name: str, chat_id: str, namespace: str = Query(...), pod_name: str = Query(...)):
    sandbox = create_sandbox(claim_name, namespace, pod_name)
    return _sandbox_proxy(sandbox, "GET", f"api/chats/{chat_id}")


@router.delete("/workspaces/{claim_name}/chats/{chat_id}")
def delete_chat(claim_name: str, chat_id: str, namespace: str = Query(...), pod_name: str = Query(...)):
    sandbox = create_sandbox(claim_name, namespace, pod_name)
    return _sandbox_proxy(sandbox, "DELETE", f"api/chats/{chat_id}")


@router.get("/workspaces/{claim_name}/chats/{chat_id}/messages")
def get_messages(claim_name: str, chat_id: str, namespace: str = Query(...), pod_name: str = Query(...)):
    sandbox = create_sandbox(claim_name, namespace, pod_name)
    return _sandbox_proxy(sandbox, "GET", f"api/chats/{chat_id}/messages")


@router.get("/workspaces/{claim_name}/chats/{chat_id}/artifacts")
def list_chat_artifacts(claim_name: str, chat_id: str, namespace: str = Query(...), pod_name: str = Query(...)):
    sandbox = create_sandbox(claim_name, namespace, pod_name)
    return _sandbox_proxy(sandbox, "GET", f"api/chats/{chat_id}/artifacts")
