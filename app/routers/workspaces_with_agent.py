from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.dependencies import create_sandbox
from app.models.schemas import AnswerRequest, CreateChatRequest, SendMessageRequest

router = APIRouter()


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
