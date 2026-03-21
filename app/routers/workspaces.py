import uuid

from agentic_sandbox import SandboxClient
from fastapi import APIRouter, HTTPException

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


@router.delete("/workspaces/{workspace_id}")
def delete_workspace(workspace_id: str):
    sandbox = workspaces.pop(workspace_id, None)
    if sandbox:
        sandbox.__exit__(None, None, None)
    return {"deleted": True}
