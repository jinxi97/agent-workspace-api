from urllib.parse import quote

from agentic_sandbox import SandboxClient
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.dependencies import get_sandbox_or_404, workspaces
from app.schemas import (
    AnswerRequest,
    CreateChatRequest,
    ExecuteRequest,
    SendMessageRequest,
)

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


@router.post("/execute")
def exec_command(req: ExecuteRequest):
    sandbox = get_sandbox_or_404(req.workspace_id)
    result = sandbox.run(req.command)
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
    }


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


@router.get("/workspaces/{workspace_id}/files/download/{file_path:path}")
def download_workspace_file(workspace_id: str, file_path: str):
    sandbox = get_sandbox_or_404(workspace_id)

    try:
        encoded_path = quote(file_path, safe="/")
        response = sandbox._request("GET", f"api/files/download/{encoded_path}", stream=True)
        response.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error communicating with sandbox: {exc}") from exc

    headers = {}
    for header in ("content-disposition", "content-length", "last-modified", "etag"):
        value = response.headers.get(header)
        if value:
            headers[header] = value

    return StreamingResponse(
        response.iter_content(chunk_size=None),
        media_type=response.headers.get("content-type", "application/octet-stream"),
        headers=headers,
    )


@router.delete("/workspaces/{workspace_id}")
def delete_workspace(workspace_id: str):
    sandbox = workspaces.pop(workspace_id, None)
    if sandbox:
        sandbox.__exit__(None, None, None)
    return {"deleted": True}
