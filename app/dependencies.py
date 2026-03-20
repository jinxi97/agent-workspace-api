from agentic_sandbox import SandboxClient
from fastapi import HTTPException

# Store active workspaces (in-memory cache of SandboxClient instances)
workspaces: dict[str, SandboxClient] = {}


def get_sandbox_or_404(workspace_id: str) -> SandboxClient:
    sandbox = workspaces.get(workspace_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return sandbox
