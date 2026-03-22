import time
from dataclasses import dataclass

from agentic_sandbox import SandboxClient
from fastapi import HTTPException, Request

from utils.auth import AuthError, decode_jwt
from utils.db import verify_api_key

# Store active workspaces (in-memory cache of SandboxClient instances)
workspaces: dict[str, SandboxClient] = {}


@dataclass
class PendingRestore:
    """Tracks an in-flight restore whose sandbox is not yet ready."""

    claim_name: str
    template_name: str
    snapshot_group: str
    created_at: float  # time.monotonic()


# Restores that have been kicked off but not yet finalized.
pending_restores: dict[str, PendingRestore] = {}
RESTORE_TIMEOUT_SECONDS = 300


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


async def require_api_key(request: Request) -> None:
    """FastAPI dependency that validates an API key from the X-API-Key header."""
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")

    record = await verify_api_key(api_key)
    if not record:
        raise HTTPException(status_code=401, detail="Invalid API key")

    request.state.api_key_user_id = str(record.user_id)


def get_sandbox_or_404(workspace_id: str) -> SandboxClient:
    sandbox = workspaces.get(workspace_id)
    if not sandbox:
        if workspace_id in pending_restores:
            raise HTTPException(
                status_code=409,
                detail="Workspace is still restoring — poll GET /snapshots/restore/{workspace_id}/status until ready",
            )
        raise HTTPException(status_code=404, detail="Workspace not found")
    return sandbox
