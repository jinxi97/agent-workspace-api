from agentic_sandbox import SandboxClient
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.dependencies import get_sandbox_or_404
from app.models.schemas import AnswerRequest, CreateChatRequest, SendMessageRequest

router = APIRouter()


def _sandbox_proxy(sandbox: SandboxClient, method: str, path: str, **kwargs):
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


@router.post("/workspaces/{workspace_id}/chats", status_code=201)
def create_chat(workspace_id: str, req: CreateChatRequest):
    sandbox = get_sandbox_or_404(workspace_id)
    return _sandbox_proxy(sandbox, "POST", "api/chats", json={"title": req.title})


@router.post("/workspaces/{workspace_id}/chats/{chat_id}/messages")
def send_message(workspace_id: str, chat_id: str, req: SendMessageRequest):
    sandbox = get_sandbox_or_404(workspace_id)

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


@router.post("/workspaces/{workspace_id}/chats/{chat_id}/answer")
def answer_question(workspace_id: str, chat_id: str, req: AnswerRequest):
    sandbox = get_sandbox_or_404(workspace_id)
    return _sandbox_proxy(sandbox, "POST", f"api/chats/{chat_id}/answer", json={"answers": req.answers})


@router.get("/workspaces/{workspace_id}/chats")
def list_chats(workspace_id: str):
    sandbox = get_sandbox_or_404(workspace_id)
    return _sandbox_proxy(sandbox, "GET", "api/chats")


@router.get("/workspaces/{workspace_id}/chats/{chat_id}")
def get_chat(workspace_id: str, chat_id: str):
    sandbox = get_sandbox_or_404(workspace_id)
    return _sandbox_proxy(sandbox, "GET", f"api/chats/{chat_id}")


@router.delete("/workspaces/{workspace_id}/chats/{chat_id}")
def delete_chat(workspace_id: str, chat_id: str):
    sandbox = get_sandbox_or_404(workspace_id)
    return _sandbox_proxy(sandbox, "DELETE", f"api/chats/{chat_id}")


@router.get("/workspaces/{workspace_id}/chats/{chat_id}/messages")
def get_messages(workspace_id: str, chat_id: str):
    sandbox = get_sandbox_or_404(workspace_id)
    return _sandbox_proxy(sandbox, "GET", f"api/chats/{chat_id}/messages")


@router.get("/workspaces/{workspace_id}/chats/{chat_id}/artifacts")
def list_chat_artifacts(workspace_id: str, chat_id: str):
    sandbox = get_sandbox_or_404(workspace_id)
    return _sandbox_proxy(sandbox, "GET", f"api/chats/{chat_id}/artifacts")
