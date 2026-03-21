from agentic_sandbox import SandboxClient
from fastapi import HTTPException, Request

from utils.auth import AuthError, decode_jwt

# Store active workspaces (in-memory cache of SandboxClient instances)
workspaces: dict[str, SandboxClient] = {}


async def require_auth(request: Request) -> dict:
    """FastAPI dependency that validates JWT and returns claims.

    Sets request.state.user_id and request.state.workspace_id as a
    convenience for handlers that receive the Request directly.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = auth_header[len("Bearer "):]
    try:
        claims = decode_jwt(token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc

    request.state.user_id = claims["sub"]
    request.state.workspace_id = claims.get("workspace_id")
    return claims


def get_sandbox_or_404(workspace_id: str) -> SandboxClient:
    sandbox = workspaces.get(workspace_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return sandbox
