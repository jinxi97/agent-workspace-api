import uuid
from urllib.parse import quote

from agentic_sandbox import SandboxClient
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.config import SANDBOX_API_URL, SANDBOX_NAMESPACE, SANDBOX_TEMPLATE_NAME
from app.dependencies import get_sandbox_or_404, workspaces
from app.models.schemas import ExecuteRequest

router = APIRouter()


@router.post("/workspace")
def create_workspace():
    workspace_id = str(uuid.uuid4())
    sandbox = SandboxClient(
        template_name=SANDBOX_TEMPLATE_NAME,
        namespace=SANDBOX_NAMESPACE,
        api_url=SANDBOX_API_URL,
    )
    sandbox.__enter__()
    workspaces[workspace_id] = sandbox
    return {"workspace_id": workspace_id}


@router.post("/execute")
def exec_command(req: ExecuteRequest):
    sandbox = get_sandbox_or_404(req.workspace_id)
    result = sandbox.run(req.command)
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
    }


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
